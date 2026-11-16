"""Token usage from Claude Code transcripts (ADR-013).

The daemon (or `myvec tokens`) reads `<claude>/projects/<proj>/<session>.jsonl` and
`<session>/subagents/**/agent-<id>.jsonl` incrementally (by byte offset) and keeps one row per model request in a
separate `tokens.db`. Only a whitelist of metadata is stored (ids, model, effort, cwd, branch, counters); message
text, prompts and agent descriptions never reach the database (only ticket ids found in a description).
Attribution to a project and ticket is computed on read (like sessions, ADR-009).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from myvec import config, searchkind

SCHEMA_VERSION = 4
BLOCK = 8 * 1024 * 1024            # bytes read per step of a transcript
TICKET_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Z][A-Za-z0-9]*-\d{2,}(?![A-Za-z0-9_])")
REAL_TICKET_RE = re.compile(r"E\d+-\d+")
ORCH = "оркестратор / без тікета"  # main agent, no ticket found
NO_TICKET = "без тікета"           # subagent, no ticket found
NO_PROJECT = "(поза проєктами)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS token_files(
  file_id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, dev INTEGER, ino INTEGER,
  "offset" INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0, mtime REAL, meta_mtime REAL,
  session_id TEXT, agent_id TEXT, agent_type TEXT, desc_tickets TEXT, req_model TEXT, req_effort TEXT,
  spawn_depth INTEGER, host TEXT, gone INTEGER NOT NULL DEFAULT 0, scanned_at REAL,
  tool_seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS token_usage(
  msg_id TEXT NOT NULL, request_id TEXT NOT NULL, file_id INTEGER NOT NULL, ts REAL, model TEXT,
  requested_model TEXT, effort TEXT, cwd TEXT, git_branch TEXT, input INTEGER, output INTEGER, cache_read INTEGER,
  cache_write INTEGER, cache_write_1h INTEGER, PRIMARY KEY(msg_id, request_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS token_usage_ts ON token_usage(ts);
CREATE INDEX IF NOT EXISTS token_usage_file ON token_usage(file_id);
CREATE TABLE IF NOT EXISTS tool_calls(
  tool_id TEXT PRIMARY KEY, file_id INTEGER NOT NULL, ts REAL, seq INTEGER, cat TEXT NOT NULL, cwd TEXT,
  kind TEXT, doc INTEGER
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tool_calls_ts ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS tool_calls_file ON tool_calls(file_id, seq);
"""


def open_db(readonly: bool = False) -> sqlite3.Connection:
    path = config.tokens_db_path()
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
    else:
        config.ensure_home()
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))   # -wal/-shm inherit the mode of the db file
        db = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    if readonly:
        return db
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    _migrate(db)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(str(path) + suffix, 0o600)
        except OSError:
            pass
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """v1 -> v2: `token_files.tool_seq`; v2 -> v3: `tool_calls.kind`/`doc`; v3 -> v4: one more re-read (v3 builds left NULL labels). Below v4 also a one-off re-read of the
    transcripts still on disk (offset -> 0) so that `tool_calls` gets its history and labels; `token_usage` is
    idempotent by primary key, so the sums do not change."""
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    cols = {r[1] for r in db.execute("PRAGMA table_info(token_files)")}
    tcols = {r[1] for r in db.execute("PRAGMA table_info(tool_calls)")}
    if row is not None and int(row[0] or 0) >= SCHEMA_VERSION and "tool_seq" in cols and {"kind", "doc"} <= tcols:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if "tool_seq" not in {r[1] for r in db.execute("PRAGMA table_info(token_files)")}:
            db.execute("ALTER TABLE token_files ADD COLUMN tool_seq INTEGER NOT NULL DEFAULT 0")
        have = {r[1] for r in db.execute("PRAGMA table_info(tool_calls)")}
        if "kind" not in have:
            db.execute("ALTER TABLE tool_calls ADD COLUMN kind TEXT")
        if "doc" not in have:
            db.execute("ALTER TABLE tool_calls ADD COLUMN doc INTEGER")
        if row is not None and int(row[0] or 0) < SCHEMA_VERSION:
            db.execute('UPDATE token_files SET "offset"=0, tool_seq=0 WHERE gone=0')
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------ parsing (pure)

def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _str(v, limit: int = 200) -> str | None:
    return v[:limit] if isinstance(v, str) and v else None


