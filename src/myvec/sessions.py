"""Claude Code sessions (ADR-007): the hook journal `state/session_events.jsonl` is folded into the `sessions` table.

Hooks only append to the journal (stdlib, no DB). `fold()` (daemon tick, or `myvec sessions` without a daemon) claims
the journal atomically, replays the events in `ts` order through the pure state machine `apply()` and upserts.
Stored states: working | waiting | ended. "idle" (silent) is computed on read by `effective_state()`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from myvec import config, filters
from myvec import db as dbmod

WAITING_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog",
                         "agent_needs_input"}
COLUMNS = ("session_id", "state", "last_event", "last_event_at", "last_state_event_at", "last_seen", "started_at", "ended_at", "end_reason",
           "source", "cwd", "worktree", "branch", "project_path", "transcript_path", "permission_mode", "model",
           "title", "question", "question_at", "notification_type", "last_message", "ticket_id", "ticket_source",
           "updated_at", "question_options")


def ensure_table(db: sqlite3.Connection) -> None:
    db.executescript(dbmod.SESSIONS_SCHEMA)


def _mask(text) -> str | None:
    """Masked text; None when it holds a private key / known token (never stored)."""
    if not isinstance(text, str) or not text:
        return None
    if filters.has_hard_secret(text):
        return None
    return filters.mask_secrets(text)[0]


def _options_json(event: dict) -> str | None:
    """JSON of the AskUserQuestion widget shape ({n, multi, labels}); labels only for a single plain question."""
    n = event.get("qn")
    if not isinstance(n, int) or n < 1:
        return None
    labels = [_mask(x) for x in event["qo"]] if isinstance(event.get("qo"), list) and n == 1 else []
    return json.dumps({"n": n, "multi": bool(event.get("qm")), "labels": labels}, ensure_ascii=False)


def parse_options(raw) -> dict | None:
    """{n, multi, labels} from the stored JSON; None when absent or broken."""
    try:
        d = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("n"), int):
        return None
    labels = d.get("labels")
    return {"n": d["n"], "multi": bool(d.get("multi")),
            "labels": [x if isinstance(x, str) else None for x in labels] if isinstance(labels, list) else []}


def new_row(sid: str, ts: float) -> dict:
    row = dict.fromkeys(COLUMNS)
    row.update(session_id=sid, state="working", last_event_at=ts, last_seen=ts, started_at=ts)
    return row


def _set_state(row: dict, state: str) -> None:
    if row["state"] != state:
        row["state"] = state
        row["question"] = None
        row["question_at"] = None
        row["question_options"] = None
        row["notification_type"] = None


def apply(row: dict | None, event: dict) -> dict:
    """Pure state machine: returns the new row for `row` + one journal event."""
    ts = float(event.get("ts") or 0)
    ev = event.get("ev")
    stale = bool(row) and ts < float(row.get("last_state_event_at") or 0)   # replayed old event: no state rollback
    row = dict(row) if row else new_row(event["sid"], ts)
    row["last_seen"] = max(row["last_seen"] or 0, ts)
    row["last_event_at"] = max(row["last_event_at"] or 0, ts)
    row["updated_at"] = ts
    for col, key in (("cwd", "cwd"), ("worktree", "wt"), ("branch", "branch"), ("transcript_path", "tp"),
                     ("permission_mode", "pm"), ("model", "model"), ("title", "title")):
        if event.get(key):
            row[col] = event[key]
    if event.get("agent") or stale:   # subagent events say nothing about the main thread
        return row
    ended = row["state"] == "ended"
    if ev in ("start", "prompt", "answered", "stop", "ask", "notify", "end") and not (
            ev == "start" and event.get("src") == "compact"):
        row["last_state_event_at"] = max(row.get("last_state_event_at") or 0, ts)   # heartbeats never move it
    if ev == "tool":   # heartbeat: liveness only, never changes state (parallel tools must not hide a real wait)
        return row
    if ev == "start":
        if event.get("src") == "compact":
            return row
        if ended or row["last_event"] is not None:
            row["started_at"] = ts
        row["source"] = event.get("src")
        _set_state(row, "working")
    elif ev in ("prompt", "answered"):
        _set_state(row, "working")
    elif ev == "stop":
        _set_state(row, "working" if event.get("bg") else "waiting")
        if not event.get("bg"):
            row["last_message"] = _mask(event.get("msg"))
    elif ev == "ask":
        _set_state(row, "waiting")
        row["question"] = _mask(event.get("q"))
        row["question_at"] = ts
        row["question_options"] = _options_json(event)
    elif ev == "notify":
        nt = event.get("ntype")
        if nt is None or nt in WAITING_NOTIFICATIONS:
            _set_state(row, "waiting")
            row["notification_type"] = nt
        else:
            return row
    elif ev == "end":
        _set_state(row, "ended")
        row["ended_at"] = ts
        row["end_reason"] = event.get("reason")
        row["last_event"] = ev
        return row
    else:
        return row
    if ended:
        row["ended_at"] = None
        row["end_reason"] = None
    row["last_event"] = ev
    return row


def effective_state(row, now: float, idle_min: float) -> str:
    """working + silent for more than `idle_min` minutes -> idle. A waiting session never turns idle."""
    state = row["state"]
    if state == "working" and now - float(row["last_seen"] or 0) > idle_min * 60:
        return "idle"
    return state


def project_of(db: sqlite3.Connection, *paths: str | None) -> str | None:
    """Deepest registered project whose root contains one of the paths."""
    best: tuple[int, str] | None = None
    for p in paths:
        if not p:
            continue
        p = os.path.normpath(p)
        for r in db.execute("SELECT path, root FROM projects"):
            root = os.path.normpath(r[1])
            if (p == root or p.startswith(root.rstrip(os.sep) + os.sep)) and (best is None or len(root) > best[0]):
                best = (len(root), r[0])
        if best:
            return best[1]
    return None


def short_cwd(path: str | None) -> str | None:
    """cwd for display: ``~/...`` under HOME, as is otherwise; None when empty."""
    if not path:
        return None
    home = os.path.normpath(str(Path.home()))
    p = os.path.normpath(path)
    if home != os.sep and p == home:
        return "~"
    if home != os.sep and p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def main_root_of(worktree: str | None) -> str | None:
    """Root of the main checkout for a linked git worktree (`<wt>/.git` is a file `gitdir: …/.git/worktrees/<n>`).
    Reads files only (no git process); None for a normal checkout or on any error."""
    if not worktree:
        return None
    try:
        dot = Path(worktree) / ".git"
        if not dot.is_file():
            return None
        first = dot.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
        if not first.startswith("gitdir:"):
            return None
        gitdir = Path(first[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = Path(worktree) / gitdir
        gitdir = Path(os.path.realpath(gitdir))
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common_dir = Path(os.path.realpath(gitdir / common))
        return str(common_dir.parent) if common_dir.name == ".git" else None
    except (OSError, ValueError, IndexError):
        return None


def resolve_ticket(row, claim: dict | None, ids_by_project: dict, project_of_root):
    """Ticket of a session, computed on read (ADR-009): (ticket_project, ticket_id, source) or None.
    Order: claim > branch `t/<id>[-…]` (longest known id) > worktree `…/.worktrees/<id>`.
    `project_of_root(root) -> project | None` maps a claim's project_root."""
    project = row["project_path"]
    if claim and claim.get("ticket_id"):
        cp = project_of_root(claim.get("project_root")) if claim.get("project_root") else None
        return (cp or project, claim["ticket_id"], "claim")
    ids = ids_by_project.get(project) if project else None
    if not ids:
        return None
    branch = row["branch"] or ""
    if branch.startswith("t/"):
        rest = branch[2:]
        best = None
        for tid in ids:
            if (rest == tid or rest.startswith(tid + "-")) and (best is None or len(tid) > len(best)):
                best = tid
        if best:
            return (project, best, "branch")
    wt = os.path.normpath(row["worktree"]) if row["worktree"] else ""
    if wt and os.path.basename(os.path.dirname(wt)) == ".worktrees" and os.path.basename(wt) in ids:
        return (project, os.path.basename(wt), "worktree")
    return None


