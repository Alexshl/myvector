"""Cross-project knowledge (E8-05, ADR-030): "did we do this anywhere else?".

- `shared = false` in `.myvector.toml` hides a project (code + memories, with its descendants) from `--scope all`,
  `--by-project`, the UI search and `--project` of other project families. Read from the marker at search time
  (no DB column); a broken marker hides the project (fail-closed).
- `summarize` groups the `scope=all` hits by top-level project; `target_ctx` makes the context of another project
  for the drill-down (`--project`).
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import tomllib
from pathlib import Path

from myvec import activity, projects

BY_PROJECT_LIMIT = 6
TOP_HITS = 2
_CACHE: dict[str, tuple[int, bool]] = {}   # marker file -> (mtime_ns, shared)


class CrossError(Exception):
    """A user-facing error of --project / --by-project (the CLI prints it with exit code 1)."""


def is_shared(root: str | Path) -> bool:
    """`shared` of the project marker at `root`. No marker -> True (everything is shared by default);
    unparsable TOML or a non-bool `shared` -> False (fail-closed). Cached by the marker's mtime."""
    f = Path(root) / projects.MARKER
    try:
        mt = f.stat().st_mtime_ns
    except OSError:
        return True
    key = str(f)
    hit = _CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        v = tomllib.loads(f.read_text(encoding="utf-8")).get("shared", True)
        shared = v if isinstance(v, bool) else False
    except Exception:  # noqa: BLE001 - any read/parse failure hides the project
        shared = False
    _CACHE[key] = (mt, shared)
    return shared


def under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def is_hidden(path: str, hidden) -> bool:
    return any(under(path, h) for h in hidden)


def hidden_projects(db: sqlite3.Connection, ctx) -> set[str]:
    """Paths of registered projects with `shared = false`, except those of the family of `ctx` (it sees itself)."""
    own = projects.top_level(ctx.project_path) if ctx is not None else None
    out: set[str] = set()
    for r in db.execute("SELECT path, root FROM projects"):
        if own is not None and projects.top_level(r["path"]) == own:
            continue
        if not is_shared(r["root"]):
            out.add(r["path"])
    return out


def target_ctx(db: sqlite3.Connection, arg: str, cwd: str | Path):
    """(ctx of the project `arg`, hidden set of the viewer in `cwd`) for `--project`. The ctx has no global memory."""
    from myvec import ctx as ctxmod

    if (not db.execute("SELECT 1 FROM projects WHERE path=?", (arg,)).fetchone() and not arg.startswith("~")
            and not Path(arg).is_absolute()):
        arg = str(Path(cwd) / arg)   # a relative path is relative to the caller's cwd (the daemon has another one)
    try:
        path = activity.resolve_project(db, arg, cwd)
    except ValueError as e:
        raise CrossError(str(e)) from None
    row = db.execute("SELECT root FROM projects WHERE path=?", (path,)).fetchone()
    if row is None:
        raise CrossError(f"unknown project {arg!r}")
    root = Path(row["root"])
    if not root.is_dir():
        raise CrossError(f"project {path} root {root} does not exist on this machine")
    viewer = ctxmod.current(db, cwd)
    hidden = hidden_projects(db, viewer)
    if is_hidden(path, hidden):
        raise CrossError(f"project {path} opted out of cross-project search (shared = false)")
    c = ctxmod.current(db, root)
    if c is None:
        raise CrossError(f"project {path}: cannot read {projects.MARKER}")
    return dataclasses.replace(c, marker=dataclasses.replace(c.marker, share_global=False)), hidden


def summarize(db: sqlite3.Connection, cfg, query: str, ctx, kind: str, qvec) -> dict:
    """Group the `scope=all` hits by top-level project. The pool is the usual TOP per list; code and memories are
    searched separately so that `max_memory_hits` does not eat the memories of other projects."""
    from myvec import search as S

    hits = []
    if kind in ("code", "docs", "all"):
        hits += S.search(db, cfg, None, query, ctx, k=200, scope="all", kind="docs" if kind == "docs" else "code",
                         qvec=qvec)
    if kind in ("memory", "all"):
        hits += S.search(db, cfg, None, query, ctx, k=50, scope="all", kind="memory", qvec=qvec)
    hits.sort(key=lambda h: -h.score)
    repo_project = {r["id"]: r["project_path"] for r in db.execute("SELECT id, project_path FROM repos")}
    own = projects.top_level(ctx.project_path) if ctx is not None else None
    groups: dict[str, dict] = {}
    current = 0
    for h in hits:
        if h.kind == "code":
            pp = repo_project.get(h.data["repo_id"])
        else:
            pp = None if h.data["scope"] == "global" else h.data["scope"]
        if pp is None:
            continue
        top = projects.top_level(pp)
        if top == own:
            current += 1
            continue
        g = groups.setdefault(top, {"project": top, "best": h.score, "code": 0, "memory": 0, "hits": []})
        g[h.kind] += 1
        g["hits"].append(h)
    ranked = sorted(groups.values(), key=lambda g: -g["best"])
    out = []
    for g in ranked[:BY_PROJECT_LIMIT]:
        out.append({"project": g["project"], "best": round(g["best"], 6), "code": g["code"], "memory": g["memory"],
                    "top": S.to_json(g["hits"][:TOP_HITS])})
    return {"query": query, "kind": kind, "total": len(ranked), "pool": S.TOP,
            "current": {"project": own, "hits": current} if own is not None else None,
            "projects": out}


def format_summary(s: dict, budget_tokens: int) -> str:
    """Text of the summary (same style as the other CLI output), within the token budget."""
    if not s["projects"] and not (s["current"] and s["current"]["hits"]):
        return "no results"
    lines = [f"found in {s['total']} projects (top {s['pool']} candidates per list):"]
    used = len(lines[0]) // 4 + 1
    for i, p in enumerate(s["projects"], 1):
        block = [f"{i}. {p['project']}  best {p['best']:.3f} · code {p['code']} · memory {p['memory']}"]
        for t in p["top"]:
            if t["kind"] == "code":
                block.append(f"   {t['repo']} {t['path']}:{t['start']}-{t['end']}"
                             + (f" {t['symbol']}" if t.get("symbol") else "") + f" ({t['score']:.3f})")
            else:
                txt = " ".join(t["text"].split())
                block.append(f"   [M] {t['date']} {t['id']}: {txt[:160]}")
        cost = (sum(len(x) + 1 for x in block)) // 4 + 1
        if i > 1 and used + cost > budget_tokens:
            break
        lines += block
        used += cost
    cur = s["current"]
    if cur and cur["hits"]:
        lines.append(f"current project ({cur['project']}): {cur['hits']} more")
    if s["projects"]:
        lines.append('next: myvec search --project <name> "<query>"')
    return "\n".join(lines)


def to_json(s: dict) -> str:
    out = {"query": s["query"], "kind": s["kind"], "current": s["current"], "projects": s["projects"]}
    return json.dumps(out, ensure_ascii=False, indent=2)