def _ts(v) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_line(line: bytes) -> dict | None:
    """One transcript line -> whitelisted usage fields, or None (not an assistant record with usage, `<synthetic>`,
    zero usage, broken JSON, no message id)."""
    if b'"usage"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
        return None
    u = msg["usage"]
    model = _str(msg.get("model"))
    msg_id = _str(msg.get("id"), 100)
    if model == "<synthetic>" or not msg_id:
        return None
    cc = u.get("cache_creation")
    out = {
        "msg_id": msg_id, "request_id": _str(rec.get("requestId"), 100) or "", "ts": _ts(rec.get("timestamp")),
        "model": model, "requested_model": _str(rec.get("requestedModel")),
        "effort": _str(rec.get("perTurnEffort")) or _str(rec.get("effort")),
        "cwd": _str(rec.get("cwd"), 1000), "git_branch": _str(rec.get("gitBranch")),
        "input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
        "cache_read": _int(u.get("cache_read_input_tokens")),
        "cache_write": _int(u.get("cache_creation_input_tokens")),
        "cache_write_1h": _int(cc.get("ephemeral_1h_input_tokens")) if isinstance(cc, dict) else 0,
    }
    if not (out["input"] or out["output"] or out["cache_read"] or out["cache_write"]):
        return None
    return out


_SEGMENTS = re.compile(r"&&|\|\||;|\n")
_SELF_CMDS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "fdfind"})
_VAR_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _command_words(seg: str) -> list[str]:
    """Words of one command segment with the wrappers (`VAR=x`, `sudo`, `env`, `timeout N`, `uv run`) removed."""
    w = seg.split()
    while w:
        head = os.path.basename(w[0])
        if _VAR_ASSIGN.match(w[0]) or head in ("sudo", "env", "nohup", "command", "time"):
            w = w[1:]
        elif head == "timeout":
            w = w[1:]
            while w and w[0].startswith("-"):
                takes_value = w[0] in ("-k", "-s", "--kill-after", "--signal")
                w = w[2:] if takes_value else w[1:]
            if w and re.fullmatch(r"[0-9.]+[smhd]?", w[0]):
                w = w[1:]
        elif head == "uv" and len(w) > 1 and w[1] == "run":
            w = w[2:]
            while w and w[0].startswith("-"):
                w = w[1:]
        else:
            break
    return w


def classify(name: str | None, tool_input) -> str | None:
    """Category of a tool call: `self` (Grep/Glob, shell grep/rg/find/fd/`ls -R`), `myvec` (`myvec search`), `recall`
    (`myvec recall`), `read` (Read of a documentation file, E8-16), else None. A heuristic without a shell parser: the first command of each `&&`/`||`/`;`/newline
    segment counts, what follows a pipe is a filter. The command text is only inspected here, never stored."""
    if name in ("Grep", "Glob"):
        return "self"
    if name == "Read":
        return "read" if isinstance(tool_input, dict) and searchkind.is_doc(tool_input.get("file_path")) else None
    if name != "Bash" or not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        return None
    for seg in _SEGMENTS.split(tool_input["command"]):
        w = _command_words(seg.split("|", 1)[0])
        if not w:
            continue
        head = os.path.basename(w[0])
        if head == "myvec" and len(w) > 1 and w[1] in ("search", "recall"):
            return "myvec" if w[1] == "search" else "recall"
        if head in _SELF_CMDS:
            return "self"
        if head == "ls" and any((a.startswith("-") and not a.startswith("--") and "R" in a) or a == "--recursive"
                                for a in w[1:]):
            return "self"
    return None


def parse_tool(line: bytes) -> list[dict]:
    """One transcript line -> its `tool_use` blocks as `{tool_id, ts, cwd, cat, kind, doc}` (`cat` None when the call
    is not a search; those are not stored but still advance the per-file sequence number). `kind` is the sub-kind label
    of a `self` call (None = unclassified), `doc` 0/1 for `self`/`read`, else None. Whitelisted fields only."""
    if b'"tool_use"' not in line:
        return []
    try:
        rec = json.loads(line)
    except ValueError:
        return []
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return []
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            cat = classify(b.get("name"), b.get("input"))
            kind, doc = (searchkind.kind_of(b.get("name"), b.get("input")) if cat == "self" else (None, cat == "read"))
            out.append({"tool_id": _str(b.get("id"), 100), "ts": _ts(rec.get("timestamp")),
                        "cwd": _str(rec.get("cwd"), 1000), "cat": cat, "kind": kind,
                        "doc": int(doc) if cat in ("self", "read") else None})
    return out


