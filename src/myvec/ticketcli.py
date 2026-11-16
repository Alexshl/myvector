"""`myvec ticket ...`: перевірка, індекс, створення, Log і claim тікетів (ADR-004)."""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from myvec import ticketops as ops
from myvec import tickets as T

ticket_app = typer.Typer(help="Tickets: check, index, new, log, claim (canon v1, docs/todo).",
                         no_args_is_help=True, add_completion=False)

RootOpt = Annotated[Optional[Path], typer.Option("--root", help="Project root (default: git toplevel of cwd).")]
DirOpt = Annotated[str, typer.Option("--dir", help="Tickets dir relative to root.")]


def _fail(msg: str, code: int = 1):
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _root(root: Path | None) -> Path:
    if root is not None:
        return root.resolve()
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def _load(root: Path, dir_: str) -> T.TicketSet:
    if not (root / dir_).is_dir():
        _fail(f"{root / dir_} is not a directory")
    return T.load_dir(root, dir_)


def _print_issues(issues, err: bool = False) -> None:
    for i in issues:
        tag = {"error": "ERROR", "health": "HEALTH"}.get(i.severity, "WARN")
        typer.echo(f"{tag} {i.path}: {i.message} [{i.code}]", err=err)


def _summary(lang: str, tickets: int, groups: int, errs: int, warns: int, tips: int) -> str:
    """Підсумок `ticket check` мовою тікетів проєкту (E5-07); uk - за замовчуванням, текст незмінний."""
    if lang == "en":
        return (f"{tickets} tickets, {groups} groups, {errs} errors, {warns} warnings, "
                f"{tips} {'tip' if tips == 1 else 'tips'}")
    if lang == "ru":
        return (f"{tickets} тикетов, {groups} групп, {errs} ошибок, {warns} предупреждений, "
                f"{tips} {_uk_plural(tips, 'совет', 'совета', 'советов')}")
    return (f"{tickets} тікетів, {groups} груп, {errs} помилок, {warns} попереджень, "
            f"{tips} {_uk_plural(tips, 'порада', 'поради', 'порад')}")


def _do_check(ts: T.TicketSet, strict: bool = False, lang: str = "uk") -> bool:
    issues = T.check(ts)
    issues += T.health(ts.tickets.values(), datetime.date.today(), lang=lang)
    _print_issues(issues)
    errs = sum(i.severity == "error" for i in issues)
    tips = sum(i.severity == "health" for i in issues)
    warns = len(issues) - errs - tips
    groups = sum(t.kind == "group" for t in ts.tickets.values())
    typer.echo(_summary(lang, len(ts.tickets) - groups, groups, errs, warns, tips))
    return not errs and not (strict and warns)


@ticket_app.command()
def check(root: RootOpt = None, dir: DirOpt = "docs/todo",
          strict: Annotated[bool, typer.Option("--strict", help="Warnings fail too.")] = False):
    """Validate the tickets tree."""
    r = _root(root)
    if not _do_check(_load(r, dir), strict, ops.lang_for(r / dir)):
        raise typer.Exit(1)


@ticket_app.command()
def index(root: RootOpt = None, dir: DirOpt = "docs/todo",
          index_name: Annotated[str, typer.Option("--index-name", help="File name of the generated index.")] = "INDEX.md"):
    """Regenerate INDEX.md (tree) and group tables (only where markers exist)."""
    r = _root(root)
    ts = _load(r, dir)
    if not _do_check(ts, lang=ops.lang_for(r / dir)):
        typer.echo("index not written: fix the errors above", err=True)
        raise typer.Exit(1)
    base = r / dir
    if (not index_name.endswith(".md") or index_name != Path(index_name).name or index_name.startswith(".")):
        _fail("--index-name must be a plain file name ending in .md", 2)
    clash = sorted(e.name for e in base.iterdir() if e.name.lower() == index_name.lower() and e.name != index_name)
    if clash:
        _fail(f"{dir}/{clash[0]} collides with the generated {index_name} on a case-insensitive filesystem "
              f"(macOS): nothing written. Rename {clash[0]} or pass --index-name", 2)
    idx = base / index_name
    lang = ops.lang_for(base)
    n = 0
    try:
        if not idx.exists():
            ops.write_atomic(idx, ops._L[lang]["index_head"])
        ops.replace_generated(idx, ops.render_index(ts, dir, lang))
        for t in ts.tickets.values():
            if t.kind != "group":
                continue
            p = r / t.path
            if p.is_file() and ops.has_markers(ops.read_text(p)):
                ops.replace_generated(p, ops.render_group_table(ts, t, lang))
                n += 1
    except (ops.TicketOpError, OSError) as e:
        _bad(e)
    typer.echo(f"index written: {len(ts.tickets)} tickets, {n} group tables")


