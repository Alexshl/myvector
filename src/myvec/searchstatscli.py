"""`myvec searches`: share of myvec versus self-search per project (ADR-029)."""
from __future__ import annotations

import json
import os
from typing import Annotated, Optional

import typer

from myvec import config, searchstats
from myvec import tokens as T
from myvec.tokencli import _fail, _maybe_scan, _open_index

searches_app = typer.Typer(help="myvec versus grep: share of searches per project.", add_completion=False)

HEADS = ["проєкт", "сесій", "myvec", "recall", "self", "self у суб.", "частка", "розвідка", "частка (розв.)", "класиф.", "промахи", "у лозі", "порожніх", "FTS-fallback", "p50 мс", "p50 доба", "до відліку"]


def _n(v) -> str:
    return f"{v:,}".replace(",", " ") if isinstance(v, int) else "–"


def _pct(v) -> str:
    return "–" if v is None else f"{round(v * 100)}%"


def _row(d: dict) -> list[str]:
    share = _pct(d["share"])
    k = d["self_by_kind"]
    expl = k["explore"] + k["exact"] + k["unclassified"]
    p50 = "–" if d["p50_ms"] is None else _n(round(d["p50_ms"]))
    p24 = "–" if d["p50_ms_24h"] is None else _n(round(d["p50_ms_24h"]))
    return [d["project"], _n(d["sessions"]), _n(d["myvec"]), _n(d["recall"]), _n(d["self"]), _n(d["self_sub"]), share,
            _n(expl), _pct(d["share_explore"]), _pct(d["classified"]), _n(d["miss"]), _n(d["logged"]), _n(d["zero_results"]), _n(d["fts_fallback"]), p50, p24, _n(d["pre_onset"])]


def resolve_project(idx, value: str) -> str | None:
    """Project name for `--project`: '.', a path inside a project (also a worktree) or the project name itself."""
    from myvec import sessions

    if idx is None:
        return None
    if value != "." and idx.execute("SELECT 1 FROM projects WHERE path=?", (value,)).fetchone():
        return value
    if value == ".":
        cands = [os.getcwd()]
        pwd = os.environ.get("PWD")   # getcwd is already real; PWD may keep the symlinked spelling
        try:
            if pwd and os.path.isabs(pwd) and os.path.samefile(pwd, cands[0]):
                cands.insert(0, os.path.abspath(pwd))
        except OSError:
            pass
    else:
        p = os.path.abspath(os.path.expanduser(value))
        if not os.path.exists(p):
            return None   # a mistyped name must not resolve to the cwd project
        cands = [p, os.path.realpath(p)]   # as written after `relocate`, then the real path
    seen: list[str] = []
    for p in cands:
        if p in seen:
            continue
        seen.append(p)
        base = p.split("/.worktrees/")[0] if "/.worktrees/" in p else None
        hit = sessions.project_of(idx, p, base, sessions.main_root_of(p))
        if hit:
            return hit
    # the root may be recorded through a symlink (after `relocate`): compare real paths
    real = os.path.realpath(cands[-1])
    best: tuple[int, str] | None = None
    for path, root in idx.execute("SELECT path, root FROM projects"):
        r = os.path.realpath(root)
        if (real == r or real.startswith(r.rstrip(os.sep) + os.sep)) and (best is None or len(r) > best[0]):
            best = (len(r), path)
    return best[1] if best else None


def render(res: dict) -> str:
    body = [_row(d) for d in res["projects"]] + [_row({**res["total"], "project": "усього"})]
    widths = [max(len(x[i]) for x in [HEADS] + body) for i in range(len(HEADS))]
    lines = []
    for n, r in enumerate([HEADS] + body):
        lines.append("  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))))
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    t = res["total"]
    lines.append(f"сесій з myvec серед розвідувальних: {_n(t['sessions_myvec_explore'])} / {_n(t['sessions_explore'])}")
    k = t["self_by_kind"]
    lines.append(f"self за підвидом: логи {_n(k['logs'])}, підрахунок {_n(k['count'])}, файл {_n(k['file'])}, "
                 f"розвідка {_n(k['explore'])}, точний {_n(k['exact'])}, за іменем {_n(k['name'])}")
    lines.append(f"читання доків: Read {_n(t['doc_reads'])}, grep по доках {_n(t['self_doc'])}, "
                 f"myvec --kind docs {_n(t['myvec_docs'])}")
    lines.append(f"некласифіковано: {_n(k['unclassified'])} (транскрипти видалено або нерозбірна команда); "
                 "у частці (розв.) вони в знаменнику")
    lines.append(f"не враховано: {_n(t['pre_onset'])} до відліку, {_n(t['foreign'])} з іншої ОС")
    col = res["collector"]
    if not col["enabled"]:
        lines.append("колектор токенів вимкнено: self-пошуки не рахуються")
    elif col["backfill"]:
        lines.append(f"колектор ще дочитує транскрипти ({_n(col['pending_bytes'] // 1024)} КБ): числа неповні")
    return "\n".join(lines)


@searches_app.callback(invoke_without_command=True)
def searches_cmd(
    days: Annotated[int, typer.Option("--days", min=1, max=365, help="Window in days.")] = 14,
    project: Annotated[Optional[str], typer.Option("--project", help="Project path or '.'.")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    no_scan: Annotated[bool, typer.Option("--no-scan", help="Do not read new transcript lines first.")] = False,
):
    _maybe_scan(no_scan)
    idx = _open_index()
    tdb = T.open_db(readonly=True) if config.tokens_db_path().exists() else None
    try:
        res = searchstats.usage(idx, tdb, days)
        name = resolve_project(idx, project) if project is not None else None
    finally:
        for c in (idx, tdb):
            if c is not None:
                c.close()
    if project is not None:
        if name is None:
            _fail(f"{project!r} is neither a registered project name nor an existing path inside one (see `myvec projects`)", 1)
        res["projects"] = [d for d in res["projects"] if d["project"] == name] or [searchstats.blank_row(name)]
    if json_out:
        typer.echo(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        typer.echo(render(res))