def parse_meta(path: Path) -> dict:
    """`agent-<id>.meta.json` -> agent type, ticket ids found in the description (the description itself is dropped),
    requested model/effort, spawn depth. Missing or broken file -> type `unknown`."""
    out = {"agent_type": "unknown", "desc_tickets": "", "req_model": None, "req_effort": None, "spawn_depth": None}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    out["agent_type"] = _str(data.get("agentType"), 64) or "unknown"
    desc = data.get("description")
    if isinstance(desc, str):
        ids: list[str] = []
        for t in TICKET_RE.findall(desc):
            if t not in ids and not t.startswith("ADR-"):
                ids.append(t)
        ids.sort(key=lambda t: not REAL_TICKET_RE.fullmatch(t))   # E<n>-<nn> first, stable otherwise
        out["desc_tickets"] = " ".join(ids[:3])
    out["req_model"] = _str(data.get("model"), 64)
    out["req_effort"] = _str(data.get("effort"), 32)
    sd = data.get("spawnDepth")
    out["spawn_depth"] = sd if isinstance(sd, int) and not isinstance(sd, bool) else None
    return out


# ------------------------------------------------------------------ scanning

@dataclass
class ScanStats:
    files: int = 0
    new_rows: int = 0
    bytes_read: int = 0
    pending_bytes: int = 0
    locked: bool = False


@contextmanager
def _scan_lock():
    p = config.tokens_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def _discover(root: Path):
    """(path, session_id, agent_id|None) for main transcripts and subagent transcripts (incl. workflows)."""
    for p in root.glob("*/*.jsonl"):
        yield p, p.stem, None
    for p in root.glob("*/*/subagents/**/agent-*.jsonl"):
        rel = p.relative_to(root).parts
        yield p, rel[1], p.stem[len("agent-"):]


def _meta_path(p: Path) -> Path:
    return p.with_name(p.stem + ".meta.json")


_INSERT = ("INSERT INTO token_usage(msg_id, request_id, file_id, ts, model, requested_model, effort, cwd, "
           "git_branch, input, output, cache_read, cache_write, cache_write_1h) "
           "VALUES(:msg_id, :request_id, :file_id, :ts, :model, :requested_model, :effort, :cwd, :git_branch, "
           ":input, :output, :cache_read, :cache_write, :cache_write_1h) "
           # a request seen in several files (fork/resume) belongs to the earliest record, tie: smaller path
           "ON CONFLICT(msg_id, request_id) DO UPDATE SET file_id=excluded.file_id, ts=excluded.ts, "
           "model=excluded.model, requested_model=excluded.requested_model, effort=excluded.effort, "
           "cwd=excluded.cwd, git_branch=excluded.git_branch WHERE excluded.ts < token_usage.ts OR "
           "(excluded.ts = token_usage.ts AND (SELECT path FROM token_files WHERE file_id=excluded.file_id) < "
           "(SELECT path FROM token_files WHERE file_id=token_usage.file_id))")


# a re-read (v3 migration) only labels rows that have no label yet; cwd (relocate may have rewritten it), ts, seq, cat stay
_INSERT_TOOL = ("INSERT INTO tool_calls(tool_id, file_id, ts, seq, cat, cwd, kind, doc) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tool_id) DO UPDATE SET kind=excluded.kind, doc=excluded.doc "
                "WHERE tool_calls.kind IS NULL AND tool_calls.doc IS NULL")


