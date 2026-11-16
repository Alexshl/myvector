"""Typer CLI (imported lazily: hooks bypass this module)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from myvec import config, logutil, projects
from myvec import db as dbmod
from myvec import lock as lockmod

SYNC_LOCK_WAIT_S = 2.0

app = typer.Typer(help="myvec: local hybrid code search + two-level memory for Claude Code.",
                  no_args_is_help=True, add_completion=False)


def _fail(msg: str, code: int = 1):
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _open(vec: bool = True):
    cfg = config.load_config()
    try:
        return cfg, dbmod.open_ready(cfg, vec=vec)
    except dbmod.ModelMismatch as e:
        _fail(str(e), 2)


def _marker_or_fail(cwd: Path | None = None) -> projects.Marker:
    try:
        m = projects.find_marker(cwd or Path.cwd())
    except projects.InvalidMarker as e:
        _fail(str(e))
    if m is None:
        _fail("not inside a myvec project (no .myvector.toml found). Run `myvec init` in the project root.")
    return m


def _spawn_detached(args: list[str], cwd: Path, logname: str) -> None:
    config.ensure_home()
    out = logutil.open_append(config.logs_dir() / logname)
    subprocess.Popen([sys.executable, "-m", "myvec", *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
                     stdout=out, stderr=out, start_new_session=True, close_fds=True)
    out.close()


def _sync_log(msg: str) -> None:
    logutil.append(config.logs_dir() / "sync.log", msg)


# ------------------------------------------------------------------ init / sync

@app.command()
def init(
    name: Annotated[Optional[str], typer.Option(help="Project name (default: directory name)")] = None,
    parent: Annotated[Optional[str], typer.Option(help="Parent project path, e.g. 'project-b'")] = None,
    include: Annotated[Optional[list[str]], typer.Option(help="Include glob (repeatable)")] = None,
    exclude: Annotated[Optional[list[str]], typer.Option(help="Exclude glob (repeatable)")] = None,
    share_global: Annotated[Optional[bool], typer.Option("--share-global/--no-share-global",
                                                         help="Let this project see global memories")] = None,
    fg: Annotated[bool, typer.Option("--fg", help="Run the first sync in the foreground")] = False,
):
    """Write .myvector.toml in the current directory, register the project and start indexing."""
    from myvec import indexer

    root = Path.cwd().resolve()
    try:
        old = projects.read_marker(root)
    except projects.InvalidMarker:
        old = None  # a broken marker is simply rewritten
    new_name = name or (old.name if old else root.name)
    new_parent = parent if parent is not None else (old.parent if old else None)
    why = projects.path_error(projects.prospective_path(root, new_name, new_parent))
    if why:
        _fail(f"invalid project name {new_name!r}: {why}. Choose another with `myvec init --name ...`.")
    cfg, db = _open()
    try:
        # nothing is written until the project path is known to be free (two dirs named `api` must not collide)
        indexer.check_project_free(db, projects.Marker(root, new_name, new_parent,
                                                       path=projects.prospective_path(root, new_name, new_parent)))
    except indexer.ProjectConflict as e:
        db.close()
        _fail(str(e))
    try:
        projects.write_marker(
            root,
            new_name,
            new_parent,
            include if include is not None else (old.include if old else []),
            exclude if exclude is not None else (old.exclude if old else []),
            share_global if share_global is not None else (old.share_global if old else True),
        )
    except projects.MarkerRewriteRefused as e:
        db.close()
        _fail(str(e))
    m = projects.read_marker(root)
    config.register_root(str(root))
    indexer.register_project(db, m)
    db.close()
    typer.echo(f"project '{m.path}' registered ({root / projects.MARKER})")
    try:
        from myvec import gitexcludes

        xfile, added = gitexcludes.ensure_marker_excluded()
        if added:
            typer.echo(f"added {gitexcludes.LINE} to the global git excludes ({xfile})")
    except Exception as e:  # noqa: BLE001 - never fail init because of git excludes
        typer.echo(f"warning: could not update the global git excludes: {e}", err=True)
    if fg:
        sync(bg=False, quiet=False)
    else:
        _spawn_detached(["sync", "--quiet"], root, "init-sync.log")
        typer.echo(f"sync started in background (log: {config.logs_dir() / 'init-sync.log'})")


@app.command()
def sync(
    bg: Annotated[bool, typer.Option("--bg", help="Run detached in the background")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="No output (errors go to logs/sync.log)")] = False,
):
    """Index the current project (all its repos, current branch)."""
    marker = _marker_or_fail()
    if bg:
        _spawn_detached(["sync", "--quiet"], Path.cwd(), "sync.log")
        if not quiet:
            typer.echo("sync started in background")
        return
    with lockmod.sync_lock(wait=SYNC_LOCK_WAIT_S) as got:  # a sync that only just started releases within moments
        if not got:
            if not quiet:
                typer.echo("sync already running")
            return
        from myvec import indexer
        from myvec.embed import EmbedError, get_embedder

        cfg, db = _open()
        t0 = time.perf_counter()
        log = (lambda s: None) if quiet else typer.echo
        try:
            stats = indexer.sync_project(db, cfg, get_embedder(cfg), marker, log)
        except KeyboardInterrupt:
            # an open transaction rolls back when the connection closes; saved batches resume next sync
            _sync_log(f"sync of {marker.path} interrupted (Ctrl+C), saved batches resume next sync")
            if not quiet:
                typer.echo("sync interrupted (Ctrl+C); saved batches are kept, the next sync resumes", err=True)
            raise typer.Exit(130)
        except EmbedError as e:
            _sync_log(f"embed error: {e}")
            _fail(str(e))
        except indexer.ProjectConflict as e:
            _sync_log(f"project conflict: {e}")
            _fail(str(e))
        finally:
            db.close()
        dt = time.perf_counter() - t0
        msg = (f"synced {marker.path}: {stats.repos} repos, {stats.files} files, {stats.new_blobs} new blobs, "
               f"{stats.chunks_embedded} chunks embedded, {stats.vectors_reused} vectors reused, "
               f"{stats.secrets_masked} secret values masked, {stats.secrets_skipped} secret chunks skipped, {dt:.1f}s")
        _sync_log(msg + (" ERRORS: " + "; ".join(stats.errors) if stats.errors else ""))
        if not quiet:
            typer.echo(msg)
            for e in stats.errors:
                typer.echo(f"  error: {e}", err=True)


# ------------------------------------------------------------------ search

@app.command()
def search(
    query: Annotated[Optional[str], typer.Argument(help="Query (or several with -q)")] = None,
    q: Annotated[Optional[list[str]], typer.Option("-q", "--query", help="Query; repeat for up to 5 per call")] = None,
    k: Annotated[Optional[int], typer.Option("--k", help="Number of results (default: k in config.toml, 10)")] = None,
    scope: Annotated[str, typer.Option(help="repo | project | all")] = "project",
    kind: Annotated[str, typer.Option(help="code | memory | docs (.md/.rst files) | all")] = "all",
    mode: Annotated[str, typer.Option(help="auto | vector | hybrid (auto: single-token query -> hybrid, else vector)")] = "auto",
    full: Annotated[bool, typer.Option("--full", help="Print whole chunks")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Timings to stderr")] = False,
    by_project: Annotated[bool, typer.Option("--by-project", help="Summary per project over all projects "
                                                                  "(\"did we do this elsewhere?\")")] = False,
    project: Annotated[Optional[str], typer.Option("--project", help="Search in one project (name or path), "
                                                                     "e.g. another one from --by-project")] = None,
    compact: Annotated[bool, typer.Option("--compact", help="One `path:lines symbol` line per hit, no snippets "
                                                            "(default when stdout is not a terminal)")] = False,
    expand: Annotated[bool, typer.Option("--expand", help="Snippets per hit (default in a terminal)")] = False,
):
    """Hybrid (vector + full-text) search over code and memories (via the daemon if it runs, else directly)."""
    import json as _json
    import sys

    from myvec import daemon_client, searchcmd

    queries = ([query] if query is not None else []) + list(q or [])
    if not queries:
        _fail("give a query: myvec search \"...\" or -q \"...\"")
    if len(queries) > 5:
        _fail("at most 5 queries per call")
    if compact and expand:
        _fail("--compact and --expand cannot be combined")
    use_compact = False if (expand or full) else (compact or not sys.stdout.isatty())
    cwd = str(Path.cwd())
    many = len(queries) > 1
    needs_echo = use_compact and not (json_out or by_project)  # a daemon that predates E8-13 ignores `compact`
    code, outs = 0, []
    for i, qq in enumerate(queries, 1):
        res = None
        if not debug:  # `--debug` prints local timings: always direct
            r = daemon_client.request("POST", "/api/cli/search", {
                "q": qq, "cwd": cwd, "scope": scope, "kind": kind, "mode": mode, "k": k, "full": full,
                "json": json_out, "by_project": by_project, "project": project, "compact": use_compact,
                "total": True})
            if r is not None and "stdout" in r and "exit_code" in r and (not (by_project or project) or r.get("cross")) \
                    and (not needs_echo or r.get("compact")):
                res = searchcmd.CliResult(stdout=r["stdout"], stderr=r.get("stderr", ""), exit_code=int(r["exit_code"]))
        if res is None:
            res = searchcmd.run_search(qq, cwd, k=k, scope=scope, kind=kind, mode=mode, full=full, json_out=json_out,
                                       debug=debug, by_project=by_project, project=project, compact=use_compact, total=True)
        if res.stderr:
            head = f"# {i}/{len(queries)} " if many else ""
            typer.echo("\n".join(head + ln for ln in res.stderr.splitlines()) if many else res.stderr, err=True)
        if res.exit_code:
            code = code or res.exit_code
            continue
        outs.append((i, qq, res.stdout))
    if outs:
        if not many:
            typer.echo(outs[0][2])
        elif json_out:
            typer.echo(_json.dumps([{"query": qq, "results": _json.loads(o)} for _, qq, o in outs],
                                   ensure_ascii=False, indent=2))
        else:
            typer.echo("\n".join(f"# {i}/{len(queries)} {qq[:80]}\n{o}" for i, qq, o in outs))
    if code:
        raise typer.Exit(code)


# ------------------------------------------------------------------ memory

@app.command()
def remember(
    text: str,
    global_: Annotated[bool, typer.Option("--global", help="Global scope")] = False,
    repo: Annotated[bool, typer.Option("--repo", help="Scope = current sub-repo instead of project")] = False,
    branch: Annotated[bool, typer.Option("--branch", help="Visible only on the current branch (auto-promoted to "
                                                          "the project when merged, archived if deleted unmerged)")] = False,
    tag: Annotated[Optional[list[str]], typer.Option("--tag", help="Tag (repeatable)")] = None,
    pin: Annotated[bool, typer.Option("--pin")] = False,
    supersedes: Annotated[Optional[str], typer.Option(help="Id of a memory this one replaces")] = None,
):
    """Save a memory (markdown in the vault + searchable index)."""
    from myvec import ctx as ctxmod
    from myvec import gitutil, memory
    from myvec.embed import EmbedError, get_embedder

    cfg, db = _open()
    c = ctxmod.current(db, Path.cwd())
    br = commit = origin_branch = origin_repo = base_commit = None
    if global_:
        scope = "global"
    else:
        if c is None:
            _fail("not inside a myvec project; use --global or run `myvec init` first.")
        scope = c.repo_path if repo else c.project_path
    if branch and (c is None or c.repo_dir is None or not gitutil.is_repo(c.repo_dir)):
        _fail("--branch needs a git repository")
    if c is not None and c.repo_dir is not None and gitutil.is_repo(c.repo_dir):
        # origin metadata is always recorded; only --branch turns it into a visibility filter
        origin_branch, origin_repo, commit = c.branch, c.repo_path, gitutil.head_commit(c.repo_dir)
        if branch:
            br = origin_branch
            default = gitutil.default_branch(c.repo_dir)
            if default and br != default:  # the lifecycle needs to know whether HEAD was already inside the default
                base_commit = gitutil.merge_base(c.repo_dir, "HEAD", default)
    try:
        m, action = memory.remember(db, get_embedder(cfg), text, scope, branch=br, commit=commit,
                                    tags=tag or [], pin=pin, supersedes=supersedes, origin_branch=origin_branch,
                                    origin_repo=origin_repo, base_commit=base_commit)
    except (ValueError, EmbedError) as e:
        _fail(str(e))
    if c is not None:
        from myvec import activity

        activity.touch(c.project_path)
    if m.masked:
        from myvec import filters

        typer.echo(f"note: {m.masked} secret value(s) masked as {filters.MASK}")
    if action == "updated":
        typer.echo(f"updated existing memory {m.id} (similar one found in scope '{scope}'): {m.file}")
    else:
        typer.echo(f"saved memory {m.id} in scope '{scope}': {m.file}")


@app.command()
def forget(memory_id: str):
    """Archive a memory (moved to vault/_archive, never hard-deleted)."""
    from myvec import memory

    cfg, db = _open()
    try:
        m = memory.forget(db, memory_id)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"archived memory {m.id}")


@app.command()
def pin(memory_id: str, unpin: Annotated[bool, typer.Option("--unpin")] = False):
    """Pin a memory (always shown at session start)."""
    from myvec import memory

    cfg, db = _open()
    try:
        m = memory.pin(db, memory_id, on=not unpin)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"{'unpinned' if unpin else 'pinned'} memory {m.id}")


@app.command()
def promote(memory_id: str):
    """Move a memory to the global scope (vault/global/)."""
    from myvec import memory

    cfg, db = _open()
    try:
        m = memory.promote(db, memory_id)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"promoted memory {m.id} to global: {m.file}")


@app.command(name="import")
def import_cmd():
    """Read-only import of Claude memory files, CLAUDE.md and graphify GRAPH_REPORT.md."""
    from myvec import memory
    from myvec.embed import EmbedError, get_embedder

    cfg, db = _open()
    try:
        r = memory.import_all(db, get_embedder(cfg))
    except EmbedError as e:
        _fail(str(e))
    typer.echo(f"imported {r['memories']} memories from {r['files']} files "
               f"({r['skipped_secrets']} secret pieces skipped, {r['removed']} stale files dropped)")


@app.command()
def topics():
    """Cluster memories offline and write MOC notes to vault/_topics/."""
    from myvec import memory

    cfg, db = _open()
    files = memory.topics(db)
    typer.echo(f"wrote {len(files)} topic notes" + "".join(f"\n  {f}" for f in files))


@app.command("memory-audit")
def memory_audit(as_json: Annotated[bool, typer.Option("--json")] = False,
                 candidates: Annotated[bool, typer.Option("--candidates", help="also list vault project memories "
                                                          "that might belong in global (local view only)")] = False):
    """Read-only audit of memory composition (aggregates; --candidates adds per-memory signals)."""
    import sqlite3

    import sqlite_vec

    from myvec import memaudit

    path = config.db_path()
    if not path.exists():
        _fail("no index database yet (nothing to audit)")
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        res = memaudit.audit(db)
        cand = memaudit.candidates(db) if candidates else None
    except sqlite3.Error as e:
        _fail(f"cannot read the index database: {e}")
    if res["active_total"] == 0 and not res["archived"]:
        _fail("no memories in the index")
    if as_json:
        typer.echo(json.dumps({**res, **({"candidates": cand} if cand is not None else {})}, ensure_ascii=False))
        return
    typer.echo(f"active memories: {res['active_total']} in {res['scopes']} scopes "
               f"(pinned {res['pinned']}, with branch {res['with_branch']})")
    for k, v in res["active"].items():
        typer.echo(f"  {k}: {v}")
    if res["archived"]:
        typer.echo("archived: " + ", ".join(f"{k} {v}" for k, v in res["archived"].items()))
    typer.echo("by month: " + ", ".join(f"{k} {v}" for k, v in res["by_month"].items()))
    typer.echo("import by type: " + ", ".join(f"{k} {v}" for k, v in res["import_by_type"].items()))
    typer.echo(f"import duplicate groups across scopes: {res['import_duplicate_groups']} "
               f"(up to {res['import_duplicate_max_scopes']} scopes)")
    if cand is not None:
        typer.echo(f"\ncandidates for global: {len(cand)}")
        for c in cand:
            typer.echo(f"  {c['id']}  [{c['scope']}]  sim>=0.75 in {c['other_scopes_075']} scopes, "
                       f"max cos {c['max_cos_other']}, refs {'yes' if c['has_project_refs'] else 'no'}: {c['first']}")


# ------------------------------------------------------------------ maintenance

@app.command()
def rebuild(no_import: Annotated[bool, typer.Option("--no-import")] = False):
    """Recreate memory tables from the vault and re-sync code (index.db is disposable)."""
    from myvec import indexer, memory
    from myvec.embed import EmbedError, get_embedder

    cfg, db = _open()
    emb = get_embedder(cfg)
    try:
        n = memory.rebuild_from_vault(db, emb)
        typer.echo(f"restored {n} memories from the vault")
        roots = [Path(r) for r in config.load_registry()]
        markers = [m for m in (projects.try_read_marker(r) for r in roots if r.exists()) if m]
        registered = []
        for m in markers:
            try:
                indexer.register_project(db, m)
                registered.append(m)
            except indexer.ProjectConflict as e:
                typer.echo(f"warning: {e}", err=True)
        markers = registered
        if not no_import:
            r = memory.import_all(db, emb)
            typer.echo(f"imported {r['memories']} memories from {r['files']} files")
        with lockmod.sync_lock() as got:
            if not got:
                typer.echo("sync already running; skipped code re-sync")
                return
            for m in markers:
                st = indexer.sync_project(db, cfg, emb, m)
                typer.echo(f"synced {m.path}: {st.files} files, {st.chunks_embedded} chunks embedded")
    except EmbedError as e:
        _fail(str(e))


@app.command()
def reembed():
    """Re-create all vectors after a model/dims change in config.toml."""
    from myvec import indexer
    from myvec.embed import EmbedError, get_embedder

    cfg = config.load_config()
    db = dbmod.connect()
    dbmod.init_schema(db, cfg)  # no model check on purpose: reembed is the way out of a mismatch
    with lockmod.sync_lock() as got:
        if not got:
            typer.echo("sync already running")
            return
        try:
            r = indexer.reembed(db, cfg, get_embedder(cfg), log=typer.echo)
        except EmbedError as e:
            _fail(str(e))
    typer.echo(f"re-embedded {r['chunks']} chunks and {r['memories']} memories with {cfg.model} ({cfg.dims} dims)")


@app.command()
def gc(
    vacuum: Annotated[bool, typer.Option("--vacuum", help="Then compact index.db (VACUUM): returns the space freed "
                                         "by removed chunks to the disk; searches wait while it runs")] = False,
):
    """Drop manifests of deleted branches and unreferenced blobs/chunks/vectors."""
    from myvec import indexer

    cfg, db = _open()
    with lockmod.sync_lock() as got:
        if not got:
            typer.echo("sync already running")
            return
        def log(msg: str) -> None:
            typer.echo(msg)
            _sync_log(msg)

        r = indexer.gc(db, log)
        typer.echo(f"gc: {r['repos']} vanished repos, {r['manifests']} branch manifests, {r['vectors']} vectors, {r['chunks']} chunks, {r['blobs']} blobs removed; "
                   f"memories: {r.get('memories_merged', 0)} branch->project, {r.get('memories_archived', 0)} archived")
        from myvec import forget as forgetmod

        if (h := forgetmod.hint(db)):
            typer.echo(h)
        if vacuum:  # under the sync lock: no writer races the rewrite
            before = config.db_path().stat().st_size
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            after = config.db_path().stat().st_size
            typer.echo(f"vacuum: index.db {before / 1e6:,.0f} MB -> {after / 1e6:,.0f} MB")


@app.command()
def relocate(
    from_: Annotated[str, typer.Option("--from", help="Old absolute path prefix, e.g. /Users/me")],
    to: Annotated[str, typer.Option("--to", help="New absolute path prefix, e.g. /home/me")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only count what would change")] = False,
):
    """Rewrite path prefixes (project roots, repo dirs, vault files, registry) after moving the index to another home.
    Vectors and chunks are content-addressed and stay untouched; run `myvec sync` in each project afterwards."""
    from myvec import relocate as reloc

    if not from_.startswith("/") or not to.startswith("/"):
        _fail("--from and --to must be absolute paths")
    _, db = _open(vec=False)
    with lockmod.sync_lock() as got:
        if not got:
            _fail("sync is running; try again in a moment")
        counts = reloc.relocate(db, from_, to, dry_run=dry_run)
    for table, n in counts.items():
        typer.echo(f"{table}: {n}")
    typer.echo(("would rewrite " if dry_run else "rewrote ") + f"{sum(counts.values())} paths: {from_} -> {to}")


@app.command(name="forget-project")
def forget_project(
    target: Annotated[Optional[str], typer.Argument(help="Project path (as in `myvec status`) or its root directory")] = None,
    missing: Annotated[bool, typer.Option("--missing", help="All roots whose directory is gone (the parent exists); "
                                          "unreachable ones are only listed")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Actually forget (default: dry run)")] = False,
):
    """Forget projects/registry roots in the index (not their vault memory, files or `.myvector.toml`)."""
    from myvec import forget as forgetmod

    if bool(target) == bool(missing):
        _fail("give exactly one of TARGET or --missing", 2)
    if not config.db_path().exists():
        typer.echo("no index yet")
        return
    _, db = _open()
    try:
        everything = forgetmod.all_items(db)
        items = forgetmod.plan(db, target, missing, everything)
    except LookupError as e:
        _fail(str(e))
    skipped = []
    if missing:
        seen = {i.root for i in items}
        skipped = sorted({i.root for i in everything if i.state == "unreachable"} - seen)
    for it in items:
        typer.echo(f"{it.project or '(registry only)'}  root={it.root}  state={it.state}  repos={it.repos}  "
                   f"files={it.files}  chunks={it.chunks}  tickets={it.tickets}  "
                   f"registry={'yes' if it.in_registry else 'no'}  child projects kept={it.children}")
        if it.state == "present":
            typer.echo("  warning: the directory exists; its .myvector.toml stays and `myvec init`/`sync` there brings the project back")
    for root in skipped:
        typer.echo(f"skipped: {root}: parent directory is missing (maybe not mounted) - forget it explicitly by name")
    typer.echo("note: the project's vault memory is NOT removed (only the index and the registry)")
    if not yes:
        n_proj = sum(1 for i in items if i.project)
        n_reg = sum(1 for i in items if i.in_registry)
        typer.echo(f"dry run: {n_proj} projects / {n_reg} registry roots would be forgotten; rerun with --yes")
        if items:
            sw = forgetmod.sweep_preview(db, items)
            typer.echo(f"dry run: the sweep would also drop {sw['vectors']} vectors, {sw['chunks']} chunks, "
                       f"{sw['blobs']} blobs (orphans, as in gc)")
        return
    if not items:
        typer.echo("nothing to forget")
        return
    with lockmod.sync_lock() as got:
        if not got:
            _fail("sync is running; try again in a moment")
        try:  # re-plan under the lock: a root restored since the first plan must not be forgotten
            items = forgetmod.plan(db, target, missing)
        except LookupError as e:
            _fail(str(e))
        if not items:
            typer.echo("nothing to forget")
            return
        r = forgetmod.apply(db, items)
    typer.echo(f"forgot {r['projects']} projects, {r['repos']} repos, {r['registry']} registry roots; "
               f"swept {r['vectors']} vectors, {r['chunks']} chunks, {r['blobs']} blobs")


@app.command()
def status():
    """Projects, repos, branches, counts, last sync, sync mode / activity window, masked secrets."""
    if not config.db_path().exists():
        typer.echo("no index yet")
        return
    from myvec import activity

    cfg = config.load_config()
    db = dbmod.connect(vec=False)
    has_meta = db.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone()
    m = {r[0]: r[1] for r in db.execute("SELECT key, value FROM meta")} if has_meta else {}
    if has_meta:
        dbmod.migrate(db)
        try:
            activity.fold_activity(db)
        except Exception:  # noqa: BLE001
            pass
    ok = m.get("model") == cfg.model and m.get("dims") == str(cfg.dims)
    typer.echo(f"home: {config.home()}")
    typer.echo(f"index model: {m.get('model')} / {m.get('dims')} dims" + ("" if ok else f"   !! config says {cfg.model}/{cfg.dims}: run `myvec reembed`"))
    from myvec import daemon_client

    d = daemon_client.read_daemon()
    typer.echo(f"daemon: " + (f"running on 127.0.0.1:{d['port']} (pid {d['pid']})" if d else "not running"))
    if has_meta:
        from myvec import forget as forgetmod

        if (h := forgetmod.hint(db)):
            typer.echo(h)
    now = time.time()
    for p in db.execute("SELECT * FROM projects ORDER BY path"):
        typer.echo(f"\nproject {p['path']}  ({p['root']})  share_global={bool(p['share_global'])}")
        st = activity.describe(p, cfg.window_hours, now)
        if st["mode"] == "always":
            act = "always on"
        elif st["mode"] == "off":
            act = "off (no automatic sync)"
        elif st["active"]:
            act = f"active until {time.strftime('%Y-%m-%d %H:%M', time.localtime(st['active_until']))}"
        else:
            act = "inactive (idle)"
        typer.echo(f"  mode={st['mode']}  window={st['window_hours']:g}h  {act}")
        for r in db.execute("SELECT * FROM repos WHERE project_path=? ORDER BY path", (p["path"],)):
            nb = db.execute("SELECT count(DISTINCT branch) FROM branch_files WHERE repo_id=?", (r["id"],)).fetchone()[0]
            nf = db.execute("SELECT count(*) FROM branch_files WHERE repo_id=? AND branch=?", (r["id"], r["current_branch"])).fetchone()[0]
            nc = db.execute("SELECT count(DISTINCT c.id) FROM chunks c JOIN branch_files bf ON bf.blob_sha=c.blob_sha "
                            "WHERE bf.repo_id=? AND bf.branch=?", (r["id"], r["current_branch"])).fetchone()[0]
            masked, dropped = db.execute(
                "SELECT coalesce(sum(b.secret_masked),0), coalesce(sum(b.secret_skipped),0) FROM blobs b "
                "WHERE b.sha IN (SELECT blob_sha FROM branch_files WHERE repo_id=? AND branch=?)",
                (r["id"], r["current_branch"])).fetchone()
            ls = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["last_sync"])) if r["last_sync"] else "never"
            typer.echo(f"  {r['path']:<32} branch={r['current_branch'] or '?'}  manifests={nb}  files={nf}  chunks={nc}  "
                       f"masked={masked}  dropped={dropped}  last sync {ls}")
    typer.echo("\nmemories:")
    for r in db.execute("SELECT scope, source, count(*) n FROM memories WHERE superseded_by IS NULL GROUP BY scope, source ORDER BY scope"):
        typer.echo(f"  {r['scope']:<32} {r['source']:<7} {r['n']}")
    tot = db.execute("SELECT count(*), coalesce(sum(secret_skipped),0), coalesce(sum(secret_masked),0) FROM blobs").fetchone()
    typer.echo(f"\nblobs: {tot[0]}  chunks: {db.execute('SELECT count(*) FROM chunks').fetchone()[0]}  "
               f"masked secret values: {tot[2]}  skipped secret chunks: {tot[1]}")


@app.command(name="mode")
def mode_cmd(
    mode: Annotated[str, typer.Argument(metavar="auto|always|off", help="auto: sync while active; always; off")],
    project: Annotated[Optional[str], typer.Option("--project", help="Project path or directory (default: cwd's)")] = None,
    window_hours: Annotated[Optional[float], typer.Option("--window-hours", help="Activity window of this project")] = None,
):
    """Set the sync mode (and activity window) of a project."""
    from myvec import activity, daemon_client

    cfg, db = _open(vec=False)
    try:
        path = activity.resolve_project(db, project, Path.cwd())
        activity.set_mode(db, path, mode, window_hours)
    except ValueError as e:
        _fail(str(e))
    row = db.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
    daemon_client.request("POST", "/api/refresh", {})  # let a running daemon notice right away
    st = activity.describe(row, cfg.window_hours)
    typer.echo(f"project {path}: mode={st['mode']}, window={st['window_hours']:g}h")


# ------------------------------------------------------------------ daemon

@app.command()
def serve(port: Annotated[Optional[int], typer.Option(help="Port (default: daemon_port in config.toml, 7878)")] = None):
    """Run the local daemon in the foreground: warm search, file watchers, event bus, JSON API, web UI."""
    from myvec import daemon

    raise typer.Exit(daemon.serve(port))


def _service():
    """The daemon service manager of this OS: launchd on macOS, a systemd user unit on Linux."""
    if sys.platform == "darwin":
        from myvec import launchd
        return launchd, "launchctl"
    if sys.platform.startswith("linux"):
        from myvec import systemd
        return systemd, "systemctl"
    _fail(f"no daemon service support for {sys.platform}: run `myvec serve` yourself")


@app.command(name="install-daemon")
def install_daemon(
    no_load: Annotated[bool, typer.Option("--no-load", help="Only write the service file, do not load/enable it")] = False,
):
    """Install the daemon service and start it: a LaunchAgent (macOS) or a systemd user unit (Linux)."""
    mod, tool = _service()
    try:
        path, loaded = mod.install(load=not no_load)
    except RuntimeError as e:
        _fail(str(e))
    typer.echo(f"wrote {path}" + (f" and loaded it with {tool}" if loaded else ""))


@app.command(name="uninstall-daemon")
def uninstall_daemon():
    """Stop and remove the daemon service (LaunchAgent on macOS, systemd user unit on Linux)."""
    mod, tool = _service()
    path, removed = mod.uninstall()
    typer.echo(f"removed {path}" if removed else f"{path} not installed")


@app.command(name="context")
def context_cmd():
    """Short session-start context for the current directory (prints nothing outside a project)."""
    from myvec import context

    text = context.build(Path.cwd())
    if text:
        typer.echo(text)


@app.command(name="install-hooks")
def install_hooks(settings: Annotated[Optional[Path], typer.Option(help="settings.json to modify")] = None):
    """Merge myvec hooks into Claude Code settings.json (idempotent, with backup): SessionStart, PostToolUse
    (post-edit, AskUserQuestion, async heartbeat), UserPromptSubmit, Stop, Notification, SessionEnd, PreToolUse
    (AskUserQuestion; Bash|Grep search hint), SubagentStart (search hint for subagents). Other hooks are left untouched."""
    from myvec import hooks

    path = (settings or Path.home() / ".claude" / "settings.json").expanduser()
    try:
        changed, backup = hooks.install(path)
    except (ValueError, json.JSONDecodeError) as e:
        _fail(f"cannot update {path}: {e}")
    if not changed:
        typer.echo(f"hooks already installed in {path}")
    else:
        typer.echo(f"hooks installed in {path}" + (f" (backup: {backup})" if backup else ""))


def _age(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    return f"{sec // 3600}h" if sec < 172800 else f"{sec // 86400}d"


@app.command()
def sessions(all_: Annotated[bool, typer.Option("--all", help="Include sessions ended more than a day ago")] = False,
             json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
             ticket: Annotated[str | None, typer.Option("--ticket", help="Only sessions bound to this ticket ID (current project)")] = None):
    """Claude Code sessions: working / waiting / idle / ended (folds the hook journal when no daemon runs)."""
    from myvec import daemon_client
    from myvec import sessions as sess

    cfg = config.load_config()
    if not config.db_path().exists():
        typer.echo("[]" if json_out else "no sessions")
        return
    db = dbmod.connect(vec=False)
    sess.ensure_table(db)
    if not daemon_client.read_daemon():
        try:
            sess.fold(db, retention_days=cfg.session_retention_days)
        except Exception as e:  # noqa: BLE001
            typer.echo(f"warning: cannot fold session events: {e}", err=True)
    tkey = None
    if ticket:
        from myvec import gitutil

        cwd = str(Path.cwd())
        top = gitutil.run(cwd, "rev-parse", "--show-toplevel", check=False).decode(errors="replace").strip() or None
        cur = sess.project_of(db, cwd, top, sess.main_root_of(top))
        if not cur:
            typer.echo("--ticket: current directory is not in a registered project", err=True)
            raise typer.Exit(1)
        tkey = (cur, ticket)
    rows = sess.list_sessions(db, idle_min=cfg.session_idle_minutes, include_old_ended=all_, ticket=tkey)
    if json_out:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        typer.echo("no sessions")
        return
    for r in rows:
        q = (r["question"] or "").replace("\n", " ")
        q = q[:60] + "…" if len(q) > 60 else q
        tk = (r["ticket_id"] + ("*" if r["ticket_source"] == "claim" else "")) if r["ticket_id"] else "-"
        typer.echo(f"{r['session_id'][:8]}  {r['effective_state']:<8} {r['project_path'] or r['project_label'] or '-':<16} {tk:<10} "
                   f"{r['branch'] or '-':<28} {_age(r['age_s']):>5}  {q}".rstrip())


@app.command(name="hook", hidden=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def hook_cmd(ctx: typer.Context, which: str = typer.Argument("")):
    """Hook entrypoint (normally handled by the fast path in myvec.cli)."""
    from myvec import hooks

    raise typer.Exit(hooks.main([which]))


@app.command(name="measure", context_settings={"allow_extra_args": True, "ignore_unknown_options": True,
                                               "help_option_names": []})
def measure_cmd(ctx: typer.Context):
    """Run a command and measure it: `myvec measure [--ticket ID] [--agent NAME] [--label L] [--interval S] -- <cmd>`.
    Wall and CPU time of the whole process tree, average/peak cores, peak RSS, exit code; prints a summary line to
    stderr and records the run in `test_runs` (normally handled by the fast path in myvec.cli)."""
    from myvec import measure

    raise typer.Exit(measure.main(list(ctx.args)))


@app.command(name="scaffold")
def scaffold_cmd(
    answers: Annotated[Optional[Path], typer.Option("--answers", help="answers.json (see skill/myvector/bootstrap.md)")] = None,
    root: Annotated[Optional[Path], typer.Option("--root", help="Target directory (default: current)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan, write nothing")] = False,
    overwrite: Annotated[Optional[list[str]], typer.Option("--overwrite", help="Relative path to replace with the template; a .bak-<ts> is kept (repeatable)")] = None,
    keep: Annotated[Optional[list[str]], typer.Option("--keep", help="Relative path of a conflicting file to leave as is (repeatable)")] = None,
    append: Annotated[Optional[list[str]], typer.Option("--append", help="CLAUDE.md only: append the process section to the existing file (repeatable)")] = None,
    no_tickets: Annotated[bool, typer.Option("--no-tickets", help="Do not write anything under docs/todo/")] = False,
    ticket_template: Annotated[bool, typer.Option("--ticket-template", help="With --no-tickets (or foreign tickets): still write docs/todo/ticket-template.md")] = False,
    diff: Annotated[bool, typer.Option("--diff", help="With --dry-run: unified diff for conflict/merge rows (secrets masked)")] = False,
    detect: Annotated[bool, typer.Option("--detect", help="Print facts about the folder (stack, tests, state) and exit; writes nothing")] = False,
    show_state: Annotated[bool, typer.Option("--state", help="Print the process version stored in the project (.myvec/scaffold.json) and exit; writes nothing")] = False,
    update: Annotated[bool, typer.Option("--update", help="Re-render the process with the current templates and merge it 3-way into the project (state-based); shows the plan and diff, writes only with --write")] = False,
    adopt: Annotated[bool, typer.Option("--adopt", help="Project without a scaffold state: add only .claude/rules/search.md (plan without --write; the state is not written)")] = False,
    write: Annotated[bool, typer.Option("--write", help="With --update or --adopt: consent to write (conflicts are never written)")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print [{path, action}] (or the --detect / --state / --update report) as JSON")] = False,
):
    """Render the orchestrator process (CLAUDE.md, .claude/agents+rules, docs/todo, ...) into a project.
    `--adopt [--write]` (ADR-041): project without a state gets only .claude/rules/search.md (own file = foreign, never replaced).
    Exit codes: 0 ok, 2 invalid input or a secret in the answers, 3 conflicts or the folder changed after the plan
    (nothing is written). `--update [--write]` (ADR-035): 3-way merge of a new render into a project with a scaffold state;
    exit 0 preview/written/already current, 2 no or broken state (`none`, `invalid`, `newer`), invalid answers, no git,
    forbidden flags, 3 unresolved conflicts (resolve with --keep/--overwrite PATH) or the folder changed."""
    from myvec import scaffold
    from myvec import ticketops

    target = (root or Path.cwd()).resolve()
    if write and not (update or adopt):
        _fail("--write works only with --update or --adopt", 2)
    if adopt:
        if update or answers or overwrite or keep or append or no_tickets or ticket_template or detect or show_state or dry_run or diff:
            _fail("--adopt cannot be combined with --update, --answers, --detect, --state, --overwrite, --keep, --append, "
                  "--no-tickets, --ticket-template, --dry-run or --diff", 2)
        _scaffold_adopt(target, write, as_json)
        return
    if update:
        if append or no_tickets or ticket_template or detect or show_state or dry_run or diff:
            _fail("--update cannot be combined with --append, --no-tickets, --ticket-template, --detect, --state, --dry-run or --diff", 2)
        _scaffold_update(target, answers, overwrite, keep, write, as_json)
        return
    if show_state:
        if answers or overwrite or keep or append or no_tickets or ticket_template or diff or dry_run or detect:
            _fail("--state cannot be combined with --answers, --detect, --dry-run or the other flags", 2)
        from myvec.scaffold import state as st

        rep = st.read(target)
        if as_json:
            typer.echo(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            typer.echo(f"status: {rep['status']}")
            typer.echo(f"version: {rep['version'] if rep['version'] is not None else 'unknown'} (current {rep['current']})")
            typer.echo(f"files: {len(rep['files'])}")
            if rep["reason"]:
                typer.echo(f"reason: {rep['reason']}")
        return
    if detect:
        if answers or overwrite or keep or append or no_tickets or ticket_template or diff or dry_run:
            _fail("--detect cannot be combined with --answers, --dry-run or the conflict flags", 2)
        from myvec.scaffold import detect as det

        rep = det.detect(target)
        if as_json:
            typer.echo(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            st = rep["state"]
            typer.echo(f"project: {rep['answers'].get('project')}")
            for k in ("stack", "test_cmd", "lint_cmd"):
                typer.echo(f"{k}: {rep['answers'].get(k, '-')}")
            typer.echo(f"ui: {rep['ui']}")
            typer.echo(f"docs language: {rep['docs_language_detected']}")
            typer.echo(f"marker: {st['marker']}")
            typer.echo(f"process: {st['process']}")
            typer.echo(f"tickets: {st['tickets']}")
            typer.echo(f"git: {st['git']}")
            typer.echo("unknown: " + ", ".join(u["key"] for u in rep["unknown"]))
        return
    if answers is None:
        _fail("--answers is required (or use --detect)", 2)
    if diff and not dry_run:
        _fail("--diff works only with --dry-run", 2)

    def rels(xs):
        return {o[2:] if o.startswith("./") else o for o in (xs or [])}

    ow, kp, ap = rels(overwrite), rels(keep), rels(append)
    try:
        a, raw = scaffold.load_answers_raw(answers)
        tickets = "no" if no_tickets else None
        state = "none"
        if tickets is None:
            from myvec.scaffold import detect as det

            state = det._tickets(target)["state"]
            if state == "foreign":
                tickets = "foreign"
        # an existing project (its own tickets, or --no-tickets) gets no first group
        rendered = scaffold.render(a, with_group=tickets is None and state == "none")
        pl = scaffold.plan(target, rendered, a, tickets, with_template=ticket_template)
        scaffold.check_choices(target, pl, rendered, {"--overwrite": ow, "--keep": kp, "--append": ap})
        scaffold.with_state(target, pl, rendered, raw, scaffold.labelled(pl, ow, kp, ap), a)
    except (scaffold.ScaffoldError, ticketops.TicketOpError) as e:
        _fail(str(e), 2)

    def show(rows, extra=None):
        if as_json:
            out = []
            for r, act in rows:
                d = {"path": r, "action": act}
                d.update((extra or {}).get(r, {}))
                out.append(d)
            typer.echo(json.dumps(out, ensure_ascii=False))
        else:
            for r, act in rows:
                typer.echo(f"{act:<12} {r}")
                if extra and "diff" in extra.get(r, {}):
                    typer.echo(extra[r]["diff"].rstrip("\n"))
                elif extra and extra.get(r, {}).get("diff_withheld"):
                    typer.echo(f"(diff withheld: {extra[r]['note']})" if "note" in extra[r]
                               else "(diff withheld: the file contains a secret)")

    bad = scaffold.conflicts(pl, ow, kp, ap)
    if dry_run:
        rows = scaffold.labelled(pl, ow, kp, ap)
        try:
            extra = scaffold.diffs(target, rows, rendered, a, ap) if diff else None
        except (scaffold.ScaffoldError, OSError) as e:
            _fail(str(e), 2)
        show(rows, extra)
        raise typer.Exit(3 if bad else 0)
    if bad:
        show(pl)
        typer.echo("error: conflicts, nothing written (pass --overwrite, --keep or --append PATH for each file): "
                   + ", ".join(bad), err=True)
        raise typer.Exit(3)
    try:
        done = scaffold.apply(target, pl, rendered, ow, a, kp, ap)
        errs = [] if tickets else scaffold.regenerate_index(target)
    except scaffold.ScaffoldChanged as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    except (scaffold.ScaffoldError, ticketops.TicketOpError, OSError) as e:
        _fail(str(e), 2)
    show(done)
    if errs:
        for e in errs:
            typer.echo(f"error: {e}", err=True)
        _fail("files written, but the ticket tree has errors (index not generated)", 1)


def _scaffold_adopt(target: Path, write: bool, as_json: bool) -> None:
    from myvec import scaffold
    from myvec.scaffold import adopt as ad

    try:
        rep = ad.run(target, write)
    except scaffold.ScaffoldChanged as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    except (scaffold.ScaffoldError, OSError) as e:
        _fail(str(e), 2)
    if as_json:
        typer.echo(json.dumps({k: rep[k] for k in ("status", "files", "write")}, ensure_ascii=False, indent=2))
        return
    for f in rep["files"]:
        typer.echo(f"{f['action']:<12} {f['path']}")
    if rep["status"] == "preview":
        typer.echo(rep["text"].rstrip("\n"))
        if rep["files"][0]["action"] == "update":
            typer.echo(f"the search rule is an older myvec version (v{rep['version']}, unchanged): rerun with --write to update it")
        else:
            typer.echo("preview only, nothing written: rerun with --write")
    elif rep["status"] == "written" and rep["backups"]:
        typer.echo("the previous file is kept as " + ", ".join(rep["backups"]) + " (remove it when not needed)")
    elif rep["status"] == "foreign":
        typer.echo("the project has its own search rule: left as is")
        typer.echo("it differs from every myvec version (edited or your own): merge the new text by hand")
    elif rep["status"] == "current":
        typer.echo("the search rule is already in place")


def _scaffold_update(target: Path, answers, overwrite, keep, write: bool, as_json: bool) -> None:
    from myvec import scaffold
    from myvec.scaffold import state as st
    from myvec.scaffold import update as up_

    def rels(xs):
        return {o[2:] if o.startswith("./") else o for o in (xs or [])}

    rep, data = st.load(target)
    if data is None:
        why = {"none": "no scaffold state here: run the bootstrap first (skill /myvector, bootstrap.md, \"Existing code\")",
               "newer": "the project state is newer than this myvec: update myvec",
               "invalid": f"the scaffold state is invalid: {rep['reason']}"}[rep["status"]]
        _fail(why, 2)
    try:
        if answers is not None:
            a, raw = scaffold.load_answers_raw(answers)
        else:
            raw = data["answers"]
            try:
                a = scaffold.parse_answers(raw)
            except scaffold.ScaffoldError as e:
                raise scaffold.ScaffoldError(f"the saved answers are not valid for this version ({e}); pass --answers FILE") from None
        up = up_.plan_update(target, data, a, raw, rels(overwrite), rels(keep))
    except (scaffold.ScaffoldError, OSError) as e:
        _fail(str(e), 2)
    backups: list[str] = []
    changed = False
    if write and not up.conflicts:
        try:
            backups = up_.execute(target, up)
        except scaffold.ScaffoldChanged as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(3)
        except (scaffold.ScaffoldError, OSError) as e:
            _fail(str(e), 2)
        changed = bool(up.write)
    stat = up_.status(up, changed)
    if as_json:
        typer.echo(json.dumps(up_.report(up, stat, backups), ensure_ascii=False, indent=2))
    else:
        typer.echo(f"process v{up.from_version} -> v{up.to_version}")
        for r, act in up.rows:
            typer.echo(f"{act:<12} {r}")
            d = up.diffs.get(r, {})
            if "diff" in d:
                typer.echo(d["diff"].rstrip("\n"))
            elif "template_diff" in d:
                typer.echo("(the template changed:)\n" + d["template_diff"].rstrip("\n"))
            elif d.get("diff_withheld"):
                typer.echo(f"(diff withheld: {d['note']})" if "note" in d else "(diff withheld: the file contains a secret)")
        if backups:
            typer.echo("backups kept next to replaced files (git-ignored and not indexed): " + ", ".join(backups))
        if stat == "preview":
            typer.echo("preview only, nothing written: rerun with --write")
        elif stat == "current":
            typer.echo("the process is already up to date")
    if up.conflicts:
        typer.echo("error: conflicts, nothing written (pass --keep or --overwrite PATH for each file): "
                   + ", ".join(up.conflicts), err=True)
        raise typer.Exit(3)


from myvec.ticketcli import ticket_app  # noqa: E402

app.add_typer(ticket_app, name="ticket")

from myvec.tokencli import tokens_app  # noqa: E402

app.add_typer(tokens_app, name="tokens")

from myvec.searchstatscli import searches_app  # noqa: E402

app.add_typer(searches_app, name="searches")
