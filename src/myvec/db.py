"""SQLite connection + schema (vec0 + FTS5)."""
from __future__ import annotations

import os
import sqlite3
import struct

from myvec import config

SCHEMA_VERSION = 8

# columns added after schema v1: (table, column, DDL) -> applied by migrate() to databases created earlier
MIGRATIONS = [
    ("projects", "sync_mode", "TEXT DEFAULT 'auto'"),
    ("projects", "window_hours", "REAL"),
    ("projects", "last_access", "REAL DEFAULT 0"),
    ("repos", "fingerprint", "TEXT"),
    ("blobs", "secret_masked", "INTEGER DEFAULT 0"),
    ("memories", "origin_branch", "TEXT"),
    ("memories", "origin_repo", "TEXT"),
    ("memories", "base_commit", "TEXT"),   # v4: merge-base with the default branch when `remember --branch` ran
    ("memories", "tip_seen", "TEXT"),      # v4: last branch tip observed while it had commits outside the default
    ("sessions", "question_options", "TEXT"),   # v8: JSON {"n", "multi", "labels"} of the AskUserQuestion widget (ADR-014)
]

# v6: Claude Code sessions folded from the hook journal (ADR-007); states: working | waiting | ended
SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, state TEXT NOT NULL, last_event TEXT, last_event_at REAL, last_state_event_at REAL, last_seen REAL,
  started_at REAL, ended_at REAL, end_reason TEXT, source TEXT, cwd TEXT, worktree TEXT, branch TEXT,
  project_path TEXT, transcript_path TEXT, permission_mode TEXT, model TEXT, title TEXT, question TEXT,
  question_at REAL, notification_type TEXT, last_message TEXT, ticket_id TEXT, ticket_source TEXT, updated_at REAL, question_options TEXT);
