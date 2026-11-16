"""`myvec context`: <= ~300 tokens for session start. Stdlib sqlite only (no vec extension)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from myvec import config, ctx as ctxmod, projects
from myvec.hooks import SEARCH_HINT

MAX_CHARS = 1200


def build(cwd: str | Path) -> str:
    dbp = config.db_path()
    db = None
    if dbp.exists():
        try:
            db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=2.0)
            db.row_factory = sqlite3.Row
        except sqlite3.Error:
            db = None
    try:
        c = ctxmod.current(db, cwd)
        if c is None:
            return ""
        head = [f"[myvec] project: {c.project_path} | repo: {c.repo_path} | branch: {c.branch or '-'}", SEARCH_HINT]
        lines: list[str] = []
        mems: list[sqlite3.Row] = []
        glob: list[sqlite3.Row] = []
        if db is not None:
            try:
                scopes = projects.ancestors(c.repo_path)
                marks = ",".join("?" * len(scopes))
                mems = db.execute(
                    f"SELECT * FROM memories WHERE scope IN ({marks}) AND superseded_by IS NULL AND source='vault' "
                    "ORDER BY pinned DESC, updated DESC LIMIT 30", scopes).fetchall()
                mems = [m for m in mems if ctxmod.branch_visible(c, m)][:5]
                if c.marker.share_global:
                    glob = db.execute("SELECT * FROM memories WHERE scope='global' AND pinned=1 AND superseded_by IS NULL "
                                      "ORDER BY updated DESC LIMIT 3").fetchall()
            except sqlite3.Error:
                pass
        if glob:
            lines.append("Global (pinned):")
            lines += [_line(m) for m in glob]
        if mems:
            lines.append("Project memories:")
            lines += [_line(m) for m in mems]
        if mems or glob:
            lines.append("`myvec remember \"<text>\"` to save.")
        head_s = "\n".join(head)
        rest = "\n".join(lines)
        if not rest:
            return head_s
        return head_s + "\n" + rest[:max(0, MAX_CHARS - len(head_s) - 1)]
    finally:
        if db is not None:
            db.close()


def _line(m: sqlite3.Row) -> str:
    txt = " ".join(m["text"].split())
    pin = "*" if m["pinned"] else "-"
    return f"{pin} {m['id']} ({(m['updated'] or '')[:10]}): {txt[:170]}{'...' if len(txt) > 170 else ''}"
