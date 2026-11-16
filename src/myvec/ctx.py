"""Where is the user? Resolve cwd -> project / repo / branch."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from myvec import gitutil, projects


@dataclass
class Ctx:
    marker: projects.Marker
    repo_id: int | None
    repo_path: str       # path of the repo containing cwd (== project path for the root/pseudo repo)
    repo_dir: Path | None
    branch: str | None

    @property
    def project_path(self) -> str:
        return self.marker.path


def _under(cwd: Path, d: Path) -> bool:
    return cwd == d or d in cwd.parents


def _checkout_dir(cwd: Path, rdir: Path) -> Path:
    """Nearest dir at or above cwd (not above the repo dir) with its own `.git`: a linked worktree nested in the
    repo (`.worktrees/<id>`) has its own HEAD, the main checkout's would show the wrong branch."""
    for d in [cwd, *cwd.parents]:
        if d == rdir or rdir not in d.parents:
            break
        if (d / ".git").exists():
            return d
    return rdir


def current(db: sqlite3.Connection | None, cwd: str | Path) -> Ctx | None:
    cwd = Path(cwd).resolve()
    try:
        marker = projects.find_marker(cwd)
    except projects.InvalidMarker:
        return None
    if marker is None:
        return None
    best: tuple[int | None, str, Path] | None = None
    rows = []
    if db is not None:
        try:
            rows = db.execute("SELECT id, path, dir FROM repos WHERE project_path=?", (marker.path,)).fetchall()
        except sqlite3.Error:
            rows = []
    cands = [(r["id"], r["path"], Path(r["dir"])) for r in rows]
    if not cands:
        specs, _ = projects.discover(marker)
        cands = [(None, s.path, s.dir) for s in specs]
    for rid, rpath, rdir in cands:
        if _under(cwd, rdir) and (best is None or len(rdir.parts) > len(best[2].parts)):
            best = (rid, rpath, rdir)
    if best is None:
        best = (None, marker.path, marker.root)
    rid, rpath, rdir = best
    branch = gitutil.head_branch(_checkout_dir(cwd, rdir)) if gitutil.is_repo(rdir) else "-"
    return Ctx(marker, rid, rpath, rdir, branch)


def branch_visible(c: Ctx | None, row) -> bool:
    """Branch-scoped memories (`remember --branch`) are only visible on that branch (and in that repo)."""
    keys = row.keys()
    branch = row["branch"] if "branch" in keys else None
    if not branch:
        return True
    if c is None or c.branch != branch:
        return False
    origin = row["origin_repo"] if "origin_repo" in keys else None
    return not origin or c.repo_path == origin
