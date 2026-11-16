"""Rewrite absolute path prefixes after moving the index to another machine/home (`myvec relocate`)."""
from __future__ import annotations

import sqlite3

from myvec import config

# (table, key column, path column): every column of index.db that holds an absolute path
COLUMNS = [("projects", "path", "root"), ("repos", "id", "dir"), ("memories", "n", "vault_file")]


def _norm(p: str) -> str:
    return p.rstrip("/") or "/"


def rewrite(value: str | None, old: str, new: str) -> str | None:
    """`value` with the prefix `old` replaced by `new` (only on a path-segment boundary), else None."""
    if not value:
        return None
    if value == old:
        return new
    base = old if old.endswith("/") else old + "/"
    if value.startswith(base):
        return new.rstrip("/") + "/" + value[len(base):]
    return None


def _relocate_tokens(old: str, new: str, dry_run: bool) -> int:
    """Rewrite `cwd` in tokens.db (ADR-013); 0 when there is no such database."""
    from myvec import tokens

    if not config.tokens_db_path().exists():
        return 0
    tdb = tokens.open_db()
    n = 0
    try:
        tdb.execute("BEGIN IMMEDIATE")
        try:
            for (cwd,) in tdb.execute("SELECT DISTINCT cwd FROM token_usage WHERE cwd IS NOT NULL").fetchall():
                v = rewrite(cwd, old, new)
                if v is not None:
                    n += tdb.execute("UPDATE token_usage SET cwd=? WHERE cwd=?", (v, cwd)).rowcount
            for (cwd,) in tdb.execute("SELECT DISTINCT cwd FROM tool_calls WHERE cwd IS NOT NULL").fetchall():
                v = rewrite(cwd, old, new)
                if v is not None:
                    n += tdb.execute("UPDATE tool_calls SET cwd=? WHERE cwd=?", (v, cwd)).rowcount
            tdb.execute("ROLLBACK" if dry_run else "COMMIT")
        except BaseException:
            if tdb.in_transaction:
                tdb.execute("ROLLBACK")
            raise
    finally:
        tdb.close()
    return n


def relocate(db: sqlite3.Connection, old: str, new: str, dry_run: bool = False) -> dict[str, int]:
    """Counts of rewritten rows per table (plus `registry`). One transaction; nothing is written on a dry run."""
    old, new = _norm(old), _norm(new)
    counts: dict[str, int] = {}
    db.execute("BEGIN IMMEDIATE")
    try:
        for table, key, col in COLUMNS:
            n = 0
            for r in db.execute(f'SELECT "{key}", "{col}" FROM {table} WHERE "{col}" IS NOT NULL').fetchall():
                v = rewrite(r[1], old, new)
                if v is not None:
                    db.execute(f'UPDATE {table} SET "{col}"=? WHERE "{key}"=?', (v, r[0]))
                    n += 1
            counts[table] = n
        roots = config.load_registry()
        new_roots = [rewrite(r, old, new) or r for r in roots]
        counts["registry"] = sum(a != b for a, b in zip(roots, new_roots))
        if dry_run:
            db.execute("ROLLBACK")
        else:
            db.execute("COMMIT")
            if counts["registry"]:
                config.save_registry(new_roots)
        counts["tokens"] = _relocate_tokens(old, new, dry_run)
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    return counts
