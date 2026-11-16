"""Branch-aware, content-addressed indexing of code repos."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import pathspec

from myvec import chunk as chunkmod
from myvec import config
from myvec import db as dbmod
from myvec import filters, gitutil, projects
from myvec.config import Config
from myvec.embed import Embedder

PSEUDO_BRANCH = "-"
BATCH_CHUNKS = 64


class SyncCancelled(Exception):
    """Cooperative cancel (daemon stopping): raised between batches, never inside a DB transaction."""


@dataclass
class SyncStats:
    repos: int = 0
    files: int = 0
    new_blobs: int = 0
    chunks_embedded: int = 0
    secrets_skipped: int = 0     # chunks dropped (private keys / known token formats)
    secrets_masked: int = 0      # literal values replaced by ••• (password = '...')
    vectors_reused: int = 0
    errors: list[str] = field(default_factory=list)
    blobs_total: int = 0         # progress (grows as repos are discovered)
    blobs_done: int = 0
    progress_cb: Callable[["SyncStats"], None] | None = None
    cancel: Callable[[], bool] | None = None

    def check(self) -> None:
        if self.cancel is not None and self.cancel():
            raise SyncCancelled()

    def notify(self) -> None:
        if self.progress_cb is not None:
            try:
                self.progress_cb(self)
            except Exception:  # noqa: BLE001 - progress reporting must never break a sync
                pass


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------- registration

class ProjectConflict(ValueError):
    """The project (or repo) path is already used by a different, still existing directory."""


def _same_dir(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def _suggest_name(root: Path, name: str) -> str:
    last = name.strip("/").split("/")[-1] or root.name
    return f"{root.parent.name}-{last}" if root.parent.name else f"{last}-2"


def check_project_free(db: sqlite3.Connection, m: projects.Marker) -> None:
    """Refuse to take over a project path that belongs to another existing project root: two directories both
    named `api` would otherwise hijack each other's repos and memories."""
    row = db.execute("SELECT root FROM projects WHERE path=?", (m.path,)).fetchone()
    if row is None or _same_dir(row[0], m.root):
        return
    old = Path(row[0])
    om = projects.try_read_marker(old) if old.is_dir() else None
    if om is not None and om.path == m.path:  # the other root is still a live project under this very path
        raise ProjectConflict(
            f"project path {m.path!r} is already used by {old}. Pick another name, e.g. "
            f"`myvec init --name {_suggest_name(m.root, m.name)}` (or `--parent` to nest it)")


def register_project(db: sqlite3.Connection, m: projects.Marker) -> None:
    check_project_free(db, m)
    config.register_root(str(m.root))  # registry.toml keeps index.db rebuildable
    db.execute(
        "INSERT INTO projects(path, root, parent, share_global, registered_at, last_access) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET root=excluded.root, parent=excluded.parent, share_global=excluded.share_global",
        (m.path, str(m.root), m.parent, int(m.share_global), time.strftime("%Y-%m-%dT%H:%M:%S"), time.time()),
    )


def _upsert_repo(db: sqlite3.Connection, project_path: str, spec: projects.RepoSpec) -> int:
    r = db.execute("SELECT id, path FROM repos WHERE dir=?", (str(spec.dir),)).fetchone()
    if r:
        if r["path"] != spec.path:
            other = db.execute("SELECT id, dir FROM repos WHERE path=? AND id!=?", (spec.path, r["id"])).fetchone()
            if other is not None:
                if Path(other["dir"]).exists():
                    raise ProjectConflict(f"repo path {spec.path!r} is already used by {other['dir']}")
                drop_repo(db, other["id"])  # leftover of a directory that no longer exists
        db.execute("UPDATE repos SET project_path=?, path=? WHERE id=?", (project_path, spec.path, r["id"]))
        return r["id"]
    r = db.execute("SELECT id, dir FROM repos WHERE path=?", (spec.path,)).fetchone()
    if r:
        if Path(r["dir"]).exists() and not _same_dir(r["dir"], spec.dir):
            raise ProjectConflict(f"repo path {spec.path!r} is already used by {r['dir']}; "
                                  f"give this project another name (`myvec init --name ...`)")
        # same repo path, its old directory is gone: the directory moved
        db.execute("UPDATE repos SET project_path=?, dir=? WHERE id=?", (project_path, str(spec.dir), r["id"]))
        return r["id"]
    cur = db.execute("INSERT INTO repos(project_path, path, dir) VALUES(?,?,?)",
                     (project_path, spec.path, str(spec.dir)))
    return cur.lastrowid


