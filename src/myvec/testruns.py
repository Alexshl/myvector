"""Read-only access to `test_runs` (ADR-011, ADR-015): the API behind the «Тести» tab and the «Навантаження» page.

Only a whitelist of columns leaves this module (no `cwd`, `session_id`, `pid`). The command is already masked at write
time (ADR-011); it is returned as stored, including the `[команда приховано]` placeholder.
"""
from __future__ import annotations

import socket
import sqlite3
import statistics
import time

from myvec import filters

FIELDS = (
    "id", "run_uid", "source", "state", "host", "project_path", "branch", "ticket_id", "agent", "label", "command",
    "started_at", "ended_at", "wall_s", "cpu_s", "avg_cores", "peak_cores", "peak_rss_bytes", "exit_code", "signal",
    "interrupted",
)
HIDDEN = "[команда приховано]"
DEFAULT_RULE = {"pct": 25.0, "runs": 10, "min_runs": 3, "min_abs_s": 2.0}
_SELECT = "SELECT " + ", ".join(FIELDS) + " FROM test_runs"


def outcome(row: dict, host: str | None = None) -> str:
    """running | passed | failed | interrupted | ended | lost. A `running` row of another host cannot be confirmed: lost."""
    state = row.get("state")
    if state == "running":
        return "lost" if host is not None and row.get("host") not in (None, host) else "running"
    if state == "lost":
        return "lost"
    if row.get("interrupted"):
        return "interrupted"
    if row.get("source") == "passive" and row.get("exit_code") is None and not row.get("signal"):
        return "ended"   # a passive run has no exit code: do not claim «passed»
    if row.get("signal") or row.get("exit_code") not in (None, 0):
        return "failed"
    return "passed"


def _clean(text):
    """Re-mask on output: passive runs (E6-03) may store raw command lines."""
    if not text:
        return text
    return HIDDEN if filters.has_hard_secret(text) else filters.mask_secrets(text)[0]


def _shape(r: sqlite3.Row, host: str | None = None) -> dict:
    d = {k: r[k] for k in FIELDS}
    d["interrupted"] = bool(d["interrupted"])
    for k in ("command", "label", "agent"):
        d[k] = _clean(d[k])
    d["outcome"] = outcome(d, host or socket.gethostname())
    d["regression"] = None            # filled by _mark_regressions (ADR-018)
    return d


