"""myvec versus self-search per project (ADR-029), computed at read time.

Tool calls come from `tokens.db.tool_calls` (categories only, from the transcripts: the same source for the main
session and sub-agents); latency, result counts and channels come from `logs/searches.jsonl`. Nothing is written.
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
import time
from datetime import datetime
from bisect import bisect_left

from myvec import config, searchlog
from myvec import tokens as T
from myvec.searchkind import KINDS

MISS_WINDOW = 10        # a `self` search within this many tool calls after `myvec search` counts as a miss
AGENT_WINDOW_S = 5.0    # log record -> nearest `myvec` tool call of the same session
FOREIGN_PREFIX = "/home/" if sys.platform == "darwin" else "/Users/"   # cwd of the other OS (transcripts copied over)


def _blank(project: str) -> dict:
    return {"project": project, "sessions": 0, "sessions_myvec": 0, "myvec": 0, "recall": 0, "self": 0,
            "self_sub": 0, "myvec_sub": 0, "share": None, "miss": 0,
            "logged": 0, "zero_results": 0, "fts_fallback": 0, "routes": {}, "p50_ms": None, "via": {}, "agents": {},
            "pre_onset": 0, "onset": None, "sessions_explore": 0, "sessions_myvec_explore": 0, "p50_ms_24h": None,
            "self_by_kind": {**{k: 0 for k in KINDS}, "unclassified": 0}, "classified": None, "share_explore": None,
            "doc_reads": 0, "self_doc": 0, "myvec_docs": 0}


def _derive(d: dict) -> None:
    """Shares from the counters of one row. `share_explore` = myvec / (myvec + explore + exact + unclassified): the
    unclassified calls stay in the denominator (a lower bound); `count`, `file`, `logs`, `name` are not exploration."""
    k = d["self_by_kind"]
    d["classified"] = (d["self"] - k["unclassified"]) / d["self"] if d["self"] else None
    den = d["myvec"] + k["explore"] + k["exact"] + k["unclassified"]
    d["share_explore"] = d["myvec"] / den if den else None


def blank_row(project: str) -> dict:
    """Zero row of a registered project that had no calls in the window."""
    return _blank(project)


def _is_foreign(cwd: str | None) -> bool:
    return bool(cwd) and cwd.startswith(FOREIGN_PREFIX)


def _registered_epoch(idx: sqlite3.Connection | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if idx is None:
        return out
    try:
        for r in idx.execute("SELECT path, registered_at FROM projects WHERE registered_at IS NOT NULL"):
            try:
                out[r[0]] = datetime.fromisoformat(r[1]).timestamp()
            except (TypeError, ValueError):
                pass
    except sqlite3.Error:
        pass
    return out


def _onsets(idx, tdb: sqlite3.Connection | None, att) -> dict[str, float]:
    """Per project: min(registered_at, first `myvec`/`recall` call over all time). Foreign-OS cwd is ignored."""
    onset = _registered_epoch(idx)
    if tdb is None:
        return onset
    try:
        firsts = tdb.execute("SELECT cwd, MIN(ts) FROM tool_calls WHERE cat IN ('myvec','recall') AND ts IS NOT NULL "
                             "GROUP BY cwd").fetchall()
    except sqlite3.Error:
        return onset
    for cwd, ts in firsts:
        if _is_foreign(cwd):
            continue
        p = att.project_of_cwd(cwd) or T.NO_PROJECT
        if p not in onset or ts < onset[p]:
            onset[p] = ts
    return onset


def _tool_rows(tdb: sqlite3.Connection | None, since: float, until: float) -> list[sqlite3.Row]:
    if tdb is None:
        return []
    try:
        have = {r[1] for r in tdb.execute("PRAGMA table_info(tool_calls)")}
        kd = "c.kind, c.doc" if {"kind", "doc"} <= have else "NULL AS kind, NULL AS doc"
        return tdb.execute(
            f"SELECT c.file_id, c.seq, c.ts, c.cat, c.cwd, {kd}, f.session_id, f.agent_id, f.agent_type "
            "FROM tool_calls c JOIN token_files f ON f.file_id=c.file_id WHERE c.ts >= ? AND c.ts < ? "
            "ORDER BY c.file_id, c.seq", (since, until)).fetchall()
    except sqlite3.Error:
        return []


def _misses(tdb: sqlite3.Connection | None, rows: list[sqlite3.Row]) -> set[tuple[int, int]]:
    """(file_id, seq) of the `myvec` calls followed by a `self` call within MISS_WINDOW tool calls of the same file."""
    out: set[tuple[int, int]] = set()
    if tdb is None:
        return out
    files = {r["file_id"] for r in rows if r["cat"] == "myvec"}
    for fid in files:
        try:
            calls = tdb.execute("SELECT seq, cat FROM tool_calls WHERE file_id=? ORDER BY seq", (fid,)).fetchall()
        except sqlite3.Error:
            continue
        selfs = [c["seq"] for c in calls if c["cat"] == "self"]
        for c in calls:
            if c["cat"] != "myvec":
                continue
            i = bisect_left(selfs, c["seq"] + 1)
            if i < len(selfs) and selfs[i] <= c["seq"] + MISS_WINDOW:
                out.add((fid, c["seq"]))
    return out


def _agent_for(rec: dict, by_session: dict[str, list[tuple[float, str]]]) -> str:
    sid, ts = rec.get("session"), rec.get("ts")
    cand = by_session.get(sid) if sid else None
    if not cand or not isinstance(ts, (int, float)):
        return "unknown"
    best = min(cand, key=lambda x: abs(x[0] - ts))
    return best[1] if abs(best[0] - ts) <= AGENT_WINDOW_S else "unknown"


def usage(idx_ro: sqlite3.Connection | None, tdb_ro: sqlite3.Connection | None, days: int,
          now: float | None = None) -> dict:
    now = time.time() if now is None else now
    since = now - days * 86400
    until = now + 1
    att = T._Attributor(idx_ro)
    projects: dict[str, dict] = {}
    sess: dict[str, set] = {}
    sess_my: dict[str, set] = {}

    def row(p: str) -> dict:
        return projects.setdefault(p, _blank(p))

    onset = _onsets(idx_ro, tdb_ro, att)
    real = [v for k, v in onset.items() if k != T.NO_PROJECT]
    if real and T.NO_PROJECT not in onset:
        onset[T.NO_PROJECT] = min(real)
    elif real:
        onset[T.NO_PROJECT] = min(onset[T.NO_PROJECT], min(real))
    foreign = 0
    sess_self: dict[str, set] = {}
    rows = _tool_rows(tdb_ro, since, until)
    miss = _misses(tdb_ro, rows)
    by_session: dict[str, list[tuple[float, str]]] = {}
    for r in rows:
        cat, sub = r["cat"], r["agent_id"] is not None
        if cat not in ("myvec", "recall", "self", "read"):
            continue
        if _is_foreign(r["cwd"]):
            foreign += 1
            continue
        p = att.project_of_cwd(r["cwd"]) or T.NO_PROJECT
        d = row(p)
        if p in onset and r["ts"] is not None and r["ts"] < onset[p]:
            d["pre_onset"] += 1
            continue
        if cat == "read":
            d["doc_reads"] += 1
            continue
        d[cat] += 1
        if cat == "self":
            k = r["kind"] if r["kind"] in KINDS else "unclassified"
            d["self_by_kind"][k] += 1
            if r["doc"] == 1:
                d["self_doc"] += 1
        if cat == "self" and sub:
            d["self_sub"] += 1
        if cat == "myvec":
            sess_my.setdefault(p, set()).add(r["session_id"])
            if sub:
                d["myvec_sub"] += 1
            if (r["file_id"], r["seq"]) in miss:
                d["miss"] += 1
            if r["ts"] is not None:
                by_session.setdefault(r["session_id"], []).append(
                    (r["ts"], r["agent_type"] or ("unknown" if sub else "main")))
        if cat in ("myvec", "self"):
            sess.setdefault(p, set()).add(r["session_id"])
        if cat == "self":
            sess_self.setdefault(p, set()).add(r["session_id"])

    lat: dict[str, list[float]] = {}
    lat24: dict[str, list[float]] = {}
    for rec in searchlog.read(since, until):
        d = row(rec.get("project") or T.NO_PROJECT)
        d["logged"] += 1
        if rec.get("kind") == "docs":
            d["myvec_docs"] += 1
        if rec.get("results") == 0:
            d["zero_results"] += 1
        if isinstance(rec.get("ms"), (int, float)):
            lat.setdefault(d["project"], []).append(float(rec["ms"]))
            if isinstance(rec.get("ts"), (int, float)) and rec["ts"] >= now - 86400:
                lat24.setdefault(d["project"], []).append(float(rec["ms"]))
        via = rec.get("via") or "?"
        d["via"][via] = d["via"].get(via, 0) + 1
        route = rec.get("route")
        route = route if isinstance(route, str) and route else "?"
        d["routes"][route] = d["routes"].get(route, 0) + 1
        if route == "fts-fallback":
            d["fts_fallback"] += 1
        if rec.get("session"):
            a = _agent_for(rec, by_session)
            d["agents"][a] = d["agents"].get(a, 0) + 1

    for p, d in projects.items():
        d["sessions"] = len(sess.get(p, ()))
        d["sessions_myvec"] = len(sess_my.get(p, ()))
        tot = d["myvec"] + d["self"]
        d["share"] = d["myvec"] / tot if tot else None
        _derive(d)
        d["sessions_explore"] = len(sess_self.get(p, ()))
        d["sessions_myvec_explore"] = len(sess_self.get(p, set()) & sess_my.get(p, set()))
        d["onset"] = onset.get(p)
        if lat.get(p):
            d["p50_ms"] = round(statistics.median(lat[p]), 1)
        if lat24.get(p):
            d["p50_ms_24h"] = round(statistics.median(lat24[p]), 1)
    out = sorted(projects.values(), key=lambda d: (-(d["myvec"] + d["self"]), -d["logged"], d["project"]))

    total = _blank("total")
    for d in out:
        for k in ("sessions", "sessions_myvec", "myvec", "recall", "self", "self_sub", "myvec_sub", "miss", "logged",
                  "zero_results", "fts_fallback", "pre_onset", "sessions_explore", "sessions_myvec_explore",
                  "doc_reads", "self_doc", "myvec_docs"):
            total[k] += d[k]
        for k, n in d["self_by_kind"].items():
            total["self_by_kind"][k] += n
        for k in ("via", "agents", "routes"):
            for a, n in d[k].items():
                total[k][a] = total[k].get(a, 0) + n
    tt = total["myvec"] + total["self"]
    total["share"] = total["myvec"] / tt if tt else None
    _derive(total)
    all_lat = [x for v in lat.values() for x in v]
    total["p50_ms"] = round(statistics.median(all_lat), 1) if all_lat else None
    all24 = [x for v in lat24.values() for x in v]
    total["p50_ms_24h"] = round(statistics.median(all24), 1) if all24 else None
    total["foreign"] = foreign
    total["onset"] = min((d["onset"] for d in out if d["onset"] is not None), default=None)
    return {"days": days, "since": since, "projects": out, "total": total, "collector": collector(tdb_ro)}


def collector(tdb: sqlite3.Connection | None) -> dict:
    enabled = bool(config.load_config().tokens_enabled)
    pending, last = 0, None
    if tdb is not None:
        try:
            st = T.stats(tdb)
            pending, last = st["pending_bytes"], st["last_scan"]
        except sqlite3.Error:
            pass
    return {"enabled": enabled, "pending_bytes": pending, "last_scan": last, "backfill": pending > 0}