def _bad(e: Exception):
    _fail(str(e))


@ticket_app.command()
def new(title: Annotated[str, typer.Option("--title")], slug: Annotated[str, typer.Option("--slug")],
        parent: Annotated[Optional[str], typer.Option("--parent")] = None,
        kind: Annotated[str, typer.Option("--kind", help="ticket or group")] = "ticket",
        id: Annotated[Optional[str], typer.Option("--id")] = None,
        size: Annotated[str, typer.Option("--size")] = "S",
        deps: Annotated[str, typer.Option("--deps", help="Comma-separated ids.")] = "",
        root: RootOpt = None, dir: DirOpt = "docs/todo"):
    """Create a ticket (or a group with --kind group --id)."""
    r = _root(root)
    ts = _load(r, dir)
    base = r / dir
    if not ops.SLUG.match(slug):
        _fail("slug must match [a-z0-9-]+ (no leading/trailing/double dashes)")
    if kind not in T.KINDS:
        _fail("kind must be ticket or group")
    if size.upper() not in T.SIZES:
        _fail("size must be S, M or L")
    dep_ids = [d.strip() for d in deps.split(",") if d.strip()]
    for d in dep_ids:
        if d not in ts.tickets:
            _fail(f"dependency {d} does not exist")
    par = None
    if parent:
        par = ts.tickets.get(parent)
        if par is None:
            _fail(f"parent {parent} does not exist")
        if par.kind != "group":
            _fail(f"parent {parent} is not kind: group")
    if kind == "group":
        if not id:
            _fail("a group requires --id")
        tid = id
    elif id:
        tid = id
    elif par is not None:
        nums = [int(t.id[len(par.id) + 1:]) for t in ts.children(par.id)
                if t.id.startswith(par.id + "-") and t.id[len(par.id) + 1:].isdigit()]
        tid = f"{par.id}-{max(nums, default=0) + 1:02d}"
    else:
        _fail("a ticket without --parent requires --id")
    if not ops.ID_RE.match(tid):
        _fail(f"invalid id {tid}")
    if tid in ts.tickets:
        _fail(f"id {tid} already exists")
    where = (r / par.path).parent if par is not None else base
    if kind == "group":
        path = where / f"{tid}-{slug}" / "index.md"
    else:
        path = where / f"{tid}-{slug}.md"
    if path.exists():
        _fail(f"{path} already exists")
    try:
        text = ops.new_ticket_text(base, tid, title, parent, size.upper(), dep_ids, kind)
        edits = {}
        for d in dep_ids:
            dp = r / ts.tickets[d].path
            cur = ops.read_text(dp)
            edits[dp] = ops.set_fm_line(cur, "blocks", ops._yaml_list(ts.tickets[d].blocks + [tid]))
    except ops.TicketOpError as e:
        _bad(e)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ops.write_atomic(path, text)
        for dp, t in edits.items():
            ops.write_atomic(dp, t)
    except ops.TicketOpError as e:
        _bad(e)
    typer.echo(str(path))