def _query(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise


def _rule(rule: dict | None) -> dict:
    return {**DEFAULT_RULE, **(rule or {})}


def _key(r: dict) -> tuple | None:
    """Baseline key (ADR-018): wrapper runs by label, passive ones (label is only the kind) by command."""
    if not r.get("project_path") or not r.get("source"):
        return None
    by_label = r["source"] == "wrapper" and bool(r.get("label"))
    name = r.get("label") if by_label else r.get("command")
    return (r["project_path"], r["source"], "label" if by_label else "command", name) if name else None


def _verdict(value, base: list[float], rule: dict) -> float | None:
    """Excess over the median in % (rounded) when `value` is `pct`% and `min_abs_s` seconds above it, else None."""
    xs = [x for x in base if x]
    if value is None or len(xs) < rule["min_runs"]:
        return None
    med = statistics.median(xs)
    if med > 0 and value - med >= rule["min_abs_s"] and value >= med * (1 + rule["pct"] / 100):
        return round((value / med - 1) * 100)
    return None


def _mark_regressions(db: sqlite3.Connection, rows: list[dict], rule: dict | None, now: float | None = None) -> None:
    """Set `regression` on finished (`passed`/`ended`) and `running` rows against the median of the previous runs of
    the same key; one query per key of the page. `failed`/`interrupted`/`lost` are neither baseline nor marked."""
    rule = _rule(rule)
    now = time.time() if now is None else now
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        k = _key(r)
        if k is not None and r["outcome"] in ("passed", "ended", "running"):
            groups.setdefault(k, []).append(r)
    for (project, source, field_, name), grp in groups.items():
        ok = "passed" if source == "wrapper" else "ended"
        cond = ("state='done' AND COALESCE(interrupted,0)=0 AND COALESCE(signal,0)=0 AND "
                + ("exit_code=0" if ok == "passed" else "exit_code IS NULL"))
        if source == "wrapper" and field_ == "command":
            cond += " AND COALESCE(label,'')=''"
        base = _query(db, f"SELECT started_at, wall_s, cpu_s FROM test_runs WHERE project_path=? AND source=? AND "
                          f"{field_}=? AND {cond} AND started_at<? ORDER BY started_at DESC LIMIT ?",
                      (project, source, name, max(g["started_at"] for g in grp), len(grp) + rule["runs"]))
        for r in grp:
            prev = [b for b in base if b["started_at"] < r["started_at"]][:rule["runs"]]
            if r["outcome"] == "running":
                wall, cpu = now - r["started_at"], None
            else:
                wall, cpu = r["wall_s"], r["cpu_s"]
            w = _verdict(wall, [b["wall_s"] for b in prev if b["wall_s"] is not None], rule)
            c = _verdict(cpu, [b["cpu_s"] for b in prev if b["cpu_s"] is not None], rule)
            if w is None and c is None:
                continue
            mw = [b["wall_s"] for b in prev if b["wall_s"]]
            mc = [b["cpu_s"] for b in prev if b["cpu_s"]]
            r["regression"] = {"wall_pct": w, "cpu_pct": c, "n": len(mw),
                               "median_wall_s": statistics.median(mw) if mw else None,
                               "median_cpu_s": statistics.median(mc) if mc else None}


def list_runs(db: sqlite3.Connection, project: str | None = None, limit: int = 50, before: float | None = None,
              rule: dict | None = None) -> list[dict]:
    where, args = [], []
    if project is not None:
        where.append("project_path=?")
        args.append(project)
    if before is not None:
        where.append("started_at<?")
        args.append(before)
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY started_at DESC, id DESC LIMIT ?"
    host = socket.gethostname()
    out = [_shape(r, host) for r in _query(db, sql, (*args, int(limit)))]
    _mark_regressions(db, out, rule)
    return out


def running(db: sqlite3.Connection, rule: dict | None = None, regressions: bool = True) -> list[dict]:
    """Runs in state `running` on this host (the daemon marks stale ones `lost` itself)."""
    host = socket.gethostname()
    out = [_shape(r, host) for r in _query(db, _SELECT + " WHERE state='running' AND host=? ORDER BY started_at DESC, id DESC", (host,))]
    if regressions:
        _mark_regressions(db, out, rule)
    return out


def recent_finished(db: sqlite3.Connection, since: float, limit: int = 5, rule: dict | None = None) -> list[dict]:
    # started_at bound keeps the scan small (a run lasting over a day is not "recent"); ended_at decides membership
    sql = (_SELECT + " WHERE state!='running' AND started_at>=? AND COALESCE(ended_at, started_at)>=? "
           "ORDER BY started_at DESC, id DESC LIMIT ?")
    host = socket.gethostname()
    out = [_shape(r, host) for r in _query(db, sql, (since - 86400, since, int(limit)))]
    _mark_regressions(db, out, rule)
    return out


def summary(db: sqlite3.Connection, project: str) -> dict:
    rows = _query(db, "SELECT COUNT(*), MIN(started_at), MAX(started_at), "
                      "COALESCE(SUM(source='wrapper'),0), COALESCE(SUM(source='passive'),0) "
                      "FROM test_runs WHERE project_path=?", (project,))
    n, first, last, wrapper, passive = rows[0] if rows else (0, None, None, 0, 0)
    ok = _query(db, "SELECT wall_s, cpu_s FROM test_runs WHERE project_path=? AND state='done' AND COALESCE(interrupted,0)=0 "
                    "AND COALESCE(signal,0)=0 AND exit_code=0 ORDER BY started_at DESC LIMIT 10", (project,))

    def med(i: int):
        xs = [r[i] for r in ok if r[i] is not None]
        return statistics.median(xs) if xs else None
    return {"n": n, "first_at": first, "last_at": last, "n_wrapper": wrapper, "n_passive": passive,
            "median_wall_s": med(0), "median_cpu_s": med(1)}