def _scan_file(tdb: sqlite3.Connection, path: Path, session_id: str, agent_id: str | None, s: os.stat_result,
               budget: int | None, now: float, host: str, st: ScanStats, row: sqlite3.Row | None) -> int:
    """Read the new complete lines of one transcript (`row` is its stored state or None); returns the bytes read."""
    sp = str(path)
    mm = None
    if agent_id is not None:
        try:
            mm = _meta_path(path).stat().st_mtime
        except OSError:
            mm = None
    same = row is not None and row["dev"] == s.st_dev and row["ino"] == s.st_ino
    if (same and not row["gone"] and row["offset"] == s.st_size and row["size"] == s.st_size
            and (agent_id is None or row["meta_mtime"] == mm)):
        return 0
    start = row["offset"] if same and s.st_size >= row["offset"] else 0
    meta = None
    if agent_id is None:
        meta = {"agent_type": "main", "desc_tickets": "", "req_model": None, "req_effort": None,
                "spawn_depth": None}
    elif row is None or row["meta_mtime"] != mm:
        meta = parse_meta(_meta_path(path))
    if row is None:
        cur = tdb.execute('INSERT INTO token_files(path, "offset", session_id, agent_id, host) VALUES(?,?,?,?,?)',
                          (sp, 0, session_id, agent_id, host))
        file_id = cur.lastrowid
    else:
        file_id = row["file_id"]
    used = 0
    processed = start
    seq = row["tool_seq"] if same and start == row["offset"] else 0
    if start < s.st_size and (budget is None or budget > 0):
        with open(path, "rb") as f:
            f.seek(start)
            pos, carry = start, b""
            while pos < s.st_size and (budget is None or used < budget):
                chunk = f.read(min(BLOCK, s.st_size - pos))
                if not chunk:
                    break
                pos += len(chunk)
                used += len(chunk)
                buf = carry + chunk
                idx = buf.rfind(b"\n")
                if idx < 0:
                    carry = buf
                    continue
                body, carry = buf[:idx + 1], buf[idx + 1:]
                rows = []
                calls = []
                for ln in body.split(b"\n"):
                    r = parse_line(ln)
                    if r is not None:
                        r["file_id"] = file_id
                        rows.append(r)
                    for t in parse_tool(ln):
                        seq += 1
                        if t["cat"] is not None and t["tool_id"]:
                            calls.append((t["tool_id"], file_id, t["ts"], seq, t["cat"], t["cwd"], t["kind"], t["doc"]))
                processed += len(body)
                tdb.execute("BEGIN IMMEDIATE")
                try:
                    tdb.executemany(_INSERT, rows)
                    tdb.executemany(_INSERT_TOOL, calls)
                    tdb.execute('UPDATE token_files SET "offset"=?, tool_seq=? WHERE file_id=?',
                                (processed, seq, file_id))
                    tdb.execute("COMMIT")
                except BaseException:
                    if tdb.in_transaction:
                        tdb.execute("ROLLBACK")
                    raise
    sets = {"dev": s.st_dev, "ino": s.st_ino, '"offset"': processed, "size": s.st_size, "mtime": s.st_mtime,
            "session_id": session_id, "agent_id": agent_id, "host": host, "gone": 0, "scanned_at": now}
    if meta is not None:
        sets.update(meta_mtime=mm, **meta)
    tdb.execute("UPDATE token_files SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE file_id=?",
                (*sets.values(), file_id))
    return used


FULL_SCAN_S = 300.0      # the daemon runs a full discovery (new transcripts) this often
LIGHT_RECENT_S = 86400   # a light pass re-reads known files changed within this window (or not yet read to the end)
LIGHT_LIMIT = 1000       # at most this many known files per light pass


def _light_targets(tdb: sqlite3.Connection, now: float) -> list[sqlite3.Row]:
    """Active sessions first (freshest stored mtime), then unfinished files that are not in that set."""
    rows = tdb.execute('SELECT * FROM token_files WHERE gone=0 AND mtime >= ? ORDER BY mtime DESC LIMIT ?',
                       (now - LIGHT_RECENT_S, LIGHT_LIMIT)).fetchall()
    have = {r["file_id"] for r in rows}
    rest = tdb.execute('SELECT * FROM token_files WHERE gone=0 AND "offset" < size ORDER BY mtime DESC LIMIT ?',
                       (LIGHT_LIMIT,)).fetchall()
    return rows + [r for r in rest if r["file_id"] not in have]


def scan(tdb: sqlite3.Connection, root: Path, *, budget_bytes: int | None = None, now: float | None = None,
         full: bool = True) -> ScanStats:
    """One pass over the transcripts. Big files continue on the next pass from their stored offset. Another running
    scanner (daemon or CLI) holds `state/tokens.lock`: then the pass is skipped (`locked`). `full=False` (the daemon
    between full passes) skips discovery: only recently changed or unfinished known files are checked; new
    transcripts, `gone` marking and a session resumed after > 24 h of silence wait for the next full pass."""
    st = ScanStats()
    now = time.time() if now is None else now
    with _scan_lock() as got:
        if not got:
            st.locked = True
            return st
        host = socket.gethostname()
        rows_before = tdb.execute("SELECT count(*) FROM token_usage").fetchone()[0]
        known: dict[str, sqlite3.Row] = {}
        found = []
        if not full:
            for r in _light_targets(tdb, now):
                try:
                    found.append((Path(r["path"]), r["session_id"], r["agent_id"], Path(r["path"]).stat()))
                except OSError:
                    continue
                known[r["path"]] = r
        else:
            known = {r["path"]: r for r in tdb.execute("SELECT * FROM token_files").fetchall()}
            if Path(root).is_dir():
                for p, sid, aid in _discover(Path(root)):
                    try:
                        found.append((p, sid, aid, p.stat()))
                    except OSError:
                        continue
        found.sort(key=lambda t: t[3].st_mtime, reverse=True)   # fresh transcripts first
        seen = set()
        left = budget_bytes
        for p, sid, aid, s in found:
            seen.add(str(p))
            try:
                used = _scan_file(tdb, p, sid, aid, s, left, now, host, st, known.get(str(p)))
            except OSError:
                continue
            st.bytes_read += used
            if left is not None:
                left = max(0, left - used)
        st.files = len(found)
        st.new_rows = tdb.execute("SELECT count(*) FROM token_usage").fetchone()[0] - rows_before
        if full:
            for r in known.values():
                if not r["gone"] and r["path"] not in seen:
                    tdb.execute("UPDATE token_files SET gone=1 WHERE file_id=?", (r["file_id"],))
        tdb.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_scan', ?)", (str(now),))
        st.pending_bytes = stats(tdb)["pending_bytes"]
    return st


