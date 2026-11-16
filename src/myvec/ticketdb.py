"""Тікети в БД (ADR-005): оновлення таблиць із файлів теки тікетів і read API для UI/пошуку.

Без HTTP і без ембеддингу: лише `stat` для відбитка, `tickets.load_dir` і одна транзакція запису."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from myvec import filters, projects, tickets


@dataclass
class RefreshResult:
    changed: bool
    n_tickets: int = 0
    n_errors: int = 0


def _inside(root, base: Path) -> bool:
    try:
        return Path(base).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def fingerprint(root, tickets_dir: str) -> str:
    """sha від `tickets_dir` і (rel, mtime_ns, size) усіх `*.md` теки; лише stat. Нема теки -> 'absent'."""
    base = Path(root) / tickets_dir
    if not base.is_dir() or not _inside(root, base):
        return "absent"
    entries = []
    for dp, dns, fns in os.walk(base):
        dns[:] = sorted(d for d in dns if d not in tickets.SKIP_DIRS)   # inbox/ не тікети (ADR-019)
        for fn in sorted(fns):
            if not fn.endswith(".md"):
                continue
            f = Path(dp) / fn
            try:
                st = f.lstat()
            except OSError:
                continue
            entries.append((f.relative_to(base).as_posix(), st.st_mtime_ns, st.st_size))
    h = hashlib.sha256(tickets_dir.encode())
    for e in entries:
        h.update(repr(e).encode())
    return h.hexdigest()


def _mask_value(v):
    """Mask string leaves of a JSON-like value (numbers/bools stay); a string with a hard secret becomes None."""
    if isinstance(v, str):
        return _safe_text(v)
    if isinstance(v, dict):
        return {str(k): _mask_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_mask_value(x) for x in v]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return _safe_text(str(v))


def _safe_text(s: str | None) -> str | None:
    if s is None:
        return None
    if filters.has_hard_secret(s):
        return None
    return filters.mask_secrets(s)[0]


def _build(root, tickets_dir: str, extra_issues: list[tickets.Issue]):
    ts = tickets.load_dir(root, tickets_dir) if _inside(root, Path(root) / tickets_dir) else tickets.TicketSet()
    kept = tickets.TicketSet()
    for tid, t in ts.tickets.items():
        if not filters.is_denied_path(t.path):
            kept.tickets[tid] = t
    kept.issues = [i for i in ts.issues if not filters.is_denied_path(i.path)] + list(extra_issues)
    return kept, tickets.check(kept), tickets.progress_all(kept)


def refresh_project(db: sqlite3.Connection, project_path: str, root, tickets_dir: str = projects.DEFAULT_TICKETS_DIR,
                    *, force: bool = False, bad_dir: str | None = None) -> RefreshResult:
    """Перечитує тікети проєкту, якщо відбиток теки змінився (або `force`)."""
    fp = fingerprint(root, tickets_dir)
    if bad_dir is not None:
        fp += "|bad:" + bad_dir
    row = db.execute("SELECT fingerprint, n_tickets, n_errors FROM ticket_dirs WHERE project_path=?",
                     (project_path,)).fetchone()
    if not force:
        if row is not None and row["fingerprint"] == fp:
            return RefreshResult(False, row["n_tickets"], row["n_errors"])
        if row is None and fp == "absent":
            return RefreshResult(False)
    extra = []
    if bad_dir is not None:
        extra.append(tickets.Issue("warning", "bad_tickets_dir", None, projects.MARKER,
                                   f"[tickets] dir «{bad_dir}» недійсний, використано {projects.DEFAULT_TICKETS_DIR}"))
    ts, issues, prog = _build(root, tickets_dir, extra)
    n_err = sum(1 for i in issues if i.severity == "error")
    own_tx = not db.in_transaction
    if own_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        for tbl in ("tickets", "ticket_edges", "ticket_issues", "ticket_dirs"):
            db.execute(f"DELETE FROM {tbl} WHERE project_path=?", (project_path,))
        for t in ts.tickets.values():
            body = None if filters.has_hard_secret(t.body) else filters.mask_secrets(t.body)[0]
            extra_json = (json.dumps(_mask_value(t.extra), ensure_ascii=False) if t.extra else None)
            db.execute(
                "INSERT INTO tickets(project_path, id, title, kind, status, status_raw, done_date, parent, size, branch,"
                " pr, summary, updated, path, body, body_dropped, ac_done, ac_total, progress, legacy, extra)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_path, t.id, _safe_text(t.title) or "", t.kind, t.status, _safe_text(t.status_raw), t.done_date, t.parent,
                 t.size, _safe_text(t.branch), _safe_text(t.pr), _safe_text(t.summary), t.updated, t.path, body, int(body is None),
                 t.ac_done, t.ac_total, prog.get(t.id), int(t.legacy), extra_json))
            for d in t.deps:
                db.execute("INSERT OR IGNORE INTO ticket_edges VALUES(?,?,?,'dep')", (project_path, t.id, d))
            for b in t.blocks:
                db.execute("INSERT OR IGNORE INTO ticket_edges VALUES(?,?,?,'block')", (project_path, t.id, b))
        for i in issues:
            db.execute("INSERT INTO ticket_issues VALUES(?,?,?,?,?,?)",
                       (project_path, i.severity, i.code, i.ticket_id, i.path, _safe_text(i.message) or ""))
        if fp != "absent" or ts.tickets or issues:
            db.execute("INSERT INTO ticket_dirs VALUES(?,?,?,?,?,?)",
                       (project_path, tickets_dir, fp, time.time(), len(ts.tickets), n_err))
        if own_tx:
            db.execute("COMMIT")
    except Exception:
        if own_tx:
            db.execute("ROLLBACK")
        raise
    return RefreshResult(True, len(ts.tickets), n_err)


def refresh_marker(db: sqlite3.Connection, marker: projects.Marker, **kw) -> RefreshResult:
    return refresh_project(db, marker.path, marker.root, marker.tickets_dir, bad_dir=marker.bad_tickets_dir, **kw)


# ---------------------------------------------------------------- read API (dict-и, JSON-готові)

def _edges(db, project_path: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    deps: dict[str, list[str]] = {}
    blocks: dict[str, list[str]] = {}
    for r in db.execute("SELECT src, dst, kind FROM ticket_edges WHERE project_path=? ORDER BY src, dst",
                        (project_path,)):
        (deps if r["kind"] == "dep" else blocks).setdefault(r["src"], []).append(r["dst"])
    return deps, blocks


def _node(r: sqlite3.Row, deps, blocks, *, body: bool = False) -> dict:
    d = {k: r[k] for k in ("id", "title", "kind", "status", "status_raw", "done_date", "parent", "size", "branch",
                           "pr", "summary", "updated", "path", "ac_done", "ac_total", "progress")}
    d["legacy"] = bool(r["legacy"])
    d["body_dropped"] = bool(r["body_dropped"])
    try:
        d["extra"] = json.loads(r["extra"]) if r["extra"] else {}
    except ValueError:
        d["extra"] = {}
    d["deps"] = deps.get(r["id"], [])
    d["blocks"] = blocks.get(r["id"], [])
    if body:
        d["body"] = r["body"]
    return d


def _project_progress(db, project_path: str) -> float | None:
    prog = [x[0] for x in db.execute(
        "SELECT t.progress FROM tickets t WHERE t.project_path=? AND t.progress IS NOT NULL AND "
        "(t.parent IS NULL OR t.parent='' OR t.parent NOT IN (SELECT id FROM tickets WHERE project_path=?))",
        (project_path, project_path))]
    return sum(prog) / len(prog) if prog else None


def _counts(db, project_path: str) -> dict[str, int]:
    """Статус -> кількість листків (група `kind: group` не рахується)."""
    return {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) AS n FROM tickets WHERE project_path=? AND kind!='group' GROUP BY status",
        (project_path,))}


def projects_summary(db: sqlite3.Connection, include_empty: bool = False, idle_min: float = 15.0, today=None,
                     now: float | None = None) -> list[dict]:
    """Проєкти з тікетами: кількості, середній відсоток кореневих вузлів, час оновлення, лічильники статусів.
    `include_empty` додає й проєкти з текою без канонічних тікетів (`n_tickets=0`)."""
    from myvec import problems
    from myvec import sessions as sess

    waiting_ids: dict[str, set] = {}   # tickets waiting by status or by a waiting session: counted once
    bound_all: dict[str, dict[str, list[dict]]] = {}   # project -> ticket id -> sessions (for health)
    for d in sess.list_sessions(db, now, idle_min=idle_min):
        if d["ticket_project"] and d["ticket_id"]:
            bound_all.setdefault(d["ticket_project"], {}).setdefault(d["ticket_id"], []).append(
                {"effective_state": d["effective_state"], "last_seen": d["last_seen"]})
        if d["effective_state"] == "waiting" and d["ticket_project"] and d["ticket_id"]:
            waiting_ids.setdefault(d["ticket_project"], set()).add(d["ticket_id"])
    out = []
    where = "" if include_empty else "WHERE n_tickets>0 "
    for r in db.execute(f"SELECT * FROM ticket_dirs {where}ORDER BY project_path"):
        pp = r["project_path"]
        upd = db.execute("SELECT MAX(updated) FROM tickets WHERE project_path=?", (pp,)).fetchone()[0]
        n_issues = db.execute("SELECT COUNT(*) FROM ticket_issues WHERE project_path=?", (pp,)).fetchone()[0]
        severities = {x[0] for x in db.execute("SELECT DISTINCT severity FROM ticket_issues WHERE project_path=?", (pp,))}
        health = _health(ticket_tree(db, pp), bound_all.get(pp, {}), today, now)
        problem_ids = {x[0] for x in db.execute(
            "SELECT id FROM tickets WHERE project_path=? AND kind!='group' AND (status='blocked' OR id IN "
            "(SELECT ticket_id FROM ticket_issues WHERE project_path=? AND ticket_id IS NOT NULL))", (pp, pp))}
        n_problems = len(problem_ids | {h["ticket_id"] for h in health})
        pr = db.execute("SELECT root FROM projects WHERE path=?", (pp,)).fetchone()
        process = problems.process_issues(pr["root"] if pr else None)
        n_process = len(process)
        severities |= {x.get("severity") or "warning" for x in process}
        if health:
            severities.add("health")
        severities = {x if x in ("error", "warning", "health") else "warning" for x in severities}
        worst = next((k for k in ("error", "warning", "health") if k in severities), None)
        status_waiting = {x[0] for x in db.execute(
            "SELECT id FROM tickets WHERE project_path=? AND kind!='group' AND status='waiting'", (pp,))}
        out.append({"project_path": pp, "n_tickets": r["n_tickets"], "n_errors": r["n_errors"],
                    "progress": _project_progress(db, pp), "refreshed_at": r["refreshed_at"],
                    "dir": r["dir"], "counts": _counts(db, pp), "updated_max": upd, "n_issues": n_issues, "n_problems": n_problems, "n_health": len(health),
                    "n_process": n_process, "worst": worst,
                    "n_waiting": len(waiting_ids.get(pp, set()) | status_waiting)})
    return out


def project_tickets(db: sqlite3.Connection, project_path: str, idle_min: float = 15.0, today=None,
                    now: float | None = None) -> dict | None:
    """Усе для вкладки «Тікети»: дерево, лічильники, відсоток проєкту, issues. None - проєкт без теки тікетів."""
    r = db.execute("SELECT * FROM ticket_dirs WHERE project_path=?", (project_path,)).fetchone()
    if r is None:
        return None
    nodes = _with_sessions(ticket_tree(db, project_path), db, project_path, idle_min)
    bound = {n["id"]: n["sessions"] for n in nodes}
    return {"project": project_path, "dir": r["dir"], "refreshed_at": r["refreshed_at"],
            "progress": _project_progress(db, project_path), "counts": _counts(db, project_path),
            "nodes": nodes, "issues": ticket_issues(db, project_path) + _health(nodes, bound, today, now)}


def project_problems(db: sqlite3.Connection, project_path: str, idle_min: float = 15.0, today=None,
                     now: float | None = None) -> dict | None:
    """Вкладка «Проблеми» (E3-04): issues + поради проєкту, згруповані за кодом, без вузлів і тексту тіл. None - нема теки."""
    from myvec import problems

    d = project_tickets(db, project_path, idle_min, today, now)
    if d is None:
        return None
    r = db.execute("SELECT root FROM projects WHERE path=?", (project_path,)).fetchone()
    root = r["root"] if r else None
    out = problems.group(d["issues"] + problems.process_issues(root), root)
    out["project"] = project_path
    return out


def _health(nodes: list[dict], bound: dict, today=None, now: float | None = None) -> list[dict]:
    """Поради здоров'я при читанні (ADR-020): dict-и вигляду `ticket_issues`, у таблицю не пишуться."""
    today = today or datetime.date.today()
    return [{"severity": i.severity, "code": i.code, "ticket_id": i.ticket_id, "path": i.path, "message": i.message}
            for i in tickets.health(nodes, today, bound, now)]


