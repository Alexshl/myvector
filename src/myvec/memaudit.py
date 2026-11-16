"""Read-only audit of the memory composition: aggregates by default, per-memory signals only on request.

Never writes (the caller passes a `mode=ro` connection) and never calls the embedder: vectors come from `memories_vec`.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from pathlib import Path

from myvec import filters

OTHER_SCOPE_COS = 0.75   # a foreign scope counts as "similar" at or above this cosine
FIRST_LINE_MAX = 100
_IMPORT_KINDS = {"CLAUDE.md": "CLAUDE.md", "GRAPH_REPORT.md": "GRAPH_REPORT.md"}
_REFS = re.compile(r"/\w|`|\b\w[\w-]*\.(?:py|js|ts|tsx|md|toml|json|ya?ml|sh|sql|html|css|rs|go)\b")


def _kind(scope: str) -> str:
    return "global" if scope == "global" else "project"


def _import_type(vault_file: str | None) -> str:
    name = Path(vault_file).name if vault_file else ""
    return _IMPORT_KINDS.get(name, "auto-memory" if name else "other")


def _body_hash(text: str) -> str:
    t = text.strip()
    body = t.split("\n", 1)[1] if "\n" in t else ""
    return hashlib.sha1(body.strip().encode("utf-8")).hexdigest()


def audit(db: sqlite3.Connection) -> dict:
    """Aggregates only: no memory text, scope names or paths."""
    rows = db.execute("SELECT scope, source, superseded_by, pinned, branch, created, text, vault_file FROM memories").fetchall()
    by_source: Counter = Counter()
    archived: Counter = Counter()
    scope_sizes: Counter = Counter()
    months: Counter = Counter()
    imp_types: Counter = Counter()
    dup: dict[str, set[str]] = {}
    pinned = branch = 0
    for r in rows:
        key = f"{r['source']}/{_kind(r['scope'])}"
        if r["superseded_by"] is not None:
            archived[key] += 1
            continue
        by_source[key] += 1
        scope_sizes[r["scope"]] += 1
        months[(r["created"] or "")[:7] or "unknown"] += 1
        pinned += bool(r["pinned"])
        branch += bool(r["branch"])
        if r["source"] == "import":
            imp_types[_import_type(r["vault_file"])] += 1
            dup.setdefault(_body_hash(r["text"]), set()).add(r["scope"])
    groups = [s for s in dup.values() if len(s) > 1]
    return {
        "active": dict(sorted(by_source.items())),
        "active_total": sum(by_source.values()),
        "archived": dict(sorted(archived.items())),
        "scopes": len(scope_sizes),
        "scope_sizes": sorted(scope_sizes.values(), reverse=True),
        "pinned": pinned,
        "with_branch": branch,
        "by_month": dict(sorted(months.items())),
        "import_by_type": dict(sorted(imp_types.items())),
        "import_duplicate_groups": len(groups),
        "import_duplicate_max_scopes": max((len(s) for s in groups), default=0),
    }


def _first_line(text: str) -> str:
    line = (text.strip().splitlines() or [""])[0]
    if filters.has_hard_secret(line):
        return "[hidden]"
    # mask the whole line before cutting: a cut can break a token pattern
    return filters.mask_secrets(line)[0][:FIRST_LINE_MAX]


def candidates(db: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Active vault project memories with signals that they might belong in global. Local owner view only."""
    import numpy as np

    rows = db.execute("SELECT n, id, scope, text FROM memories WHERE superseded_by IS NULL "
                      "AND source IN ('vault','import')").fetchall()
    vecs: dict[int, object] = {}
    for r in rows:
        e = db.execute("SELECT embedding FROM memories_vec WHERE rowid=?", (r["n"],)).fetchone()
        if e:
            v = np.frombuffer(e[0], dtype=np.float32)
            nrm = float(np.linalg.norm(v))
            if nrm > 0:
                vecs[r["n"]] = v / nrm
    others = [r for r in rows if r["scope"] != "global" and r["n"] in vecs]
    out = []
    for r in db.execute("SELECT n, id, scope, text FROM memories WHERE superseded_by IS NULL AND source='vault' "
                        "AND scope!='global'").fetchall():
        best, per_scope = 0.0, {}
        if r["n"] in vecs:
            for o in others:
                if o["scope"] == r["scope"] or o["n"] == r["n"]:
                    continue
                c = float(np.dot(vecs[r["n"]], vecs[o["n"]]))
                per_scope[o["scope"]] = max(per_scope.get(o["scope"], -1.0), c)
                best = max(best, c)
        scope_leaf = Path(r["scope"]).name
        refs = bool(_REFS.search(r["text"])) or (bool(scope_leaf) and scope_leaf.lower() in r["text"].lower())
        out.append({"id": r["id"], "scope": r["scope"], "first": _first_line(r["text"]),
                    "max_cos_other": round(best, 3),
                    "other_scopes_075": sum(1 for c in per_scope.values() if c >= OTHER_SCOPE_COS),
                    "has_project_refs": refs})
    out.sort(key=lambda d: (-d["other_scopes_075"], -d["max_cos_other"]))
    return out[:limit]
