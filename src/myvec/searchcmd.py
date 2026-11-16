"""The `myvec search` command as a function: used by the CLI (direct mode) and by the daemon
(`POST /api/cli/search`), so both print exactly the same text.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from myvec import activity, config, crossproject, gitutil, lock, logutil
from myvec import db as dbmod

FRESH_NOTE = "(індекс оновлюється у фоні)"
SPAWN_THROTTLE_S = 20.0


@dataclass
class CliResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    results: int = 0
    ms: float = 0.0
    tokens: int = 0
    project: str | None = None   # where the search ran (context of cwd); used for the event bus / UI
    repo: str | None = None
    branch: str | None = None
    route: str | None = None
    fmt: str | None = None   # compact | expanded | json | summary (a flag for the search log, not text)

    def to_dict(self) -> dict:
        return {"stdout": self.stdout, "stderr": self.stderr, "exit_code": self.exit_code}


# ------------------------------------------------------------------ lazy freshness

def stale_repos(db, repos) -> list:
    """Repos whose cheap fingerprint (.git/HEAD text + .git/index mtime; pseudo-repo: top-level dir mtimes)
    differs from the one recorded at the last sync. A few stat calls per repo."""
    out = []
    for r in repos:
        d = r["dir"]
        if not Path(d).is_dir():
            continue
        if r["fingerprint"] is None:
            continue  # never fully synced with a fingerprint (old index): the next sync records it
        if gitutil.fingerprint(d, gitutil.is_repo(d)) != r["fingerprint"]:
            out.append(r)
    return out


def trigger_background_sync(project_path: str, root: str) -> None:
    """Ask the daemon to sync, else spawn a detached `myvec sync --quiet` (throttled, never concurrent)."""
    from myvec import daemon_client

    from urllib.parse import quote

    if daemon_client.request("POST", f"/api/projects/{quote(project_path)}/sync", {}) is not None:
        return
    stamp = config.logs_dir() / f".fresh-{hashlib.sha1(project_path.encode()).hexdigest()[:10]}"
    try:
        if time.time() - stamp.stat().st_mtime < SPAWN_THROTTLE_S:
            return
    except OSError:
        pass
    if lock.is_locked() or not Path(root).is_dir():
        return
    config.ensure_home()
    stamp.touch()
    out = logutil.open_append(config.logs_dir() / "sync.log")
    try:
        subprocess.Popen([sys.executable, "-m", "myvec", "sync", "--quiet"], cwd=root, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, start_new_session=True, close_fds=True)
    finally:
        out.close()


def check_freshness(db, repos, trigger: Callable[[str, str], None]) -> bool:
    """True if some repo in scope is stale (and a background sync was requested)."""
    stale = stale_repos(db, repos)
    if not stale:
        return False
    modes = {r["path"]: r for r in db.execute("SELECT path, root, sync_mode FROM projects")}
    seen: set[str] = set()
    requested = False
    for r in stale:
        pr = modes.get(r["project_path"])
        if pr is None or activity.mode_of(pr) == "off":
            continue
        if r["project_path"] in seen:
            continue
        seen.add(r["project_path"])
        try:
            trigger(r["project_path"], pr["root"])
        except Exception:  # noqa: BLE001
            pass
        requested = True
    return requested


# ------------------------------------------------------------------ query log (direct mode)

def log_query(rec: dict) -> None:
    from myvec import searchlog

    raw = str(rec.get("q") or "")
    logutil.append(config.logs_dir() / "queries.jsonl",
                   json.dumps({**rec, "q": searchlog.mask_q(raw)}, ensure_ascii=False), stamp=False)
    searchlog.record(searchlog.shape(raw, session=os.environ.get("CLAUDE_CODE_SESSION_ID"), ts=rec.get("ts"),
                                     **{k: v for k, v in rec.items() if k in ("via", "project", "repo", "scope", "kind",
                                                                             "mode", "route", "results", "tokens", "ms",
                                                                             "fmt")}))


# ------------------------------------------------------------------ the command

def run_search(query: str, cwd: str | Path, *, k: int | None = None, scope: str = "project", kind: str = "all",
               mode: str = "auto",
               full: bool = False, json_out: bool = False, debug: bool = False, via: str = "direct",
               db=None, trigger: Callable[[str, str], None] | None = None, db_lock=None,
               by_project: bool = False, project: str | None = None, compact: bool = False,
               total: bool = False) -> CliResult:
    """`compact`: the agent-facing one-line-per-hit text (ignored by `full`, `json_out` and `by_project`).
    `total`: add the identifier total line (the CLI asks for it; the UI's preview of the output does not have it).
    `db_lock`: a lock that guards a shared connection (the daemon's). It is held only around the database
    work - the query is embedded before it is taken, so a slow Ollama never blocks other requests."""
    from contextlib import nullcontext

    from myvec import ctx as ctxmod
    from myvec import search as S
    from myvec.embed import get_embedder

    trigger = trigger or trigger_background_sync  # resolved at call time (tests replace the module attribute)
    t0 = time.perf_counter()
    err: list[str] = []
    if scope not in ("repo", "project", "all") or kind not in S.KINDS \
            or mode not in ("auto", "vector", "hybrid"):
        return CliResult(stderr="error: --scope must be repo|project|all, --kind must be code|memory|docs|all, "
                                "--mode must be auto|vector|hybrid", exit_code=1)
    if by_project and project:
        return CliResult(stderr="error: --by-project and --project cannot be combined", exit_code=1)
    if by_project:
        scope = "all"
    if project:
        scope = "project"
    if not config.db_path().exists():
        return CliResult(stderr="error: no index yet. Run `myvec init` in a project (or `myvec remember --global ...`).",
                         exit_code=1)
    cfg = config.load_config()
    k = k or cfg.k
    own = db is None
    if own:
        try:
            db = dbmod.open_ready(cfg)
        except dbmod.ModelMismatch as e:
            return CliResult(stderr=f"error: {e}", exit_code=2)
    try:
        pre: list[str] = []   # embedding notes; printed after the "not inside a project" note, as before
        dbg = (lambda s: pre.append(f"[debug] {s}")) if debug else None
        qvec = S.embed_query(get_embedder(cfg), query, debug=dbg, warn=pre.append)
        with (db_lock if db_lock is not None else nullcontext()):
            c = ctxmod.current(db, cwd)
            home, xhidden, summary, idt = c, None, None, None
            if project:
                try:
                    c, xhidden = crossproject.target_ctx(db, project, cwd)
                except crossproject.CrossError as e:
                    return CliResult(stderr=f"error: {e}", exit_code=1)
            dbg = (lambda s: err.append(f"[debug] {s}")) if debug else None
            if c is None and scope != "all":
                err.append("note: not inside a myvec project; searching global memory only (use --scope all for everything)")
            err.extend(pre)
            if by_project:
                summary = crossproject.summarize(db, cfg, query, c, kind, qvec)
                hits = [t for p in summary["projects"] for t in p["top"]]
            else:
                hits = S.search(db, cfg, get_embedder(cfg), query, c, k=k, scope=scope, kind=kind, debug=dbg,
                                warn=err.append, mode=mode, qvec=qvec, hidden=xhidden)
            fresh = False
            if kind != "memory":
                frepos = S._scope_repos(db, c, scope)
                fhid = xhidden if xhidden is not None else (
                    crossproject.hidden_projects(db, c) if scope == "all" else set())
                if fhid:
                    frepos = [r for r in frepos if not crossproject.is_hidden(r["project_path"], fhid)]
                fresh = check_freshness(db, frepos, trigger)
            if total and not by_project and not json_out and kind != "memory":
                idt = S.ident_total(db, c, scope, kind, query, hidden=xhidden)
    finally:
        if own:
            db.close()
    c = home
    if c is not None:
        activity.touch(c.project_path)
    fmt = "summary" if summary is not None else "json" if json_out else \
        "compact" if compact and not full else "expanded"
    if summary is not None:
        out = crossproject.to_json(summary) if json_out else crossproject.format_summary(summary, cfg.output_budget_tokens)
        if fresh and json_out:
            err.append(FRESH_NOTE)
        elif fresh:
            out += "\n" + FRESH_NOTE
    elif json_out:
        out = json.dumps(S.to_json(hits), ensure_ascii=False, indent=2)
        if fresh:
            err.append(FRESH_NOTE)
    else:
        if fmt == "compact":
            out = S.format_compact(hits, cfg.output_budget_tokens, c.repo_path if c else None)
        else:
            out = S.format_hits(hits, cfg.output_budget_tokens, full=full, expand_top=cfg.expand_top,
                                expand_lines=cfg.expand_lines)
        out = out or "no results"
        if idt is not None:
            out += "\n" + S.format_ident_total(idt, query, str(cwd))
        if fresh:
            out += "\n" + FRESH_NOTE
    ms = (time.perf_counter() - t0) * 1000
    if debug:
        err.append(f"[debug] wall: {ms:.0f} ms")
    res = CliResult(stdout=out, stderr="\n".join(err), results=len(hits), ms=ms, tokens=len(out) // 4, fmt=fmt,
                    project=c.project_path if c else None, repo=c.repo_path if c else None,
                    branch=c.branch if c else None, route=S.effective_route(query, mode, qvec))
    if via == "direct":
        log_query({"ts": time.time(), "type": "search", "q": query, "scope": scope, "kind": kind, "cwd": str(cwd),
                   "ms": round(ms, 1), "results": res.results, "tokens": res.tokens, "via": "direct",
                   "project": res.project, "repo": res.repo, "branch": res.branch, "route": res.route,
                   "by_project": by_project, "target": project, "fmt": fmt})
    return res