def stats(tdb: sqlite3.Connection) -> dict:
    files = tdb.execute("SELECT count(*) FROM token_files WHERE gone=0").fetchone()[0]
    requests = tdb.execute("SELECT count(*) FROM token_usage").fetchone()[0]
    pending = tdb.execute('SELECT coalesce(sum(size - "offset"), 0) FROM token_files WHERE gone=0').fetchone()[0]
    last = tdb.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
    return {"files": files, "requests": requests, "pending_bytes": pending,
            "last_scan": float(last[0]) if last else None}


# ------------------------------------------------------------------ attribution + read API

def session_context(tdb: sqlite3.Connection, session: str) -> dict | None:
    """Context size of the last request of the main transcript (`agent_id IS NULL`) of a session:
    input + cache_read + cache_write. Sub-agents are not counted; the sum over the session is far larger."""
    r = tdb.execute(
        "SELECT u.ts, u.model, u.input, u.cache_read, u.cache_write FROM token_usage u "
        "JOIN token_files f ON f.file_id = u.file_id WHERE f.session_id = ? AND f.agent_id IS NULL "
        "ORDER BY u.ts DESC LIMIT 1", (session,)).fetchone()
    if r is None:
        return None
    ctx = _int(r["input"]) + _int(r["cache_read"]) + _int(r["cache_write"])
    return {"session": session, "context": ctx, "model": r["model"], "ts": r["ts"]}