CREATE INDEX IF NOT EXISTS sessions_state ON sessions(state);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_path);
"""

# v7: test runs measured by `myvec measure` (wrapper) or seen by the daemon (passive) (ADR-003, ADR-011);
# states: running | done | lost. Not derived from the vault: `rebuild` does not restore it.
TEST_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs(
  id INTEGER PRIMARY KEY, run_uid TEXT UNIQUE, source TEXT NOT NULL, state TEXT NOT NULL, host TEXT, pid INTEGER,
  project_path TEXT, cwd TEXT, branch TEXT, ticket_id TEXT, ticket_source TEXT, agent TEXT, label TEXT,
  session_id TEXT, command TEXT, started_at REAL, ended_at REAL, wall_s REAL, cpu_user_s REAL, cpu_sys_s REAL,
  cpu_s REAL, avg_cores REAL, peak_cores REAL, peak_rss_bytes INTEGER, exit_code INTEGER, signal INTEGER,
  sample_interval_s REAL, n_samples INTEGER, interrupted INTEGER);
CREATE INDEX IF NOT EXISTS test_runs_project ON test_runs(project_path, started_at);
CREATE INDEX IF NOT EXISTS test_runs_state ON test_runs(state);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects(
  path TEXT PRIMARY KEY, root TEXT NOT NULL, parent TEXT, share_global INTEGER DEFAULT 1, registered_at TEXT,
  sync_mode TEXT DEFAULT 'auto', window_hours REAL, last_access REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS repos(
  id INTEGER PRIMARY KEY, project_path TEXT NOT NULL, path TEXT NOT NULL UNIQUE, dir TEXT NOT NULL UNIQUE,
  current_branch TEXT, last_sync REAL, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS file_state(
  repo_id INTEGER, path TEXT, mtime_ns INTEGER, size INTEGER, sha TEXT, PRIMARY KEY(repo_id, path));
CREATE TABLE IF NOT EXISTS blobs(
  sha TEXT PRIMARY KEY, lang TEXT, indexed_at REAL, skip_reason TEXT, secret_skipped INTEGER DEFAULT 0,
  secret_masked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, blob_sha TEXT NOT NULL, start_line INTEGER, end_line INTEGER, symbol TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_blob ON chunks(blob_sha);
CREATE TABLE IF NOT EXISTS chunk_repo(
  vec_rowid INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, chunk_id INTEGER NOT NULL, UNIQUE(repo_id, chunk_id));
CREATE INDEX IF NOT EXISTS chunk_repo_chunk ON chunk_repo(chunk_id);
CREATE TABLE IF NOT EXISTS branch_files(
  repo_id INTEGER NOT NULL, branch TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL, seen_at REAL,
  PRIMARY KEY(repo_id, branch, path));
CREATE INDEX IF NOT EXISTS branch_files_blob ON branch_files(repo_id, blob_sha);
CREATE INDEX IF NOT EXISTS branch_files_sha ON branch_files(blob_sha, repo_id, branch);
CREATE TABLE IF NOT EXISTS memories(
  n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, scope TEXT NOT NULL, branch TEXT, "commit" TEXT,
  vault_file TEXT, text TEXT NOT NULL, created TEXT, updated TEXT, pinned INTEGER DEFAULT 0,
  superseded_by TEXT, source TEXT DEFAULT 'vault', tags TEXT DEFAULT '', origin_branch TEXT, origin_repo TEXT,
  base_commit TEXT, tip_seen TEXT);
CREATE INDEX IF NOT EXISTS memories_id ON memories(id);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope);

CREATE TABLE IF NOT EXISTS tickets(
  project_path TEXT NOT NULL, id TEXT NOT NULL, title TEXT, kind TEXT, status TEXT, status_raw TEXT, done_date TEXT,
  parent TEXT, size TEXT, branch TEXT, pr TEXT, summary TEXT, updated TEXT, path TEXT, body TEXT,
  body_dropped INTEGER DEFAULT 0, ac_done INTEGER, ac_total INTEGER, progress REAL, legacy INTEGER, extra TEXT,
  PRIMARY KEY(project_path, id));
CREATE INDEX IF NOT EXISTS tickets_path ON tickets(project_path, path);
CREATE INDEX IF NOT EXISTS tickets_parent ON tickets(project_path, parent);
CREATE TABLE IF NOT EXISTS ticket_edges(
  project_path TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
  PRIMARY KEY(project_path, src, dst, kind));
CREATE TABLE IF NOT EXISTS ticket_issues(
  project_path TEXT NOT NULL, severity TEXT, code TEXT, ticket_id TEXT, path TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ticket_issues_project ON ticket_issues(project_path);
CREATE TABLE IF NOT EXISTS ticket_dirs(
  project_path TEXT PRIMARY KEY, dir TEXT, fingerprint TEXT, refreshed_at REAL, n_tickets INTEGER, n_errors INTEGER);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, symbol, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text, symbol) VALUES (new.id, new.text, coalesce(new.symbol, ''));
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text, symbol) VALUES ('delete', old.id, old.text, coalesce(old.symbol, ''));
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  text, content='memories', content_rowid='n', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text) VALUES (new.n, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.n, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.n, old.text);
  INSERT INTO memories_fts(rowid, text) VALUES (new.n, new.text);
END;
""" + SESSIONS_SCHEMA + TEST_RUNS_SCHEMA


class ModelMismatch(RuntimeError):
    pass