@ticket_app.command()
def log(ticket_id: Annotated[str, typer.Argument(metavar="ID")],
        text: Annotated[Optional[str], typer.Argument(help="Entry text; '-' or omitted: stdin.")] = None,
        agent: Annotated[str, typer.Option("--agent")] = ...,
        date: Annotated[Optional[str], typer.Option("--date")] = None,
        tokens: Annotated[bool, typer.Option("--tokens", help="Start the entry with the token usage line "
                                             "(myvec tokens --ticket ID --summary); TEXT is the conclusion.")] = False,
        no_scan: Annotated[bool, typer.Option("--no-scan", help="With --tokens: do not read new transcript lines.")] = False,
        root: RootOpt = None, dir: DirOpt = "docs/todo"):
    """Append '### date agent' + text to the ticket's ## Log."""
    r = _root(root)
    ts = _load(r, dir)
    t = ts.tickets.get(ticket_id)
    if t is None:
        _fail(f"unknown ticket {ticket_id}")
    if tokens:
        from myvec import tokencli, tokens as TK

        if text == "-":
            text = sys.stdin.read().strip()
        tokencli._maybe_scan(no_scan)
        idx = tokencli._open_index()
        try:
            from myvec import sessions

            proj = (sessions.project_of(idx, str(r), sessions.main_root_of(str(r))) if idx is not None else None) or str(r)
            d = TK.ticket_summary(idx, proj, ticket_id)
        finally:
            if idx is not None:
                idx.close()
        if d is None:
            _fail(f"no token usage found for {ticket_id}")
        text = TK.summary_line(d) + (f"\n\n{text}" if text else "")
    elif text is None or text == "-":
        text = sys.stdin.read()
    date = date or datetime.date.today().isoformat()
    p = r / t.path
    try:
        cur = ops.read_text(p)
        new_text = ops.set_fm_line(ops.append_log(cur, date, agent, text), "updated", date)
    except ops.TicketOpError as e:
        _bad(e)
    try:
        ops.write_atomic(p, new_text)
    except ops.TicketOpError as e:
        _bad(e)
    typer.echo(f"{ticket_id}: log entry added")


@ticket_app.command()
def claim(ticket_id: Annotated[Optional[str], typer.Argument(metavar="[ID]")] = None,
          session: Annotated[Optional[str], typer.Option("--session")] = None,
          release: Annotated[bool, typer.Option("--release")] = False,
          root: RootOpt = None, dir: DirOpt = "docs/todo"):
    """Bind this Claude session to a ticket (or release it)."""
    sid = session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        _fail("no session id: pass --session UUID or set CLAUDE_CODE_SESSION_ID")
    r = _root(root)
    if release:
        ops.record_claim(sid, None, r)
        typer.echo(f"released ← {sid[:8]}")
        return
    if not ticket_id:
        _fail("ticket ID required (or use --release)")
    if ticket_id not in _load(r, dir).tickets:
        _fail(f"unknown ticket {ticket_id}")
    ops.record_claim(sid, ticket_id, r)
    typer.echo(f"{ticket_id} ← {sid[:8]}")


def _lesson_origin(cwd: Path) -> str:
    """Default `--from`: the marker name of cwd, else the git toplevel's base name, sanitized to ID_RE."""
    import re

    from myvec import projects

    name = ""
    try:
        m = projects.find_marker(cwd)
        if m is not None:
            name = m.name
    except projects.InvalidMarker:
        pass
    if not name:
        name = _root(None).name
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name).lstrip(".-_")[:64]
    return name or "unknown"