def drop_repo(db: sqlite3.Connection, repo_id: int) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute("SELECT vec_rowid FROM chunk_repo WHERE repo_id=?", (repo_id,)).fetchall()
        for (rid,) in rows:
            _delete_vec(db, rid)
        db.execute("DELETE FROM chunk_repo WHERE repo_id=?", (repo_id,))
        db.execute("DELETE FROM branch_files WHERE repo_id=?", (repo_id,))
        db.execute("DELETE FROM file_state WHERE repo_id=?", (repo_id,))
        db.execute("DELETE FROM repos WHERE id=?", (repo_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- file listing

def _specs(marker: projects.Marker):
    inc = pathspec.PathSpec.from_lines("gitignore", marker.include) if marker.include else None
    exc = pathspec.PathSpec.from_lines("gitignore", marker.exclude) if marker.exclude else None
    return inc, exc


def _allowed(rel_in_repo: str, prefix: str, inc, exc) -> bool:
    if filters.is_denied_path(rel_in_repo):
        return False
    full = f"{prefix}/{rel_in_repo}" if prefix else rel_in_repo
    if exc is not None and exc.match_file(full):
        return False
    if inc is not None and not inc.match_file(full):
        return False
    return True


def _hash_cached(db: sqlite3.Connection, repo_id: int, base: Path, rel: str, seen: set[str],
                 pending: list[tuple]) -> str | None:
    """sha of worktree file via (mtime,size) cache; None if unreadable/too big. Read-only: new cache rows are
    appended to `pending` and written later by `_flush_file_state` (never inside a transaction held while walking)."""
    ap = base / rel
    try:
        st = ap.lstat()
    except OSError:
        return None
    if not os.path.isfile(ap) or ap.is_symlink() or st.st_size > filters.MAX_FILE_BYTES:
        return None
    seen.add(rel)
    row = db.execute("SELECT mtime_ns, size, sha FROM file_state WHERE repo_id=? AND path=?", (repo_id, rel)).fetchone()
    if row and row[0] == st.st_mtime_ns and row[1] == st.st_size:
        return row[2]
    try:
        sha = git_blob_sha(ap.read_bytes())
    except OSError:
        return None
    pending.append((repo_id, rel, st.st_mtime_ns, st.st_size, sha))
    return sha


def _flush_file_state(db: sqlite3.Connection, repo_id: int, pending: list[tuple], seen: set[str]) -> None:
    """Persist the (mtime,size)->sha cache in short write transactions and drop rows of files that are no longer
    dirty/untracked. It is only a cache: a busy database must not fail the sync."""
    try:
        for batch in _chunked(pending, 500):
            db.execute("BEGIN IMMEDIATE")
            try:
                db.executemany("INSERT OR REPLACE INTO file_state VALUES(?,?,?,?,?)", batch)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        stale = [(repo_id, p) for (p,) in db.execute("SELECT path FROM file_state WHERE repo_id=?", (repo_id,)).fetchall()
                 if p not in seen]
        for batch in _chunked(stale, 500):
            db.execute("BEGIN IMMEDIATE")
            try:
                db.executemany("DELETE FROM file_state WHERE repo_id=? AND path=?", batch)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
    except sqlite3.OperationalError:
        pass


def _under_any(p: str, prefixes: list[str]) -> bool:
    return any(p == x or p.startswith(x + "/") for x in prefixes)


def list_git_files(db, repo_id: int, spec: projects.RepoSpec, marker: projects.Marker,
                   nested: set[Path] | None = None) -> dict[str, str]:
    inc, exc = _specs(marker)
    prefix = spec.dir.relative_to(marker.root).as_posix() if spec.dir != marker.root else ""
    d = spec.dir
    skip = [n.relative_to(d).as_posix() for n in (nested or ()) if d in n.parents]  # child projects / sub-repos
    dirty, deleted = gitutil.status_dirty(d)
    files: dict[str, str] = {}
    seen: set[str] = set()
    pending: list[tuple] = []
    need_hash: list[str] = []
    for _mode, sha, p in gitutil.ls_files_staged(d):
        if p in deleted or _under_any(p, skip) or not _allowed(p, prefix, inc, exc):
            continue
        if p in dirty:
            need_hash.append(p)
        else:
            files[p] = sha
    for p in gitutil.ls_untracked(d):
        if not _under_any(p, skip) and _allowed(p, prefix, inc, exc):
            need_hash.append(p)
    for p in need_hash:  # hashing happens outside any transaction
        sha = _hash_cached(db, repo_id, d, p, seen, pending)
        if sha:
            files[p] = sha
    _flush_file_state(db, repo_id, pending, seen)  # drops cache rows for files no longer dirty/untracked
    return files


def list_plain_files(db, repo_id: int, spec: projects.RepoSpec, marker: projects.Marker,
                     nested: set[Path]) -> dict[str, str]:
    inc, exc = _specs(marker)
    prefix = spec.dir.relative_to(marker.root).as_posix() if spec.dir != marker.root else ""
    root = spec.dir
    ignores: list[tuple[str, pathspec.PathSpec]] = []

    def ignored(rel: str, is_dir: bool) -> bool:
        for d, sp in ignores:
            if d == "":
                sub = rel
            elif rel.startswith(d + "/"):
                sub = rel[len(d) + 1:]
            else:
                continue
            if sp.match_file(sub + ("/" if is_dir else "")):
                return True
        return False

    files: dict[str, str] = {}
    seen: set[str] = set()
    pending: list[tuple] = []
    # walk + hash without any open transaction
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        if ".gitignore" in filenames:
            try:
                lines = (Path(dirpath) / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
                ignores.append((rel_dir, pathspec.PathSpec.from_lines("gitignore", lines)))
            except OSError:
                pass
        keep = []
        for dn in sorted(dirnames):
            ap = Path(dirpath) / dn
            rel = f"{rel_dir}/{dn}" if rel_dir else dn
            if (filters.is_denied_dir(dn) and dn != "graphify-out") or ap in nested or ap.is_symlink():
                continue
            if ignored(rel, True):
                continue
            if projects.is_excluded_dir(exc, f"{prefix}/{rel}" if prefix else rel):
                continue
            if (ap / ".git").exists() or (ap / projects.MARKER).is_file():
                continue
            keep.append(dn)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if ignored(rel, False) or not _allowed(rel, prefix, inc, exc):
                continue
            sha = _hash_cached(db, repo_id, root, rel, seen, pending)
            if sha:
                files[rel] = sha
    _flush_file_state(db, repo_id, pending, seen)
    return files


# ---------------------------------------------------------------- embedding of new blobs

@dataclass
class _Prepared:
    sha: str
    lang: str | None
    skip_reason: str | None
    secrets: int
    masked: int
    chunks: list[chunkmod.Chunk]
    texts: list[str]


def _prepare(base: Path, rel: str, sha: str) -> _Prepared:
    """Read, chunk and mask one file. The returned `sha` is the key of the bytes that were actually read: it differs
    from `sha` if the file was edited after it was listed (the caller must not store these chunks under the old key)."""
    ap = base / rel
    try:
        st = ap.stat()
        if st.st_size > filters.MAX_FILE_BYTES:
            return _Prepared(sha, None, "too-large", 0, 0, [], [])
        data = ap.read_bytes()
    except OSError:
        return _Prepared(sha, None, "unreadable", 0, 0, [], [])   # transient: never persisted
    real = git_blob_sha(data)
    if real != sha:
        # clean/smudge filters (eol conversion) make the tracked blob differ from the worktree bytes without any edit
        if not (gitutil.is_repo(base) and gitutil.hash_object(base, rel, data) == sha):
            sha = real
    if filters.looks_binary(data):
        return _Prepared(sha, None, "binary", 0, 0, [], [])
    text = data.decode("utf-8", errors="replace")
    lang, chunks = chunkmod.chunk_file(rel, text)
    key_ranges = filters.private_key_ranges(text)  # the middle of a PEM key has no marker of its own
    kept, secrets, masked = [], 0, 0
    for c in chunks:
        if filters.has_hard_secret(c.text) or filters.overlaps_ranges(c.start_line, c.end_line, key_ranges):
            secrets += 1  # private key / known token format: the chunk is dropped
            continue
        c.text, n = filters.mask_secrets(c.text)  # what is stored AND embedded is the masked text
        masked += n
        if c.symbol:
            c.symbol = filters.mask_secrets(c.symbol)[0]  # markdown headings etc. are stored, embedded and printed too
        kept.append(c)
    return _Prepared(sha, lang, None, secrets, masked, kept, [chunkmod.embed_text(rel, c) for c in kept])


def _store_batch(db: sqlite3.Connection, repo_id: int, batch: list[_Prepared], vecs: list[list[float]]) -> None:
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        i = 0
        for p in batch:
            if p.skip_reason == "unreadable":
                continue
            db.execute("INSERT OR REPLACE INTO blobs(sha, lang, indexed_at, skip_reason, secret_skipped, secret_masked) "
                       "VALUES(?,?,?,?,?,?)",
                       (p.sha, p.lang, now, p.skip_reason, p.secrets, p.masked))
            for c in p.chunks:
                cid = db.execute(
                    "INSERT INTO chunks(blob_sha, start_line, end_line, symbol, text) VALUES(?,?,?,?,?)",
                    (p.sha, c.start_line, c.end_line, c.symbol, c.text)).lastrowid
                _insert_vec(db, repo_id, cid, vecs[i])
                i += 1
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _insert_vec(db, repo_id: int, chunk_id: int, vec) -> None:
    blob = vec if isinstance(vec, (bytes, bytearray)) else dbmod.pack(vec)
    rid = db.execute("INSERT INTO chunks_vec(repo_id, embedding) VALUES(?,?)", (repo_id, blob)).lastrowid
    if len(blob) % 4 == 0 and dbmod.has_bit_index(len(blob) // 4):
        db.execute("INSERT INTO chunks_bit(rowid, embedding) VALUES(?, vec_quantize_binary(?))", (rid, blob))
    db.execute("INSERT INTO chunk_repo(vec_rowid, repo_id, chunk_id) VALUES(?,?,?)", (rid, repo_id, chunk_id))


def _delete_vec(db, rowid: int) -> None:
    db.execute("DELETE FROM chunks_vec WHERE rowid=?", (rowid,))
    try:
        db.execute("DELETE FROM chunks_bit WHERE rowid=?", (rowid,))
    except sqlite3.OperationalError:  # no bit index (dims not a multiple of 8)
        pass


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _ensure_repo_vectors(db, repo_id: int, base: Path, files: dict[str, str], embedder: Embedder,
                         stats: SyncStats) -> None:
    """For blobs already embedded in another repo: copy vectors (no embedding)."""
    db.execute("CREATE TEMP TABLE IF NOT EXISTS cur_shas(sha TEXT PRIMARY KEY)")
    db.execute("DELETE FROM cur_shas")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.executemany("INSERT OR IGNORE INTO cur_shas VALUES(?)", [(s,) for s in set(files.values())])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    missing = db.execute(
        "SELECT c.id, c.blob_sha, c.start_line, c.end_line, c.symbol, c.text FROM chunks c "
        "JOIN cur_shas s ON s.sha=c.blob_sha "
        "WHERE NOT EXISTS (SELECT 1 FROM chunk_repo cr WHERE cr.repo_id=? AND cr.chunk_id=c.id)", (repo_id,)
    ).fetchall()
    if not missing:
        return
    path_of: dict[str, str] = {}
    for p, s in sorted(files.items()):
        path_of.setdefault(s, p)
    for group in _chunked(missing, 200):
        stats.check()
        reembed: list[tuple[int, str]] = []
        db.execute("BEGIN IMMEDIATE")
        try:
            for row in group:
                src = db.execute("SELECT vec_rowid FROM chunk_repo WHERE chunk_id=? LIMIT 1", (row["id"],)).fetchone()
                emb = None
                if src:
                    r = db.execute("SELECT embedding FROM chunks_vec WHERE rowid=?", (src[0],)).fetchone()
                    emb = r[0] if r else None
                if emb is not None:
                    _insert_vec(db, repo_id, row["id"], emb)
                    stats.vectors_reused += 1
                else:
                    reembed.append((row["id"], chunkmod.embed_text(
                        path_of.get(row["blob_sha"], ""),
                        chunkmod.Chunk(row["start_line"], row["end_line"], row["symbol"], row["text"]))))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if reembed:  # chunk existed without any vector (interrupted/failed state): embed outside txn
            stats.check()
            texts = [t for _, t in reembed]
            vecs = embedder.embed_docs(texts, check=stats.check) if stats.cancel else embedder.embed_docs(texts)
            stats.chunks_embedded += len(vecs)
            db.execute("BEGIN IMMEDIATE")
            try:
                for (cid, _), v in zip(reembed, vecs):
                    _insert_vec(db, repo_id, cid, v)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise


def sync_repo(db: sqlite3.Connection, embedder: Embedder, marker: projects.Marker, spec: projects.RepoSpec,
              repo_id: int, nested: set[Path], stats: SyncStats, log: Callable[[str], None]) -> None:
    stats.check()
    fp = gitutil.fingerprint(spec.dir, spec.is_git)  # taken before listing: later changes trigger another sync
    if spec.is_git:
        branch = gitutil.head_branch(spec.dir) or "HEAD"
        files = list_git_files(db, repo_id, spec, marker, nested)
    else:
        branch = PSEUDO_BRANCH
        files = list_plain_files(db, repo_id, spec, marker, nested)
    stats.files += len(files)

    known = {r[0] for r in db.execute("SELECT sha FROM blobs")}
    by_sha: dict[str, list[str]] = {}
    for p, s in sorted(files.items()):
        if s not in known:
            by_sha.setdefault(s, []).append(p)
    todo = list(by_sha.items())
    stats.blobs_total += len(todo)
    stats.notify()
    if todo:
        log(f"  {spec.path} [{branch}]: {len(todo)} new blobs")
    batch: list[_Prepared] = []
    queued: set[str] = set()
    ncunks = 0
    raced = False

    def flush():
        nonlocal batch, ncunks
        if not batch:
            return
        texts = [t for p in batch for t in p.texts]
        stats.check()
        # outside any DB write transaction; with a cancel flag the embedder also checks between sub-batches
        vecs = embedder.embed_docs(texts, check=stats.check) if stats.cancel else embedder.embed_docs(texts)
        _store_batch(db, repo_id, batch, vecs)
        stats.chunks_embedded += len(texts)
        stats.new_blobs += len(batch)
        stats.secrets_skipped += sum(p.secrets for p in batch)
        stats.secrets_masked += sum(p.masked for p in batch)
        stats.blobs_done += len(batch)
        batch, ncunks = [], 0
        stats.notify()

    def enqueue(p: _Prepared) -> None:
        nonlocal ncunks
        queued.add(p.sha)
        batch.append(p)
        ncunks += len(p.chunks)
        if ncunks >= BATCH_CHUNKS:
            flush()

    for sha, paths in todo:
        stats.check()
        for rel in paths:
            p = _prepare(spec.dir, rel, sha)
            if p.skip_reason == "unreadable":  # vanished / unreadable mid-sync: not indexed, not in the manifest
                files.pop(rel, None)
                raced = True
                continue
            if p.sha != sha:  # edited after it was listed: index what was read under its own key, fix the manifest
                files[rel] = p.sha
                raced = True
                if p.sha not in known and p.sha not in queued:
                    stats.blobs_total += 1
                    enqueue(p)
                continue
            if sha not in queued:
                enqueue(p)
            break  # one readable path per blob is enough
    flush()
    stats.check()
    if raced:
        fp = f"{fp}|raced"  # never equal to a fresh fingerprint: the next search/sync sees the repo as stale

    _ensure_repo_vectors(db, repo_id, spec.dir, files, embedder, stats)
    stats.check()

    # replace manifest for this branch (diff vs stored)
    now = time.time()
    stored = {r[0]: r[1] for r in db.execute("SELECT path, blob_sha FROM branch_files WHERE repo_id=? AND branch=?",
                                             (repo_id, branch))}
    db.execute("BEGIN IMMEDIATE")
    try:
        for p in stored.keys() - files.keys():
            db.execute("DELETE FROM branch_files WHERE repo_id=? AND branch=? AND path=?", (repo_id, branch, p))
        for p, s in files.items():
            if stored.get(p) != s:
                db.execute("INSERT OR REPLACE INTO branch_files VALUES(?,?,?,?,?)", (repo_id, branch, p, s, now))
        db.execute("UPDATE repos SET current_branch=?, last_sync=?, fingerprint=? WHERE id=?",
                   (branch, now, fp, repo_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    stats.repos += 1
    stats.notify()


def sync_project(db: sqlite3.Connection, cfg: Config, embedder: Embedder, marker: projects.Marker,
                 log: Callable[[str], None] = lambda s: None, stats: SyncStats | None = None,
                 _top: bool = True) -> SyncStats:
    stats = stats or SyncStats()
    register_project(db, marker)
    specs, children = projects.discover(marker)
    keep_ids = []
    kept_specs = []
    for spec in specs:
        try:
            rid = _upsert_repo(db, marker.path, spec)
        except ProjectConflict as e:
            stats.errors.append(f"{spec.path}: {e}")
            log(f"  ERROR {spec.path}: {e}")
            continue
        keep_ids.append(rid)
        kept_specs.append(spec)
    specs_all, specs = specs, kept_specs
    # forget repos of this project that disappeared (only ones that live under this project's root or are gone:
    # a repo of another root is never ours to drop)
    for (rid, rdir) in [(r[0], r[1]) for r in db.execute("SELECT id, dir FROM repos WHERE project_path=?",
                                                          (marker.path,)).fetchall()]:
        if rid in keep_ids:
            continue
        d = Path(rdir)
        if d.exists():
            rd, rr = d.resolve(), marker.root.resolve()
            if rd != rr and rr not in rd.parents:
                continue
        drop_repo(db, rid)
    for spec, rid in zip(specs, keep_ids):
        stats.check()
        try:
            sync_repo(db, embedder, marker, spec, rid, projects.nested_dirs(marker, specs_all, children, spec),
                      stats, log)
        except (dbmod.ModelMismatch, SyncCancelled):
            raise
        except Exception as e:  # keep going with other repos; embed errors abort via re-raise below
            from myvec.embed import EmbedError

            if isinstance(e, EmbedError):
                raise
            stats.errors.append(f"{spec.path}: {e}")
            log(f"  ERROR {spec.path}: {e}")
    try:  # tickets table (ADR-005): cheap (dir fingerprint), never blocks code indexing
        from myvec import ticketdb

        ticketdb.refresh_marker(db, marker)
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"{marker.path}: tickets: {e}")
        log(f"  ERROR {marker.path}: tickets: {e}")
    for child in children:
        stats.check()
        try:
            sync_project(db, cfg, embedder, child, log, stats, _top=False)
        except ProjectConflict as e:
            stats.errors.append(f"{child.path}: {e}")
            log(f"  ERROR {child.path}: {e}")
    if _top:
        try:  # cheap: no git calls unless some memory is branch-scoped
            from myvec import memory

            memory.lifecycle(db)
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"memory lifecycle: {e}")
    return stats


# ---------------------------------------------------------------- gc / reembed

def gc(db: sqlite3.Connection, log: Callable[[str], None] = lambda s: None) -> dict[str, int]:
    res = {"manifests": 0, "vectors": 0, "chunks": 0, "blobs": 0, "repos": 0}
    for repo in db.execute("SELECT id, path, dir FROM repos").fetchall():
        d = repo["dir"]
        if not Path(d).exists():
            log(f"gc: directory of repo {repo['path']} is gone ({d}); removing the repo and its index")
            drop_repo(db, repo["id"])  # manifests, vectors; orphaned chunks/blobs are swept below
            res["repos"] += 1
            continue
        if gitutil.is_repo(d):
            alive = gitutil.local_branches(d)
            cur = gitutil.head_branch(d)
            if cur:
                alive.add(cur)
        else:
            alive = {PSEUDO_BRANCH}
        for (b,) in db.execute("SELECT DISTINCT branch FROM branch_files WHERE repo_id=?", (repo["id"],)).fetchall():
            if b not in alive:
                n = db.execute("DELETE FROM branch_files WHERE repo_id=? AND branch=?", (repo["id"], b)).rowcount
                res["manifests"] += 1 if n else 0
    try:
        from myvec import memory

        lc = memory.lifecycle(db)
        res["memories_merged"], res["memories_archived"] = len(lc["merged"]), len(lc["archived"])
    except Exception:  # noqa: BLE001
        res["memories_merged"] = res["memories_archived"] = 0
    res.update(sweep_orphans(db))
    return res


def sweep_orphans(db: sqlite3.Connection) -> dict[str, int]:
    """Drop vectors/chunks/blobs no manifest references any more (shared by `gc` and `forget`)."""
    res = {"vectors": 0, "chunks": 0, "blobs": 0}
    db.execute("BEGIN IMMEDIATE")
    try:
        dead = db.execute(
            "SELECT cr.vec_rowid FROM chunk_repo cr JOIN chunks c ON c.id=cr.chunk_id WHERE NOT EXISTS "
            "(SELECT 1 FROM branch_files bf WHERE bf.repo_id=cr.repo_id AND bf.blob_sha=c.blob_sha)").fetchall()
        for (rid,) in dead:
            _delete_vec(db, rid)
            db.execute("DELETE FROM chunk_repo WHERE vec_rowid=?", (rid,))
        res["vectors"] = len(dead)
        res["chunks"] = db.execute(
            "DELETE FROM chunks WHERE blob_sha NOT IN (SELECT blob_sha FROM branch_files)").rowcount
        res["blobs"] = db.execute("DELETE FROM blobs WHERE sha NOT IN (SELECT blob_sha FROM branch_files)").rowcount
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return res


def orphan_counts(db: sqlite3.Connection, drop_repo_ids: Iterable[int] = ()) -> dict[str, int]:
    """Read-only twin of `sweep_orphans`: what it would drop once `drop_repo_ids` are dropped (none = `gc` now)."""
    drop = json.dumps(sorted(set(drop_repo_ids)))
    keep = "bf.repo_id NOT IN (SELECT value FROM json_each(?))"
    return {
        "vectors": db.execute(
            "SELECT count(*) FROM chunk_repo cr JOIN chunks c ON c.id=cr.chunk_id "
            "WHERE cr.repo_id NOT IN (SELECT value FROM json_each(?)) AND NOT EXISTS "
            "(SELECT 1 FROM branch_files bf WHERE bf.repo_id=cr.repo_id AND bf.blob_sha=c.blob_sha)", (drop,)).fetchone()[0],
        "chunks": db.execute(
            f"SELECT count(*) FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM branch_files bf "
            f"WHERE bf.blob_sha=c.blob_sha AND {keep})", (drop,)).fetchone()[0],
        "blobs": db.execute(
            f"SELECT count(*) FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM branch_files bf "
            f"WHERE bf.blob_sha=b.sha AND {keep})", (drop,)).fetchone()[0],
    }


def reembed(db: sqlite3.Connection, cfg: Config, embedder: Embedder,
            log: Callable[[str], None] = lambda s: None) -> dict[str, int]:
    """Re-create all vectors with the configured model/dims."""
    pairs = db.execute("SELECT repo_id, chunk_id FROM chunk_repo").fetchall()
    chunk_ids = sorted({p[1] for p in pairs})
    # path prefix for each chunk: any manifest path of its blob
    paths: dict[str, str] = {}
    for sha, path in db.execute("SELECT blob_sha, path FROM branch_files"):
        paths.setdefault(sha, path)
    chunk_vec: dict[int, list[float]] = {}
    for group in _chunked(chunk_ids, BATCH_CHUNKS):
        rows = db.execute(
            f"SELECT id, blob_sha, start_line, end_line, symbol, text FROM chunks WHERE id IN ({','.join('?' * len(group))})",
            group).fetchall()
        texts = [chunkmod.embed_text(paths.get(r["blob_sha"], ""),
                                     chunkmod.Chunk(r["start_line"], r["end_line"], r["symbol"], r["text"]))
                 for r in rows]
        for r, v in zip(rows, embedder.embed_docs(texts)):
            chunk_vec[r["id"]] = v
        log(f"  embedded {len(chunk_vec)}/{len(chunk_ids)} chunks")
    mems = db.execute("SELECT n, scope, text FROM memories WHERE superseded_by IS NULL").fetchall()
    mem_vec: dict[int, tuple[str, list[float]]] = {}
    for group in _chunked(mems, BATCH_CHUNKS):
        for r, v in zip(group, embedder.embed_docs([r["text"][:6000] for r in group])):
            mem_vec[r["n"]] = (r["scope"], v)
    # one transaction for drop + re-create + re-insert: a failure half-way leaves the old index untouched
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DROP TABLE IF EXISTS chunks_vec")
        db.execute("DROP TABLE IF EXISTS chunks_bit")
        db.execute("DROP TABLE IF EXISTS memories_vec")
        db.execute("DELETE FROM chunk_repo")
        for stmt in dbmod.vec_tables_statements(cfg.dims):
            db.execute(stmt)
        for repo_id, cid in pairs:
            _insert_vec(db, repo_id, cid, chunk_vec[cid])
        for n, (scope, v) in mem_vec.items():
            db.execute("INSERT INTO memories_vec(rowid, scope, embedding) VALUES(?,?,?)", (n, scope, dbmod.pack(v)))
        db.execute("INSERT OR REPLACE INTO meta VALUES('model', ?)", (cfg.model,))
        db.execute("INSERT OR REPLACE INTO meta VALUES('dims', ?)", (str(cfg.dims),))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"chunks": len(chunk_vec), "memories": len(mem_vec)}