# ---------------------------------------------------------------- journal -> table

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def pending(src: Path | None = None) -> bool:
    """Is there anything to fold (journal or a leftover claimed file)? Cheap enough for the daemon tick."""
    src = src or config.session_events_path()
    if src.exists():
        return True
    try:
        return any(True for _ in src.parent.glob(src.name + ".folding.*"))
    except OSError:
        return False


def _claim(src: Path) -> list[Path]:
    """Atomically move the journal to a private name; also adopt leftovers of failed folds (own pid, or a dead one)."""
    claimed: list[Path] = []
    tmp = src.with_name(f"{src.name}.folding.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        os.replace(src, tmp)
        claimed.append(tmp)
    except OSError:
        pass
    try:
        for old in sorted(src.parent.glob(src.name + ".folding.*")):
            if old in claimed:
                continue
            parts = old.name.split(".folding.", 1)[1].split(".")
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid != os.getpid() and _pid_alive(pid):
                continue   # a live folder owns it
            mine = old.with_name(f"{src.name}.folding.{os.getpid()}.{uuid.uuid4().hex[:8]}")
            try:
                os.replace(old, mine)
                claimed.append(mine)
            except OSError:
                continue
    except OSError:
        pass
    return claimed


def _read_events(files: list[Path]) -> list[dict]:
    events: list[dict] = []
    for f in files:
        try:
            fh = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for ln in fh:   # streamed line by line
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("sid"), str) and isinstance(e.get("ts"), (int, float)):
                    events.append(e)
    events.sort(key=lambda e: e["ts"])   # stable: same-ts events keep journal order
    return events