def pack(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def has_bit_index(dims: int) -> bool:
    return dims % 8 == 0   # vec0 bit[N] needs a multiple of 8


def vec_tables_statements(dims: int) -> list[str]:
    stmts = [
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
        f"repo_id integer partition key, embedding float[{dims}] distance_metric=cosine)",
        # `scope` is a partition key: a memory KNN is restricted to the visible scopes inside the index itself
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0("
        f"scope text partition key, embedding float[{dims}] distance_metric=cosine)",
    ]
    if has_bit_index(dims):  # binary-quantized copy (same rowids) for the coarse pass of huge cross-project scans
        stmts.append(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_bit USING vec0(embedding bit[{dims}])")
    return stmts


def vec_tables_sql(dims: int) -> str:
    return ";".join(vec_tables_statements(dims)) + ";"


def connect(*, vec: bool = True, readonly: bool = False, threads: bool = False) -> sqlite3.Connection:
    """Open index.db (created on demand unless readonly)."""
    path = config.db_path()
    if not readonly:
        config.ensure_home()
    db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=not threads)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if vec:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


def _drop_trigram(db: sqlite3.Connection) -> None:
    """v3: the trigram FTS table is gone (no gain in the benchmark, 2.2x bigger index). Its triggers go with it;
    SCHEMA recreates the plain triggers right after."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_tri'").fetchone():
        db.execute("DROP TRIGGER IF EXISTS chunks_ai")
        db.execute("DROP TRIGGER IF EXISTS chunks_ad")
        db.execute("DROP TABLE IF EXISTS chunks_tri")


def migrate(db: sqlite3.Connection) -> None:
    """Bring a database created by an older version up to SCHEMA_VERSION (idempotent)."""
    cur = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if cur is not None and int(cur[0]) >= SCHEMA_VERSION:
        return
    for table, col, ddl in MIGRATIONS:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if not have:  # table not created yet (old DB, migrate() before init_schema): SCHEMA creates it in full
            continue
        if col not in have:
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:  # another process migrated concurrently
                if "duplicate column" not in str(e):
                    raise
    # memories written before v2 used `branch`/`commit` as the origin: keep that as metadata too
    db.execute("UPDATE memories SET origin_branch=branch WHERE branch IS NOT NULL AND origin_branch IS NULL")
    db.execute("UPDATE memories SET origin_repo=scope WHERE branch IS NOT NULL AND origin_repo IS NULL AND scope!='global'")
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))


def init_schema(db: sqlite3.Connection, cfg: config.Config) -> None:
    """Create schema; record model/dims on first creation."""
    _drop_trigram(db)
    db.executescript(SCHEMA)
    migrate(db)
    row = db.execute("SELECT value FROM meta WHERE key='dims'").fetchone()
    if row is None:
        db.execute("INSERT OR REPLACE INTO meta VALUES('model', ?)", (cfg.model,))
        db.execute("INSERT OR REPLACE INTO meta VALUES('dims', ?)", (str(cfg.dims),))
        db.executescript(vec_tables_sql(cfg.dims))
    else:
        db.executescript(vec_tables_sql(int(row[0])))
    _migrate_memories_vec(db)


def _migrate_memories_vec(db: sqlite3.Connection) -> None:
    """v4: memories_vec gets a `scope` partition key. Databases created earlier have the plain table: re-create it
    (vectors are copied over, scope comes from the memories table). Idempotent and safe against concurrent runs."""
    def old_layout() -> bool:
        r = db.execute("SELECT sql FROM sqlite_master WHERE name='memories_vec'").fetchone()
        return r is not None and "partition key" not in (r[0] or "").lower()

    if not old_layout():
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        if not old_layout():  # another process got there first
            db.execute("COMMIT")
            return
        rows = db.execute("SELECT rowid, embedding FROM memories_vec").fetchall()
        dims = int(db.execute("SELECT value FROM meta WHERE key='dims'").fetchone()[0])
        db.execute("DROP TABLE memories_vec")
        db.execute(vec_tables_statements(dims)[1])
        for rid, emb in rows:
            m = db.execute("SELECT scope FROM memories WHERE n=?", (rid,)).fetchone()
            if m is not None:
                db.execute("INSERT INTO memories_vec(rowid, scope, embedding) VALUES(?,?,?)", (rid, m[0], emb))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def check_meta(db: sqlite3.Connection, cfg: config.Config) -> None:
    m = {r[0]: r[1] for r in db.execute("SELECT key, value FROM meta")}
    if m.get("model") != cfg.model or m.get("dims") != str(cfg.dims):
        raise ModelMismatch(
            f"config model/dims ({cfg.model}/{cfg.dims}) differ from the index "
            f"({m.get('model')}/{m.get('dims')}). Run `myvec reembed`."
        )


def open_ready(cfg: config.Config | None = None, *, vec: bool = True, threads: bool = False) -> sqlite3.Connection:
    """connect + init schema + verify model/dims."""
    cfg = cfg or config.load_config()
    db = connect(vec=vec, threads=threads)
    init_schema(db, cfg)
    check_meta(db, cfg)
    return db


def get_meta(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default
