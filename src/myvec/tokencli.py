"""`myvec tokens`: token usage per agent / model / effort / ticket from Claude Code transcripts (ADR-013)."""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Annotated, Optional

import typer

from myvec import config
from myvec import tokens as T
from myvec.tokens import compact

tokens_app = typer.Typer(help="Token usage from Claude Code transcripts.", add_completion=False)

COLS = [("requests", "запитів"), ("input", "input"), ("output", "output"), ("cache_read", "cache read"),
        ("cache_write", "cache write"), ("total", "усього")]


def _fail(msg: str, code: int = 1):
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _maybe_scan(no_scan: bool) -> None:
    if not no_scan and config.load_config().tokens_enabled:
        tdb = T.open_db()
        try:
            st = T.scan(tdb, config.claude_projects_root())
        finally:
            tdb.close()
        if st.locked:
            typer.echo("scan skipped: another scan is running (the daemon)", err=True)


def _open_index() -> sqlite3.Connection | None:
    if not config.db_path().exists():
        return None
    from myvec import db as dbmod

    return dbmod.connect(vec=False, readonly=True)


def _project_arg(idx, value: str | None) -> str | None:
    from myvec import sessions

    cwd = os.getcwd()
    if value is None:
        if idx is None:
            return None
        return sessions.project_of(idx, cwd, sessions.main_root_of(cwd))
    if value == ".":
        p = sessions.project_of(idx, cwd, sessions.main_root_of(cwd)) if idx is not None else None
        return p or cwd
    return os.path.abspath(os.path.expanduser(value))


def render(rows: list[dict], keys: list[str]) -> str:
    total = {c: sum(r[c] for r in rows) for c, _ in COLS}
    tot = T._finish({**T._sum_row()})
    tot.update({c: total[c] for c in ("requests", "input", "output", "cache_read", "cache_write")})
    T._finish(tot)
    heads = keys + [h for _, h in COLS] + ["кеш"]
    body = [[T.agent_label(r[k]) if k == "agent" else str(r[k]) for k in keys] + [compact(r[c]) for c, _ in COLS] + [f"{r['cache_share'] * 100:.0f}%"]
            for r in rows]
    body.append(["усього"] + [""] * (len(keys) - 1) + [compact(tot[c]) for c, _ in COLS]
                + [f"{tot['cache_share'] * 100:.0f}%"])
    widths = [max(len(x[i]) for x in [heads] + body) for i in range(len(heads))]
    lines = []
    for n, row in enumerate([heads] + body):
        lines.append("  ".join(c.ljust(w) if i < len(keys) else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths))))
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


@tokens_app.callback(invoke_without_command=True)
def tokens_cmd(
    project: Annotated[Optional[str], typer.Option("--project", help="Project path or '.' (default: current "
                                                    "project, else all).")] = None,
    ticket: Annotated[Optional[str], typer.Option("--ticket", help="Only this ticket id.")] = None,
    session: Annotated[Optional[str], typer.Option("--session", help="Only this session id.")] = None,
    since: Annotated[Optional[str], typer.Option("--since", help="7d, 24h or YYYY-MM-DD.")] = None,
    by: Annotated[str, typer.Option("--by", help="Comma list of agent,model,effort,ticket,project,session,day.")] = "agent",
    as_json: Annotated[bool, typer.Option("--json", help="Raw numbers as JSON.")] = False,
    no_scan: Annotated[bool, typer.Option("--no-scan", help="Do not read new transcript lines first.")] = False,
    summary: Annotated[bool, typer.Option("--summary", help="With --ticket: one line for the ticket Log.")] = False,
    context: Annotated[bool, typer.Option("--context", help="With --session: context size of its last request "
                                          "(main transcript only; approximate, scanned every ~60 s).")] = False,
):
    if context:
        if not session:
            _fail("--context needs --session", 2)
        if project or ticket or since or summary or by != "agent":
            _fail("--context works only with --session (and --json, --no-scan)", 2)
        _maybe_scan(no_scan)
        tdb = T.open_db()
        try:
            d = T.session_context(tdb, session)
        finally:
            tdb.close()
        if d is None:
            typer.echo(json.dumps({"session": session, "context": None}) if as_json else "no usage found")
            raise typer.Exit(1)
        typer.echo(json.dumps(d, ensure_ascii=False, indent=1) if as_json else f"{d['context']} ({compact(d['context'])})")
        return
    if summary and not ticket:
        _fail("--summary needs --ticket", 2)
    keys = [k.strip() for k in by.split(",") if k.strip()]
    if not keys or "file" in keys:  # `file` is an internal grouping key (distinct runs), not a CLI option
        _fail("--by needs at least one of agent,model,effort,ticket,project,session,day")
    try:
        since_ts = T.parse_since(since)
    except ValueError as e:
        _fail(str(e))
    _maybe_scan(no_scan)
    idx = _open_index()
    try:
        proj = _project_arg(idx, project)
        if summary:
            d = T.ticket_summary(idx, proj, ticket)
            if d is None:
                typer.echo("no usage found")
                raise typer.Exit(1)
            typer.echo(json.dumps(d, ensure_ascii=False, indent=1) if as_json else T.summary_line(d))
            return
        try:
            rows = T.usage(idx, project=proj, ticket=ticket, session=session, since=since_ts, by=keys)
        except ValueError as e:
            _fail(str(e))
    finally:
        if idx is not None:
            idx.close()
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if not rows:
        typer.echo("no usage found")
        return
    shown = [k for k in dict.fromkeys(keys + (["project"] if "ticket" in keys and "project" not in keys
                                              and proj is None else []))]
    typer.echo(render(rows, shown))