def fold(db: sqlite3.Connection, now: float | None = None, retention_days: float = 30.0) -> list[dict]:
    """Apply the journal to `sessions`. Returns [{session_id, state, project}] for sessions that were created or
    changed state. A failing write leaves the claimed file in place (the next fold adopts it). Also deletes
    sessions ended more than `retention_days` ago."""
    now = time.time() if now is None else now
    ensure_table(db)
    claimed = _claim(config.session_events_path())
    changes: list[dict] = []
    if claimed:
        events = _read_events(claimed)
        db.execute("BEGIN IMMEDIATE")
        try:
            rows: dict[str, dict | None] = {}
            before: dict[str, str | None] = {}
            for e in events:
                sid = e["sid"]
                if sid not in rows:
                    r = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
                    rows[sid] = dict(r) if r else None
                    before[sid] = rows[sid]["state"] if rows[sid] else None
                rows[sid] = apply(rows[sid], e)
            marks = ",".join("?" * len(COLUMNS))
            for sid, row in rows.items():
                row["project_path"] = project_of(db, row["cwd"], row["worktree"], main_root_of(row["worktree"]))
                db.execute(f"INSERT OR REPLACE INTO sessions({','.join(COLUMNS)}) VALUES({marks})",
                           [row[c] for c in COLUMNS])
                if before[sid] != row["state"]:
                    changes.append({"session_id": sid, "state": row["state"], "project": row["project_path"]})
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        for f in claimed:
            try:
                f.unlink()
            except OSError:
                pass
    if retention_days > 0:
        db.execute("DELETE FROM sessions WHERE state='ended' AND coalesce(ended_at, last_seen, 0) < ?",
                   (now - retention_days * 86400,))
    return changes


def list_sessions(db: sqlite3.Connection, now: float | None = None, idle_min: float = 15.0, *,
                  include_old_ended: bool = False, project: str | None = None,
                  ticket: tuple[str, str] | None = None) -> list[dict]:
    """Sessions with the computed state and ticket binding, newest activity first. Ended ones older than a day are
    hidden by default. `ticket=(project, id)` keeps only sessions bound to that ticket."""
    from myvec import ticketops

    now = time.time() if now is None else now
    ensure_table(db)
    claims = ticketops.read_claims()
    info: dict[str, dict[str, tuple]] = {}
    try:
        for t in db.execute("SELECT project_path, id, title, status FROM tickets"):
            info.setdefault(t[0], {})[t[1]] = (t[2], t[3])
    except sqlite3.Error:   # no tickets table yet
        pass
    ids_by_project = {p: set(v) for p, v in info.items()}
    root_cache: dict = {}

    def project_of_root(root):
        if root not in root_cache:
            root_cache[root] = project_of(db, root, main_root_of(root))
        return root_cache[root]

    out = []
    sql = "SELECT * FROM sessions" + (" WHERE project_path=?" if project else "") + " ORDER BY last_seen DESC"
    for r in db.execute(sql, (project,) if project else ()):
        d = dict(r)
        if d["state"] == "ended" and not include_old_ended and (d["ended_at"] or d["last_seen"] or 0) < now - 86400:
            continue
        d["question_options"] = parse_options(d.get("question_options"))
        d["effective_state"] = effective_state(d, now, idle_min)
        d["age_s"] = max(0.0, now - float(d["last_seen"] or now))
        if not d["project_path"]:   # stored before the project was registered / the worktree mapping existed
            d["project_path"] = project_of(db, d["cwd"], d["worktree"], main_root_of(d["worktree"]))
        res = resolve_ticket(d, claims.get(d["session_id"]), ids_by_project, project_of_root)
        tp, tid, src = res if res else (None, None, None)
        title, status = info.get(tp, {}).get(tid, (None, None)) if res else (None, None)
        d["project_label"] = d["project_path"] or short_cwd(d["cwd"] or d["worktree"])
        d.update(ticket_project=tp, ticket_id=tid, ticket_source=src, ticket_title=title, ticket_status=status)
        if ticket and (tp, tid) != tuple(ticket):
            continue
        out.append(d)
    return out


def wait_s(d: dict, now: float | None = None) -> float | None:
    """Seconds a waiting session has waited (since its last state event); None when it is not waiting."""
    if d.get("effective_state") != "waiting":
        return None
    now = time.time() if now is None else now
    return max(0.0, now - float(d.get("last_state_event_at") or d.get("last_seen") or now))


def by_ticket(db: sqlite3.Connection, project_path: str, now: float | None = None, idle_min: float = 15.0) -> dict:
    """{ticket_id: [session dicts]} for tickets of `project_path`, freshest first."""
    out: dict[str, list[dict]] = {}
    for d in list_sessions(db, now, idle_min):
        if d["ticket_project"] != project_path or not d["ticket_id"]:
            continue
        out.setdefault(d["ticket_id"], []).append({
            "session_id": d["session_id"], "effective_state": d["effective_state"],
            "ticket_source": d["ticket_source"], "branch": d["branch"], "last_seen": d["last_seen"],
            "age_s": d["age_s"], "question": d["question"], "question_options": d["question_options"], "state": d["state"], "cwd": d["cwd"],
            "wait_s": wait_s(d, now)})
    return out