@ticket_app.command()
def lesson(stdin_marker: Annotated[Optional[str], typer.Argument(metavar="-", help="Lesson text is read from stdin; "
                                                                    "pass '-'.")] = None,
           title: Annotated[str, typer.Option("--title", help="One-line title (<= 120 chars).")] = ...,
           to: Annotated[Optional[str], typer.Option("--to", help="Target project name (default: config "
                                                      "lessons_project).")] = None,
           from_: Annotated[Optional[str], typer.Option("--from", help="Source project name (default: marker of cwd).")] = None):
    """Propose a general lesson to another project's inbox (ADR-027); text only from stdin, via inbox.write."""
    import re
    import sqlite3

    from myvec import config, db, inbox, projects

    if stdin_marker != "-":
        _fail("lesson text must come from stdin: pass '-'", 2)
    title = title.strip()
    if not title or len(title) > 120 or re.search(r"[\x00-\x1f\x7f]", title):
        _fail("--title must be a single line of 1-120 characters", 2)
    body = sys.stdin.read(inbox.MAX_TEXT * 4 + 1)
    if len(body) > inbox.MAX_TEXT * 4:
        _fail("lesson text on stdin is too long", 2)
    body = body.strip()
    if not body:
        _fail("empty lesson text on stdin", 2)
    origin = from_ or _lesson_origin(Path.cwd())
    if not inbox.ID_RE.match(origin):
        _fail("--from is not a valid project name", 2)
    target = to or config.load_config().lessons_project
    if not config.db_path().exists():
        _fail("no index database: cannot resolve the target project", 2)
    try:
        conn = db.connect(vec=False, readonly=True)
        try:
            row = conn.execute("SELECT root FROM projects WHERE path=?", (target,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _fail(f"database error: {e}", 2)
    if row is None:
        _fail(f"unknown project {target!r}", 2)
    root = Path(row["root"])
    m = projects.try_read_marker(root)
    tickets_dir = m.tickets_dir if m is not None else projects.DEFAULT_TICKETS_DIR
    try:
        item = inbox.write(root, tickets_dir, inbox.LESSON_TICKET, f"## {title}\n\n{body}", kind="lesson",
                           source="cli", origin=origin)
    except inbox.InboxError as e:
        _fail(f"{e.code}: {e.message}", 3 if e.code == "full" else 2)
    typer.echo(item["file"])


def _resolve_migrate_dir(r: Path, dir_: str | None, dets) -> str:
    if dir_:
        from myvec import ticketmigrate as M

        d = dir_.rstrip("/")
        try:
            M.inside(r, d)
        except ops.TicketOpError as e:
            _fail(str(e), 2)
        if not (r / d).exists():
            _fail(f"{r / d} does not exist")
        return d
    from myvec import ticketmigrate as M

    mk = M._marker(r)
    first = mk.tickets_dir if mk else "docs/todo"
    if (r / first).is_dir() and any(d.dir == first for d in dets):
        return first
    cands = [d for d in dets if d.kind != "notes"]
    if len(cands) == 1:
        return cands[0].dir
    if not cands:
        _fail("no ticket candidates found", 1)
    typer.echo("error: several candidates, pass --dir:", err=True)
    for d in cands:
        typer.echo(f"  {d.dir} ({d.kind}, {d.n_md} md)", err=True)
    raise typer.Exit(2)


@ticket_app.command()
def migrate(root: RootOpt = None,
            dir: Annotated[Optional[str], typer.Option("--dir", help="Candidate dir or file relative to root.")] = None,
            detect: Annotated[bool, typer.Option("--detect", help="Report candidates and their format (default).")] = False,
            json_out: Annotated[bool, typer.Option("--json", help="With --detect: machine-readable output.")] = False,
            brief: Annotated[bool, typer.Option("--brief", help="Print the migration brief for the project agent.")] = False,
            out: Annotated[Optional[Path], typer.Option("--out", help="With --brief: write to this file.")] = None,
            check: Annotated[bool, typer.Option("--check", help="What is left before canon v1; exit 0 only when done.")] = False,
            fix: Annotated[bool, typer.Option("--fix", help="Mechanical header fixes (dry-run).")] = False,
            write: Annotated[bool, typer.Option("--write", help="With --fix or --split: apply (default is a dry-run).")] = False,
            add_goal_heading: Annotated[bool, typer.Option(
                "--add-goal-heading", help="With --fix: add the goal heading to ticket bodies that lack one "
                "(without it bodies are never changed).")] = False,
            split: Annotated[bool, typer.Option("--split", help="Split a single todo file (--dir FILE) into a group "
                                                "and tickets, text verbatim (dry-run unless --write).")] = False,
            group_id: Annotated[str, typer.Option("--group-id", help="With --split: id of the new group.")] = "E1",
            group_slug: Annotated[str, typer.Option("--group-slug", help="With --split: slug of the new group.")] = "backlog",
            group_title: Annotated[Optional[str], typer.Option("--group-title", help="With --split: title (default: the H1).")] = None,
            dest: Annotated[str, typer.Option("--dest", help="With --split: tickets dir for the new group.")] = "docs/todo",
            status_style: Annotated[str, typer.Option("--status-style", help="With --split: keys or emoji.")] = "keys"):
    """Migrate tickets to canon v1: detect the format, brief the project agent, check, fix headers (ADR-024)."""
    from myvec import ticketmigrate as M

    r = _root(root)
    if sum([brief, check, fix, split]) > 1:
        _fail("use one of --brief, --check, --fix, --split", 2)
    if write and not (fix or split):
        _fail("--write requires --fix or --split", 2)
    if add_goal_heading and not fix:
        _fail("--add-goal-heading requires --fix", 2)
    if split and not dir:
        _fail("--split requires --dir FILE (the single todo file)", 2)
    if status_style not in ("keys", "emoji"):
        _fail("--status-style must be keys or emoji", 2)
    if split:
        rel = dir.rstrip("/")
        try:
            files = M.split_plan(r, rel, group_id, group_slug, group_title, dest, status_style)
            for f in files:
                typer.echo(f"{f.kind:8} {f.path}")
            if write:
                M.apply_split(r, files)
                typer.echo(f"split: {len(files) - 2} tickets in {files[0].path.rsplit('/', 1)[0]}, "
                           f"{rel} is a pointer now; run `myvec ticket index`")
            else:
                typer.echo(f"{len(files) - 2} tickets to create (dry-run; add --write to apply)")
        except M.AlreadySplit as e:
            typer.echo(str(e))
        except (ops.TicketOpError, OSError) as e:
            _fail(str(e), 2)
        return
    dets = M.detect_all(r)
    if brief or check or fix:
        rel = _resolve_migrate_dir(r, dir, dets)
    if brief:
        det = next((d for d in dets if d.dir == rel), None) or M.detect(r, rel)
        mk = M._marker(r)
        text = M.render_brief(det, mk.name if mk else r.name, rel)
        if out is not None:
            try:
                ops.write_atomic(out, text)
            except (ops.TicketOpError, OSError) as e:
                _bad(e)
            typer.echo(str(out))
        else:
            typer.echo(text, nl=False)
        return
    if check:
        issues, n = M.migration_check(r, rel)
        _print_issues(issues)
        left = M.left_over(issues)
        lang = ops.lang_for(r / rel)
        if lang == "en":
            typer.echo(f"{n} {'ticket' if n == 1 else 'tickets'}, {len(left)} left")
        elif lang == "ru":
            typer.echo(f"{n} {_uk_plural(n, 'тикет', 'тикета', 'тикетов')}, осталось {len(left)}")
        else:
            typer.echo(f"{n} {_uk_plural(n, 'тікет', 'тікети', 'тікетів')}, {len(left)} лишилось")
        if left:
            raise typer.Exit(1)
        return
    if fix:
        try:
            plan = M.fix_plan(r, rel, add_goal=add_goal_heading)
        except ops.TicketOpError as e:
            _fail(str(e), 2)
        if not plan:
            typer.echo("nothing to fix")
            return
        for fx in plan:
            typer.echo(f"{fx.path}")
            for before, after in fx.changes:
                typer.echo(f"  - {before}\n  + {after}")
        if write:
            try:
                M.apply_fixes(r, plan)
            except (ops.TicketOpError, OSError) as e:
                _bad(e)
            typer.echo(f"fixed: {len(plan)} files")
        else:
            typer.echo(f"{len(plan)} files to fix (dry-run; add --write to apply)")
        return
    if dir:
        try:
            M.inside(r, dir.rstrip("/"))
        except ops.TicketOpError as e:
            _fail(str(e), 2)
        dets = [d for d in dets if d.dir == dir.rstrip("/")] or [M.detect(r, dir.rstrip("/"))]
    legacy = M.legacy_script_issues(r)
    if json_out:
        typer.echo(M.detect_json(dets))
        _print_issues(legacy, err=True)
        return
    if not dets:
        typer.echo("no ticket candidates found")
        _print_issues(legacy)
        return
    rec = M.recommended(dets)
    for d in dets:
        typer.echo(f"{d.dir}{'  (recommended)' if d.dir == rec else ''}")
        typer.echo(f"  kind: {d.kind}; md: {d.n_md}; header: {d.n_frontmatter}; id: {d.n_with_id}; "
                   f"canon: {d.n_canon}; statuses: {d.status_form}; lang: {d.lang}; "
                   f"git: {d.git_root if d.git_root is not None else '-'}")
        if d.n_legacy_epic or d.n_legacy_groups:
            typer.echo(f"  legacy: epic {d.n_legacy_epic}, synthetic groups {d.n_legacy_groups}")
        if d.is_file:
            typer.echo(f"  items: {d.n_items}; checkboxes: {d.n_boxes}")
        fm = [f for f in M.field_map(d) if f["target"] not in ("=",)]
        for f in fm:
            typer.echo(f"  field {f['key']} x{f['count']} -> {f['target']}")
        for s_ in M.status_map(d):
            if s_["target"] == "?" or T.normalize_status(s_["raw"]) is None:
                typer.echo(f"  status {s_['raw']} x{s_['count']} -> {s_['target']}")
    _print_issues(legacy)


def _uk_plural(n: int, one: str, few: str, many: str) -> str:
    m, d = n % 100, n % 10
    return many if 10 < m < 20 else one if d == 1 else few if 2 <= d <= 4 else many
