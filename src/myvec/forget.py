"""`myvec forget`: remove projects whose root vanished from the index and the registry (ADR-044).

Never automatic: a missing root is indistinguishable from an unmounted disk or an index copied from another OS.
Reads are side-effect free (`plan`); `apply` is called by the CLI under the sync lock."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from myvec import config, indexer, searchstats

WORKTREES_DIR = ".worktrees"


@dataclass
class Item:
    root: str
    state: str                    # present | gone | unreachable
    project: str | None           # projects.path; None for a registry-only root
    in_registry: bool
    worktree: bool = False
    repos: int = 0
    files: int = 0
    chunks: int = 0
    tickets: int = 0
    children: int = 0


def classify(root: str) -> str:
    if os.path.isdir(root):
        return "present"
    if root.startswith(searchstats.FOREIGN_PREFIX):
        return "unreachable"
    parent = os.path.dirname(root)
    if os.path.basename(parent) == WORKTREES_DIR:
        parent = os.path.dirname(parent)  # the main checkout must be in place
    # an empty parent is most likely the mount point of an unmounted disk
    return "gone" if os.path.isdir(parent) and _nonempty(parent) else "unreachable"


def _nonempty(d: str) -> bool:
    try:
        with os.scandir(d) as it:
            return next(it, None) is not None
    except OSError:
        return False


def _is_worktree(root: str) -> bool:
    return os.path.basename(os.path.dirname(root)) == WORKTREES_DIR


def _norm(root: str) -> str:
    return os.path.abspath(root)


def _count(db: sqlite3.Connection, item: Item) -> None:
    p = item.project
    if p is None:
        return
    ids = [r[0] for r in db.execute("SELECT id FROM repos WHERE project_path=?", (p,))]
    item.repos = len(ids)
    if ids:
        q = ",".join("?" * len(ids))
        item.files = db.execute(f"SELECT count(*) FROM branch_files WHERE repo_id IN ({q})", ids).fetchone()[0]
        item.chunks = db.execute(f"SELECT count(*) FROM chunk_repo WHERE repo_id IN ({q})", ids).fetchone()[0]
    item.tickets = db.execute("SELECT count(*) FROM tickets WHERE project_path=?", (p,)).fetchone()[0]
    item.children = db.execute("SELECT count(*) FROM projects WHERE path LIKE ? ESCAPE '\\'",
                               (p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%",)).fetchone()[0]


def all_items(db: sqlite3.Connection, counts: bool = True) -> list[Item]:
    registry = set(config.load_registry())
    items: list[Item] = []
    seen: set[str] = set()
    for r in db.execute("SELECT path, root FROM projects ORDER BY path"):
        root = _norm(r["root"])
        seen.add(root)
        items.append(Item(root, classify(root), r["path"], root in registry or r["root"] in registry,
                          _is_worktree(root)))
    for root in sorted(registry):
        n = _norm(root)
        if n in seen:
            continue
        items.append(Item(root, classify(n), None, True, _is_worktree(n)))
    if counts:
        for it in items:
            _count(db, it)
    return items


def plan(db: sqlite3.Connection, target: str | None = None, missing: bool = False,
         items: list[Item] | None = None) -> list[Item]:
    """Read-only. Raises LookupError for an unknown target, ValueError for a bad argument combination."""
    if bool(target) == bool(missing):
        raise ValueError("give exactly one of TARGET or --missing")
    if items is None:
        items = all_items(db)
    if missing:
        return [i for i in items if i.state == "gone"]
    norm = _norm(target)
    hit = [i for i in items if i.project == target] or [i for i in items if _norm(i.root) == norm]
    if not hit and os.path.exists(target):  # a symlinked or otherwise spelled path to an existing root
        real = os.path.realpath(target)
        hit = [i for i in items if os.path.realpath(i.root) == real]
    if not hit:
        raise LookupError(f"unknown project or root: {target}")
    return hit


def missing_summary(db: sqlite3.Connection) -> tuple[int, int, int]:
    """(gone roots, of them worktrees, unreachable roots) over distinct roots."""
    by_root: dict[str, Item] = {}
    for it in all_items(db, counts=False):
        by_root.setdefault(_norm(it.root), it)
    gone = [i for i in by_root.values() if i.state == "gone"]
    return len(gone), sum(1 for i in gone if i.worktree), sum(1 for i in by_root.values() if i.state == "unreachable")


def hint(db: sqlite3.Connection) -> str | None:
    gone, wt, unreach = missing_summary(db)
    if not gone and not unreach:
        return None
    return (f"registry: {gone} roots gone from disk ({wt} worktrees), {unreach} unreachable - "
            "run `myvec forget-project --missing` (dry run)")


def sweep_preview(db: sqlite3.Connection, items: list[Item]) -> dict[str, int]:
    """Read-only: what the sweep in `apply` would drop on top of the forgotten repos (old orphans included)."""
    ids = [r[0] for it in items if it.project is not None
           for r in db.execute("SELECT id FROM repos WHERE project_path=?", (it.project,))]
    return indexer.orphan_counts(db, ids)


def apply(db: sqlite3.Connection, items: list[Item]) -> dict[str, int]:
    """Write. Caller holds the sync lock and a vec-enabled connection."""
    res = {"projects": 0, "repos": 0, "registry": 0}
    for it in items:
        if it.project is not None:
            p = it.project
            for (rid,) in db.execute("SELECT id FROM repos WHERE project_path=?", (p,)).fetchall():
                indexer.drop_repo(db, rid)
                res["repos"] += 1
            db.execute("BEGIN IMMEDIATE")
            try:
                for t in ("tickets", "ticket_edges", "ticket_issues", "ticket_dirs"):
                    db.execute(f"DELETE FROM {t} WHERE project_path=?", (p,))
                res["projects"] += db.execute("DELETE FROM projects WHERE path=?", (p,)).rowcount
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
    res["registry"] = config.unregister_roots([i.root for i in items] + [_norm(i.root) for i in items])
    res.update(indexer.sweep_orphans(db))
    return res