def _with_sessions(nodes: list[dict], db, project_path: str, idle_min: float) -> list[dict]:
    from myvec import sessions as sess

    bound = sess.by_ticket(db, project_path, idle_min=idle_min)
    for n in nodes:
        n["sessions"] = [{k: s[k] for k in ("session_id", "effective_state", "ticket_source", "last_seen", "wait_s")}
                         for s in bound.get(n["id"], [])]
    return nodes


def ticket_tree(db: sqlite3.Connection, project_path: str) -> list[dict]:
    """Плоский список вузлів (parent, progress, status, kind, deps/blocks), батьки перед дітьми, далі за path."""
    deps, blocks = _edges(db, project_path)
    nodes = [_node(r, deps, blocks) for r in
             db.execute("SELECT * FROM tickets WHERE project_path=? ORDER BY path, id", (project_path,))]
    ids = {n["id"] for n in nodes}
    kids: dict[str | None, list[dict]] = {}
    for n in nodes:
        kids.setdefault(n["parent"] if n["parent"] in ids and n["parent"] != n["id"] else None, []).append(n)
    out: list[dict] = []
    seen: set[str] = set()

    def walk(n):
        if n["id"] in seen:
            return
        seen.add(n["id"])
        out.append(n)
        for c in kids.get(n["id"], []):
            walk(c)

    for n in kids.get(None, []):
        walk(n)
    for n in nodes:          # parent cycles: nothing is lost
        walk(n)
    return out