def parse_since(s: str | None, now: float | None = None) -> float | None:
    """`7d`, `24h`, `30m` or `YYYY-MM-DD` -> epoch seconds."""
    if not s:
        return None
    now = time.time() if now is None else now
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dhm])", s.strip())
    if m:
        return now - float(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").timestamp()
    except ValueError as e:
        raise ValueError(f"bad --since {s!r}: use 7d, 24h or YYYY-MM-DD") from e


class _Attributor:
    """project / ticket of a usage group, cached; the order for the ticket is description > claim > branch > worktree."""

    def __init__(self, idx: sqlite3.Connection | None):
        from myvec import ticketops

        self.idx = idx
        self.claims = ticketops.read_claims()
        self.ids: dict[str, set[str]] = {}
        if idx is not None:
            try:
                for r in idx.execute("SELECT project_path, id FROM tickets"):
                    self.ids.setdefault(r[0], set()).add(r[1])
            except sqlite3.Error:
                pass
        self._proj: dict = {}

    def project(self, *paths: str | None) -> str | None:
        from myvec import sessions

        if self.idx is None:
            return None
        if paths not in self._proj:
            try:
                self._proj[paths] = sessions.project_of(self.idx, *paths)
            except sqlite3.Error:
                self._proj[paths] = None
        return self._proj[paths]

    def project_of_cwd(self, cwd: str | None) -> str | None:
        from myvec import sessions

        if not cwd:
            return None
        base = cwd.split("/.worktrees/")[0] if "/.worktrees/" in cwd else None
        return self.project(cwd, base, sessions.main_root_of(cwd))

    def ticket(self, project: str | None, session_id: str, desc_tickets: str, cwd: str | None,
               branch: str | None) -> tuple[str | None, str | None, str | None]:
        """(ticket_project, ticket_id, source)."""
        from myvec import sessions

        ids = self.ids.get(project) if project else None
        if ids:
            for t in (desc_tickets or "").split():
                if t in ids:
                    return project, t, "desc"
        worktree = None
        if cwd and "/.worktrees/" in cwd:
            base, rest = cwd.split("/.worktrees/", 1)
            worktree = f"{base}/.worktrees/{rest.split('/')[0]}"
        row = {"project_path": project, "branch": branch, "worktree": worktree}
        res = sessions.resolve_ticket(row, self.claims.get(session_id), self.ids,
                                      lambda root: self.project(root, sessions.main_root_of(root)))
        return res if res else (None, None, None)


def _sum_row() -> dict:
    return {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _finish(d: dict) -> dict:
    d["total"] = d["input"] + d["output"] + d["cache_read"] + d["cache_write"]
    d["cache_share"] = d["cache_read"] / d["total"] if d["total"] else 0.0
    return d


def usage(idx_db: sqlite3.Connection | None, *, tdb: sqlite3.Connection | None = None, project: str | None = None,
          ticket: str | None = None, session: str | None = None, since: float | None = None,
          until: float | None = None, by: tuple[str, ...] | list[str] = ("agent",)) -> list[dict]:
    """Usage grouped by `by` (agent|model|effort|ticket|project|session|day), biggest total first. Grouping by ticket
    also keeps the project (ticket ids repeat across projects) unless a project is given."""
    keys = list(by)
    bad = [k for k in keys if k not in ("agent", "model", "effort", "ticket", "project", "session", "day", "file")]
    if bad:
        raise ValueError(f"unknown grouping: {', '.join(bad)}")
    if "ticket" in keys and "project" not in keys and project is None:
        keys.append("project")
    own = tdb is None
    if own:
        if not config.tokens_db_path().exists():
            return []
        tdb = open_db(readonly=True)
    try:
        where, args = ["1=1"], []
        if since is not None:
            where.append("u.ts >= ?")
            args.append(since)
        if until is not None:
            where.append("u.ts < ?")
            args.append(until)
        if session:
            where.append("f.session_id = ?")
            args.append(session)
        day = ", date(u.ts,'unixepoch','localtime') AS day" if "day" in keys else ", NULL AS day"
        grp = ", date(u.ts,'unixepoch','localtime')" if "day" in keys else ""
        rows = tdb.execute(
            f"SELECT f.file_id, f.session_id, f.agent_type, f.desc_tickets, u.cwd, u.git_branch, u.model, u.effort{day}, "
            "count(*) AS requests, sum(u.input) AS input, sum(u.output) AS output, sum(u.cache_read) AS cache_read, "
            "sum(u.cache_write) AS cache_write FROM token_usage u JOIN token_files f ON f.file_id=u.file_id "
            f"WHERE {' AND '.join(where)} GROUP BY f.file_id, u.cwd, u.git_branch, u.model, u.effort{grp}",
            args).fetchall()
    finally:
        if own:
            tdb.close()
    att = _Attributor(idx_db)
    out: dict[tuple, dict] = {}
    for r in rows:
        proj = att.project_of_cwd(r["cwd"])
        tp, tid, _src = att.ticket(proj, r["session_id"], r["desc_tickets"], r["cwd"], r["git_branch"])
        proj = proj or tp
        if project is not None and proj != project:
            continue
        if ticket is not None and tid != ticket:
            continue
        label = tid or (ORCH if r["agent_type"] == "main" else NO_TICKET)
        vals = {"agent": r["agent_type"] or "unknown", "model": r["model"] or "?", "effort": r["effort"] or "-",
                "ticket": label, "project": proj or NO_PROJECT, "session": r["session_id"], "day": r["day"], "file": r["file_id"]}
        key = tuple(vals[k] for k in keys)
        d = out.get(key)
        if d is None:
            d = out[key] = {**{k: vals[k] for k in keys}, **_sum_row(), "runs": 0, "_files": set()}
        d["_files"].add(r["file_id"])
        for c in ("requests", "input", "output", "cache_read", "cache_write"):
            d[c] += r[c] or 0
    for d in out.values():
        d["runs"] = len(d.pop("_files"))
    return sorted((_finish(d) for d in out.values()), key=lambda d: -d["total"])


def ticket_totals(idx_db: sqlite3.Connection | None, project: str, *, tdb: sqlite3.Connection | None = None) -> dict:
    """{ticket_id: {"totals": {...}, "by_agent": [...]}} for the tickets of one project (card and top)."""
    out: dict[str, dict] = {}
    for r in usage(idx_db, tdb=tdb, project=project, by=("ticket", "agent")):
        if r["ticket"] in (ORCH, NO_TICKET):
            continue
        e = out.setdefault(r["ticket"], {"totals": _finish(_sum_row()), "by_agent": []})
        for c in ("requests", "input", "output", "cache_read", "cache_write"):
            e["totals"][c] += r[c]
        _finish(e["totals"])
        e["by_agent"].append({k: v for k, v in r.items() if k not in ("ticket", "project")})
    return out


def compact(n: float) -> str:
    n = int(n)
    if n < 1000:
        return str(n)
    units = ((1e3, "тис."), (1e6, "млн"), (1e9, "млрд"))
    i = max(j for j, (div, _) in enumerate(units) if n >= div)
    txt = f"{n / units[i][0]:.1f}"
    if txt == "1000.0" and i + 1 < len(units):      # 999 950 -> 1,0 млн, not 1000,0 тис.
        i, txt = i + 1, f"{n / units[i + 1][0]:.1f}"
    return txt.replace(".", ",") + " " + units[i][1]


def agent_label(agent: str) -> str:
    """Display name of an agent type: the main session is the orchestrator (data keeps `main`)."""
    return "оркестратор" if agent == "main" else agent


def _family(model: str) -> str:
    m = re.match(r"claude-([a-z]+)-", model or "")
    return m.group(1).capitalize() if m else (model or "?")


def _ordered(rows: list[dict], key: str, fmt=lambda v: v) -> list[str]:
    tot: dict[str, int] = {}
    for r in rows:
        v = fmt(r[key])
        tot[v] = tot.get(v, 0) + r["total"]
    return [v for v, _ in sorted(tot.items(), key=lambda kv: (-kv[1], kv[0]))]


def ticket_summary(idx_db: sqlite3.Connection | None, project: str | None, ticket: str, *,
                   tdb: sqlite3.Connection | None = None) -> dict | None:
    """One-ticket totals for the Log line and the card; None when nothing was recorded."""
    rows = usage(idx_db, tdb=tdb, project=project, ticket=ticket, by=("agent", "model", "effort"))
    if not rows:
        return None
    # a transcript can span several models/efforts, so count runs on groups that keep a file whole
    return _summarize(rows, usage(idx_db, tdb=tdb, project=project, ticket=ticket, by=("session", "agent")))


def _summarize(rows: list[dict], runs: list[dict]) -> dict:
    total = sum(r["total"] for r in rows)
    agents: dict[str, int] = {}
    for r in rows:
        agents[r["agent"]] = agents.get(r["agent"], 0) + r["total"]
    top_agent, top_total = sorted(agents.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return {"total": total, "output": sum(r["output"] for r in rows), "top_agent": top_agent,
            "top_share": top_total / total if total else 0.0,
            "models": _ordered(rows, "model", _family),
            "efforts": [e for e in _ordered(rows, "effort") if e != "-"],
            "runs": sum(r["runs"] for r in runs), "requests": sum(r["requests"] for r in rows)}


def summary_line(d: dict) -> str:
    return (f"tokens: {compact(d['total'])} · output {compact(d['output'])} · {agent_label(d['top_agent'])} "
            f"{round(d['top_share'] * 100)}% · {'/'.join(d['models'])} · {'/'.join(d['efforts']) or 'effort -'} "
            f"· запусків {d['runs']}")


# ------------------------------------------------------------------ UI reports (ADR-016)

REPORT_DAYS = (7, 14, 30)
TOP_TICKETS = 7


def _day_starts(now: float, days: int) -> list[tuple[str, float]]:
    """[(YYYY-MM-DD, epoch of local midnight)] for the last `days` local days, the oldest first."""
    from datetime import date

    today = datetime.fromtimestamp(now).date()
    out = []
    for k in range(days - 1, -1, -1):
        d: date = today - timedelta(days=k)
        out.append((d.isoformat(), datetime.combine(d, dtime.min).timestamp()))
    return out


def _models_efforts(rows: list[dict]) -> tuple[list[str], list[str]]:
    return _ordered(rows, "model"), [e for e in _ordered(rows, "effort") if e != "-"]


def empty_report(days: int, *, now: float | None = None) -> dict:
    """The report of a project with no recorded spend (collector off, no `tokens.db`): zero days, no rows."""
    now = time.time() if now is None else now
    return {"totals": {"total": 0, "output": 0, "input": 0, "cache_read": 0, "cache_write": 0, "cache_share": 0.0},
            "days": [{"day": d, "ts": ts, "total": 0, "output": 0} for d, ts in _day_starts(now, days)],
            "agents": [], "tickets": [], "top_ticket": None}


def project_report(idx_db: sqlite3.Connection | None, project: str, days: int, *,
                   tdb: sqlite3.Connection | None = None, now: float | None = None) -> dict:
    """Spend of one project for the last `days` local days (the UI «Витрати» tab). Whitelisted fields only."""
    now = time.time() if now is None else now
    starts = _day_starts(now, days)
    since = starts[0][1]
    until = datetime.combine(datetime.fromtimestamp(now).date() + timedelta(days=1), dtime.min).timestamp()
    rows = usage(idx_db, tdb=tdb, project=project, since=since, until=until, by=("agent", "model", "effort", "ticket"))
    by_file = usage(idx_db, tdb=tdb, project=project, since=since, until=until, by=("agent", "ticket", "file"))
    by_day = {r["day"]: r for r in usage(idx_db, tdb=tdb, project=project, since=since, until=until, by=("day",))}

    totals = _sum_row()
    for r in rows:
        for c in ("requests", "input", "output", "cache_read", "cache_write"):
            totals[c] += r[c]
    _finish(totals)
    totals.pop("requests")

    day_rows = [{"day": d, "ts": ts, "total": (by_day.get(d) or {}).get("total", 0),
                 "output": (by_day.get(d) or {}).get("output", 0)} for d, ts in starts]

    groups: dict[tuple[str, bool], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["agent"], r["agent"] == "main" and r["ticket"] == ORCH), []).append(r)
    files: dict[tuple[str, bool], set] = {}      # runs = distinct transcript files of the row
    for r in by_file:
        files.setdefault((r["agent"], r["agent"] == "main" and r["ticket"] == ORCH), set()).add(r["file"])
    runs = {k: len(v) for k, v in files.items()}
    agents = []
    for (agent, orch), grp in groups.items():
        sm = _sum_row()
        for r in grp:
            for c in ("requests", "input", "output", "cache_read", "cache_write"):
                sm[c] += r[c]
        _finish(sm)
        models, efforts = _models_efforts(grp)
        agents.append({"agent": agent, "orch": orch, "models": models, "efforts": efforts,
                       "runs": runs.get((agent, orch), 0), "requests": sm["requests"], "output": sm["output"],
                       "total": sm["total"], "cache_share": sm["cache_share"]})
    agents.sort(key=lambda a: (-a["total"], a["agent"], a["orch"]))

    per_ticket: dict[str, dict] = {}
    for r in rows:
        if r["ticket"] in (ORCH, NO_TICKET):
            continue
        e = per_ticket.setdefault(r["ticket"], {"total": 0, "output": 0, "agents": {}})
        e["total"] += r["total"]
        e["output"] += r["output"]
        e["agents"][r["agent"]] = e["agents"].get(r["agent"], 0) + r["total"]
    titles: dict[str, str] = {}
    if idx_db is not None and per_ticket:
        try:
            for t in idx_db.execute("SELECT id, title FROM tickets WHERE project_path=?", (project,)):
                titles[t[0]] = t[1]
        except sqlite3.Error:
            pass
    tickets = []
    for tid, e in sorted(per_ticket.items(), key=lambda kv: (-kv[1]["total"], kv[0]))[:TOP_TICKETS]:
        top_agent, top_total = sorted(e["agents"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        tickets.append({"id": tid, "title": titles.get(tid), "total": e["total"], "output": e["output"],
                        "top_agent": top_agent, "top_share": top_total / e["total"] if e["total"] else 0.0})
    return {"totals": totals, "days": day_rows, "agents": agents, "tickets": tickets,
            "top_ticket": tickets[0] if tickets else None}


def ticket_card(idx_db: sqlite3.Connection | None, project: str, ticket: str, *,
                tdb: sqlite3.Connection | None = None) -> dict | None:
    """Spend block of one ticket card: the Log line plus rows per agent; None when nothing was recorded."""
    rows = usage(idx_db, tdb=tdb, project=project, ticket=ticket, by=("agent", "model", "effort"))
    if not rows:
        return None
    files = usage(idx_db, tdb=tdb, project=project, ticket=ticket, by=("session", "agent"))
    summary = _summarize(rows, files)
    by_agent = []
    for agent in _ordered(rows, "agent"):
        grp = [r for r in rows if r["agent"] == agent]
        models, efforts = _models_efforts(grp)
        by_agent.append({"agent": agent, "models": models, "efforts": efforts,
                         "runs": sum(r["runs"] for r in files if r["agent"] == agent),
                         "output": sum(r["output"] for r in grp), "total": sum(r["total"] for r in grp)})
    cache_read = sum(r["cache_read"] for r in rows)
    return {"summary": summary, "line": summary_line(summary), "by_agent": by_agent,
            "cache_share": cache_read / summary["total"] if summary["total"] else 0.0}