def get_ticket(db: sqlite3.Connection, project_path: str, ticket_id: str, idle_min: float = 15.0, today=None,
               now: float | None = None) -> dict | None:
    r = db.execute("SELECT * FROM tickets WHERE project_path=? AND id=?", (project_path, ticket_id)).fetchone()
    if r is None:
        return None
    deps, blocks = _edges(db, project_path)
    d = _node(r, deps, blocks, body=True)
    d["project_path"] = project_path
    d["sections"] = None if d["body_dropped"] else tickets.body_sections(d["body"] or "")
    d["issues"] = [i for i in ticket_issues(db, project_path) if i["ticket_id"] == ticket_id]
    from myvec import sessions as sess

    bound = sess.by_ticket(db, project_path, idle_min=idle_min)
    d["sessions"] = bound.get(ticket_id, [])
    d["issues"] += [i for i in _health(ticket_tree(db, project_path), bound, today, now) if i["ticket_id"] == ticket_id]
    return d


def ticket_issues(db: sqlite3.Connection, project_path: str) -> list[dict]:
    return [{k: r[k] for k in ("severity", "code", "ticket_id", "path", "message")} for r in
            db.execute("SELECT * FROM ticket_issues WHERE project_path=? ORDER BY rowid", (project_path,))]


def tickets_by_file(db: sqlite3.Connection, abs_paths) -> dict[str, dict]:
    """abs_path -> {project, id, title, status, progress} для файлів, що є тікетами."""
    want = set(abs_paths)
    if not want:
        return {}
    out: dict[str, dict] = {}
    for r in db.execute("SELECT t.project_path, t.id, t.title, t.status, t.progress, t.path, p.root "
                        "FROM tickets t JOIN projects p ON p.path=t.project_path"):
        ap = r["root"].rstrip("/") + "/" + r["path"]
        if ap in want:
            out[ap] = {"project": r["project_path"], "id": r["id"], "title": r["title"], "status": r["status"],
                       "progress": r["progress"]}
    return out
