"""`myvec serve`: local daemon (Starlette + uvicorn) on 127.0.0.1.

Warm DB connection + embedder, scheduler/watchers for *active* projects, event bus, JSON API, static UI.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import secrets as pysecrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from myvec import activity, config, daemon_client, filters, gitutil, logutil, projects, searchcmd, searchlog, vaultgit
from myvec import db as dbmod
from myvec import lock as lockmod
from myvec.bus import EventBus
from myvec.scheduler import Scheduler
from myvec.watcher import WatchManager, backend_name

UI_DIR = Path(__file__).parent / "ui"
REFRESH_EVERY = 60.0
KEEPALIVE_EVERY = 30 * 60.0
SSE_POLL = 0.5          # how often an open event stream checks the stop flag
SYNC_STOP_WAIT_S = 8      # lifespan shutdown: max wait for the running sync to see the cancel (TimeoutStopSec=15)
GRACEFUL_STOP_S = 3     # uvicorn: seconds to wait for open connections after SIGTERM
PROGRESS_EVERY = 0.5
ERR_LOG_MAX = 50 * 1024 * 1024   # bytes: daemon.err.log / daemon.out.log are cut in place above this (ADR-033)
TICKETS_DEBOUNCE = 1.5       # s between a file event under a tickets dir and the (light) tickets refresh
TICKETS_RESCAN = 5 * 60.0    # s between full rescans of all registered projects (dir fingerprint makes it cheap)
STATUS_TTL_S = 3.0             # s a /api/status payload is reused (single-flight across tabs)
USAGE_TTL_S = 30.0             # s a /api/search/usage payload is reused per period (E8-06)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_MEMORY_ID = re.compile(r"[0-9a-f]{8}")
_SECRET_GLOB_HINTS = ("env", "password", "secret", "pem", "key", "id_")


def _usage_clock() -> float:
    """Clock of the /api/search/usage cache; a seam so tests move it without patching global `time.monotonic`."""
    return time.monotonic()


# ------------------------------------------------------------------ security

class GuardMiddleware:
    """DNS-rebinding guard (Host) + CSRF for state-changing requests (Origin + X-CSRF-Token)."""

    def __init__(self, app, port_getter, token_getter):
        self.app = app
        self.port_getter = port_getter
        self.token_getter = token_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        port = self.port_getter()
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("host", "").lower() not in allowed:
            await self._deny(scope, receive, send, "bad Host header")
            return
        if scope["method"] not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                o = urlparse(origin)
                if o.scheme != "http" or o.netloc.lower() not in allowed:
                    await self._deny(scope, receive, send, "cross-origin request")
                    return
            sent = headers.get("x-csrf-token", "")
            if not hmac.compare_digest(sent.encode(), str(self.token_getter()).encode()):
                await self._deny(scope, receive, send, "missing or invalid CSRF token")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send, why: str) -> None:
        await JSONResponse({"error": "forbidden", "detail": why}, status_code=403)(scope, receive, send)


# ------------------------------------------------------------------ state

class DaemonState:
    def __init__(self, port: int, token: str | None = None, *, background: bool = False,
                 debounce: float = 10.0, safety: float = 900.0, tickets_debounce: float = TICKETS_DEBOUNCE,
                 tickets_rescan: float = TICKETS_RESCAN):
        self.port = port
        self.token = token or pysecrets.token_urlsafe(32)
        self.background = background
        self.stopping = threading.Event()
        self.started = time.time()
        self.bus = EventBus(config.logs_dir() / "events.jsonl")
        self.sched = Scheduler(debounce=debounce, safety=safety)
        self.watchers = WatchManager(self._on_fs_event)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="myvec-sync")
        self.ticket_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="myvec-tickets")
        self.tokens_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="myvec-tokens")
        self._sync_futures: set = set()
        self._tokens_future = None
        self._last_tokens_scan = float("-inf")
        self._last_tokens_full = float("-inf")
        self._tokens_cfg: config.Config | None = None   # config for the scan timer, re-read every `_TOKENS_CFG_S`
        self._tokens_cfg_at = float("-inf")
        self.tokens_info: dict = {"files": None, "requests": None, "last_scan": None, "pending_bytes": None,
                             "state": "ще не проскановано"}
        self.tickets_debounce = tickets_debounce
        self.tickets_rescan = tickets_rescan
        self._ticket_dirs: dict[str, Path] = {}   # active project path -> absolute tickets dir
        self._tickets_dirty: dict[str, float] = {}   # project path -> time of the last event under its tickets dir
        self._tickets_pending: set[str] = set()
        self._tickets_lock = threading.Lock()
        self._last_tickets_rescan = float("-inf")
        self.db_lock = threading.RLock()
        self.load = None                          # nodeload.Runner (sampler thread), ADR-012
        self._db: sqlite3.Connection | None = None
        self._db_ino: int | None = None           # inode of index.db the warm connection was opened on
        self._roots: dict[str, str] = {}          # active project path -> root
        self._queued: set[str] = set()
        self._qlock = threading.Lock()
        self.progress: dict[str, dict] = {}
        self._modes: dict[str, tuple] = {}
        self._last_keepalive = 0.0
        self._ollama_cache: tuple[float, bool | None] = (0.0, None)
        self._vault_cache: tuple[float, dict | None] = (0.0, None)
        self._queries_pos = self._queries_size()
        self._repo_stats_cache: dict[tuple, tuple] = {}   # (repo_id, branch, last_sync) -> (blobs, chunks, masked, dropped)
        self._repo_stats_lock = threading.Lock()
        self._status_lock = threading.Lock()              # single-flight for status(); never db_lock
        self._status_cache: tuple[float, dict] = (float("-inf"), {})
        self._usage_cache: dict[int, tuple[float, dict]] = {}   # days -> (monotonic ts, usage payload)
        self._usage_lock = threading.Lock()               # single-flight for search_usage(); never db_lock
        self._status_gen = 0                              # bumped by invalidate_status(); a build started earlier is not cached

    # ---- db
    def db(self, check: bool = False) -> sqlite3.Connection:
        """The warm connection. `check=True` re-verifies model/dims against config.toml (raises ModelMismatch)."""
        with self.db_lock:
            cfg = config.load_config()
            self._drop_if_replaced()
            if self._db is None:
                self._db = dbmod.connect(threads=True)
                self._db_ino = self._index_inode()
                dbmod.init_schema(self._db, cfg)
            if check:
                dbmod.check_meta(self._db, cfg)
            return self._db

    @staticmethod
    def _index_inode() -> int | None:
        try:
            return os.stat(config.db_path()).st_ino
        except OSError:
            return None

    def _drop_if_replaced(self) -> None:
        """index.db was deleted / replaced (e.g. `rm index.db && myvec rebuild`): the warm connection would keep
        serving the unlinked inode. Forget it so the next use reopens the new file. (Not closed here: requests that
        already hold it finish on the old one.)"""
        if self._db is not None and self._index_inode() != self._db_ino:
            self._db = None

    def close(self) -> None:
        with self.db_lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def project_rows(self) -> list[sqlite3.Row]:
        with self.db_lock:
            return self.db().execute("SELECT * FROM projects ORDER BY path").fetchall()

    def project_row(self, path: str) -> sqlite3.Row | None:
        with self.db_lock:
            return self.db().execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()

    # ---- activity / watchers
    def invalidate_status(self) -> None:
        """Drop the cached /api/status payload (call after a change the UI reads back right away)."""
        self._status_gen += 1
        self._status_cache = (float("-inf"), {})

    def refresh(self) -> None:
        """Fold queued accesses, recompute which projects are active, (un)start watchers, publish mode changes."""
        try:
            self._refresh()
        finally:
            self.invalidate_status()

    def _refresh(self) -> None:
        cfg = config.load_config()
        with self.db_lock:
            try:
                activity.fold_activity(self.db())
            except (sqlite3.Error, OSError):
                pass
            rows = self.project_rows()
        if self.load is not None:
            self.load.set_roots([(r["path"], r["root"]) for r in rows])
        now = time.time()
        active: dict[str, str] = {}
        for r in rows:
            mode = (activity.mode_of(r), activity.window_hours(r, cfg.window_hours))
            old = self._modes.get(r["path"])
            if old is not None and old != mode:
                self.bus.publish("mode_changed", project=r["path"], mode=mode[0], window_hours=mode[1])
            self._modes[r["path"]] = mode
            if activity.is_active(r, cfg.window_hours, now) and Path(r["root"]).is_dir():
                active[r["path"]] = r["root"]
        started, stopped = self.sched.set_active(set(active))
        self._roots = active
        dirs: dict[str, Path] = {}
        for k, root in active.items():
            m = projects.try_read_marker(Path(root))
            dirs[k] = Path(root) / (m.tickets_dir if m else projects.DEFAULT_TICKETS_DIR)
        self._ticket_dirs = dirs
        if self.background:
            for k in started:
                self.watchers.start(k, Path(active[k]))
        for k in started:
            self.bus.publish("watcher_start", project=k, root=active[k], watching=self.background,
                             backend=backend_name())
        for k in stopped:
            self.watchers.stop(k)
            self.bus.publish("watcher_stop", project=k)
        if active and self.background and time.time() - self._last_keepalive > KEEPALIVE_EVERY:
            self._last_keepalive = time.time()
            threading.Thread(target=self._keepalive, args=(cfg,), daemon=True).start()

    def _keepalive(self, cfg: config.Config) -> None:
        """Keep the Ollama embedding model loaded while any project is active."""
        if os.environ.get("MYVEC_EMBEDDER") == "fake" or cfg.embed_api == "openai":
            return
        try:
            import httpx

            httpx.post(cfg.ollama_url.rstrip("/") + "/api/embed", timeout=30.0,
                       json={"model": cfg.model, "input": "ping", "keep_alive": cfg.keep_alive or "2h"})
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _on_fs_event(self, _key: str, path: str) -> None:
        """Called from watchdog threads: attribute the event to the deepest active project containing it."""
        best = None
        for k, root in self._roots.items():
            r = root.rstrip("/")
            if (path == r or path.startswith(r + "/")) and (best is None or len(root) > len(self._roots[best])):
                best = k
        if best is not None:
            td = self._ticket_dirs.get(best)
            if td is not None and path.startswith(str(td) + "/inbox/"):
                return   # inbox правок з UI (ADR-019): не тікети; sync теж не потрібен
            self.sched.notify(best)
            if td is not None and (path == str(td) or path.startswith(str(td) + "/")):
                with self._tickets_lock:
                    self._tickets_dirty[best] = time.monotonic()

    # ---- sync
    def request_sync(self, path: str, reason: str) -> bool:
        """Queue a sync of project `path`. False if unknown or already queued/running."""
        row = self.project_row(path)
        if row is None:
            return False
        with self._qlock:
            if path in self._queued:
                return False
            self._queued.add(path)
        self.sched.started(path)
        fut = self.executor.submit(self._run_sync, path, row["root"], reason)
        self._sync_futures.add(fut)
        fut.add_done_callback(self._sync_futures.discard)
        return True

    def wait_sync_stopped(self, timeout: float = SYNC_STOP_WAIT_S) -> bool:
        """Block until in-flight sync tasks end (after `stopping.set()` they stop at the next check)."""
        from concurrent.futures import wait
        _, pending = wait(list(self._sync_futures), timeout=timeout)
        return not pending

    def _run_sync(self, path: str, root: str, reason: str) -> None:
        from myvec import indexer
        from myvec.embed import EmbedError, get_embedder

        retry = False
        t0 = time.perf_counter()
        stats = None
        try:
            if self.stopping.is_set():  # queued before the stop: never start
                return
            marker = projects.read_marker(Path(root))
            if marker is None:
                self.bus.publish("sync_failed", project=path, error=f"no {projects.MARKER} in {root}")
                return
            self.bus.publish("sync_started", project=path, reason=reason)
            with lockmod.sync_lock() as got:
                if not got:
                    retry = True
                    self.bus.publish("sync_busy", project=path, detail="another sync holds the lock; will retry")
                    return
                cfg = config.load_config()
                db = dbmod.open_ready(cfg)
                last = [0.0]

                def on_progress(st) -> None:
                    pct = round(100.0 * st.blobs_done / st.blobs_total, 1) if st.blobs_total else None
                    info = {"project": path, "files": st.files, "blobs_done": st.blobs_done,
                            "blobs_total": st.blobs_total, "chunks_embedded": st.chunks_embedded, "percent": pct}
                    self.progress[path] = info
                    if time.time() - last[0] >= PROGRESS_EVERY:
                        last[0] = time.time()
                        self.bus.publish("sync_progress", **info)

                stats = indexer.SyncStats(progress_cb=on_progress, cancel=self.stopping.is_set)
                try:
                    indexer.sync_project(db, cfg, get_embedder(cfg), marker, lambda s: None, stats)
                finally:
                    db.close()
            dt = time.perf_counter() - t0
            self._sync_log(f"synced {path}: {stats.repos} repos, {stats.files} files, {stats.new_blobs} new blobs, "
                           f"{stats.chunks_embedded} chunks embedded, {stats.secrets_masked} secret values masked, "
                           f"{stats.secrets_skipped} secret chunks skipped, {dt:.1f}s (daemon, {reason})")
            self.bus.publish("sync_finished", project=path, reason=reason, repos=stats.repos, files=stats.files,
                             new_blobs=stats.new_blobs, chunks_embedded=stats.chunks_embedded,
                             masked=stats.secrets_masked, dropped=stats.secrets_skipped,
                             errors=stats.errors, seconds=round(dt, 2))
        except indexer.SyncCancelled:
            done = stats.blobs_done if stats else 0
            self._sync_log(f"sync of {path} cancelled: daemon stopping ({done} blobs stored, resumes next sync)")
            self.bus.publish("sync_cancelled", project=path, reason="daemon stopping", blobs_done=done,
                             blobs_total=stats.blobs_total if stats else 0,
                             seconds=round(time.perf_counter() - t0, 2))
        except (EmbedError, dbmod.ModelMismatch) as e:
            self._sync_log(f"daemon sync of {path} failed: {e}")
            self.bus.publish("sync_failed", project=path, error=str(e))
        except Exception as e:  # noqa: BLE001 - the daemon must survive anything
            self._sync_log(f"daemon sync of {path} crashed: {e!r}")
            self.bus.publish("sync_failed", project=path, error=repr(e))
        finally:
            self.progress.pop(path, None)
            with self._qlock:
                self._queued.discard(path)
            self.invalidate_status()
            self.sched.finished(path, retry=retry)

    @staticmethod
    def _sync_log(msg: str) -> None:
        logutil.append(config.logs_dir() / "sync.log", msg)

    # ---- periodic work
    def tick(self) -> None:
        for k in self.sched.due():
            self.request_sync(k, "watcher")
        self._tick_tickets()
        self._tail_queries()
        self._tick_sessions()
        self._tick_test_runs()
        self._tick_tokens()

    def _tick_sessions(self) -> None:
        """Fold the hook journal into `sessions` (ADR-007); a failure keeps the claimed events for the next tick."""
        from myvec import sessions

        try:
            if not sessions.pending():
                return
            cfg = config.load_config()
            with self.db_lock:
                changes = sessions.fold(self.db(), retention_days=cfg.session_retention_days)
        except Exception as e:  # noqa: BLE001 - the daemon must survive anything
            self._sync_log(f"sessions fold failed: {e!r}")
            return
        for c in changes:
            self.bus.publish("session_changed", session_id=c["session_id"], state=c["state"], project=c["project"])

    def _tick_tokens(self) -> None:
        """Start a transcript scan (ADR-013) in its own thread every `tokens_scan_s`; a running one is not doubled."""
        if not self.background:
            return
        now = time.monotonic()
        if self._tokens_future is not None and not self._tokens_future.done():
            return
        cfg = self._tokens_cfg
        if cfg is None or now - self._tokens_cfg_at >= self._TOKENS_CFG_S:
            cfg = self._tokens_cfg = config.load_config()
            self._tokens_cfg_at = now
        if not cfg.tokens_enabled or now - self._last_tokens_scan < cfg.tokens_scan_s:
            return
        self._last_tokens_scan = now
        from myvec import tokens as tokens_mod

        full = now - self._last_tokens_full >= tokens_mod.FULL_SCAN_S
        if full:
            self._last_tokens_full = now
        self._tokens_future = self.tokens_executor.submit(self._scan_tokens, cfg, full)

    _TOKENS_CFG_S = 60.0

    def _scan_tokens(self, cfg: config.Config, full: bool = True) -> None:
        from myvec import tokens

        try:
            tdb = tokens.open_db()
            try:
                tokens.scan(tdb, config.claude_projects_root(cfg), budget_bytes=cfg.tokens_scan_budget_mb << 20,
                              full=full)
                self.tokens_info = {**tokens.stats(tdb), "state": "ok"}
            finally:
                tdb.close()
        except Exception as e:  # noqa: BLE001 - the daemon must survive anything
            self._sync_log(f"tokens scan failed: {e!r}")

    def _tick_test_runs(self) -> None:
        """Fold the `myvec measure` fallback journal into `test_runs` (ADR-011)."""
        from myvec import measure

        try:
            if not measure.pending():
                return
            with self.db_lock:
                measure.fold(self.db())
        except Exception as e:  # noqa: BLE001 - the daemon must survive anything
            self._sync_log(f"test_runs fold failed: {e!r}")

    # ---- tickets fast path (ADR-005): no sync_lock, no embedder
    def _tick_tickets(self) -> None:
        now = time.monotonic()
        with self._tickets_lock:
            due = [k for k, t in self._tickets_dirty.items()
                   if now - t >= self.tickets_debounce and k not in self._tickets_pending]
            for k in due:
                del self._tickets_dirty[k]
                self._tickets_pending.add(k)
        for k in due:
            root = self._roots.get(k)
            if root is None:
                with self._tickets_lock:
                    self._tickets_pending.discard(k)
                continue
            self.ticket_executor.submit(self._refresh_tickets, k, root)
        if now - self._last_tickets_rescan >= self.tickets_rescan:
            self._last_tickets_rescan = now
            self.ticket_executor.submit(self._rescan_tickets)

    def _refresh_tickets(self, key: str, root: str) -> None:
        try:
            self._refresh_tickets_of(key, root)
        except Exception as e:  # noqa: BLE001 - the daemon must survive anything
            self._sync_log(f"tickets refresh of {key} failed: {e!r}")
            self.bus.publish("daemon_error", error=f"tickets {key}: {e!r}")
        finally:
            with self._tickets_lock:
                self._tickets_pending.discard(key)

    def _refresh_tickets_of(self, key: str, root: str, db: sqlite3.Connection | None = None) -> None:
        from myvec import ticketdb

        own = db is None
        db = db or dbmod.open_ready()
        try:
            m = projects.try_read_marker(Path(root))
            tdir = m.tickets_dir if m else projects.DEFAULT_TICKETS_DIR
            res = ticketdb.refresh_project(db, key, root, tdir, bad_dir=m.bad_tickets_dir if m else None)
        finally:
            if own:
                db.close()
        if res.changed:
            self.bus.publish("tickets_updated", project=key, n_tickets=res.n_tickets, n_errors=res.n_errors)

    def _rescan_tickets(self) -> None:
        """All registered projects (also inactive ones): the dir fingerprint makes an unchanged one a few stats."""
        try:
            db = dbmod.open_ready()
            try:
                rows = db.execute("SELECT path, root FROM projects ORDER BY path").fetchall()
                for r in rows:
                    if not Path(r["root"]).is_dir():
                        continue
                    try:
                        self._refresh_tickets_of(r["path"], r["root"], db)
                    except Exception as e:  # noqa: BLE001
                        self._sync_log(f"tickets rescan of {r['path']} failed: {e!r}")
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            self._sync_log(f"tickets rescan failed: {e!r}")
            self.bus.publish("daemon_error", error=f"tickets rescan: {e!r}")

    def _queries_size(self) -> int:
        try:
            return (config.logs_dir() / "queries.jsonl").stat().st_size
        except OSError:
            return 0

    def _tail_queries(self) -> None:
        """Direct-mode CLI searches append to logs/queries.jsonl; forward new lines to the bus."""
        p = config.logs_dir() / "queries.jsonl"
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size < self._queries_pos:
            self._queries_pos = 0  # rotated / truncated
        if size == self._queries_pos:
            return
        with open(p, "rb") as f:
            f.seek(self._queries_pos)
            chunk = f.read()
        nl = chunk.rfind(b"\n")
        if nl < 0:
            return
        self._queries_pos += nl + 1
        for ln in chunk[: nl + 1].decode("utf-8", "replace").splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            rec.pop("type", None)
            self.bus.publish("search", **rec)

    # ---- status
    def ollama_reachable(self) -> bool | None:
        if os.environ.get("MYVEC_EMBEDDER") == "fake":
            return None
        ts, val = self._ollama_cache
        if time.time() - ts < 10:
            return val
        try:
            import httpx

            c = config.load_config()
            path = "/health" if c.embed_api == "openai" else "/api/tags"
            val = httpx.get(c.ollama_url.rstrip("/") + path, timeout=0.4).status_code == 200
        except Exception:  # noqa: BLE001
            val = False
        self._ollama_cache = (time.time(), val)
        return val

    def status(self) -> dict:
        """Payload of /api/status: the heavy part is cached for `STATUS_TTL_S` and single-flight (several tabs share
        one computation, no `db_lock`). The cheap live fields (`now`, `tokens`, `load`, per-project `syncing` /
        `progress` / `watching`, `daemon.watching`) are re-read on every call."""
        with self._status_lock:
            ts, val = self._status_cache
            if time.monotonic() - ts >= STATUS_TTL_S:
                gen = self._status_gen
                val = self._build_status()
                if gen == self._status_gen:   # an invalidation during the build makes this payload stale: serve, don't cache
                    self._status_cache = (time.monotonic(), val)
        return self._overlay_live(val)

    def search_usage(self, days: int) -> dict:
        """Payload of /api/search/usage (E8-06, ADR-037): `searchstats.usage` over read-only connections like
        `myvec searches` (no transcript scan, no `db_lock`), cached `USAGE_TTL_S` per period and single-flight.
        Errors are not cached."""
        from myvec import searchstats, tokens
        with self._usage_lock:
            hit = self._usage_cache.get(days)
            if hit and _usage_clock() - hit[0] < USAGE_TTL_S:
                return hit[1]
            idx = tdb = None
            try:
                idx = dbmod.connect(vec=False, readonly=True) if config.db_path().exists() else None
                tdb = tokens.open_db(readonly=True) if config.tokens_db_path().exists() else None
                val = searchstats.usage(idx, tdb, days)
            finally:
                for c in (idx, tdb):
                    if c is not None:
                        c.close()
            self._usage_cache[days] = (_usage_clock(), val)
            return val

    def _overlay_live(self, base: dict) -> dict:
        def walk(nodes: list[dict]) -> list[dict]:
            out = []
            for n in nodes:
                out.append({**n, "watching": n["path"] in self.watchers.watching, "syncing": n["path"] in self._queued,
                            "progress": self.progress.get(n["path"]), "children": walk(n["children"])})
            return out

        g = base["global"]
        return {**base, "now": time.time(), "tokens": {"enabled": base["tokens"]["enabled"], **self.tokens_info},
                "projects": walk(base["projects"]),
                "global": {**g, "load": self.load_status(),
                           "daemon": {**g["daemon"], "watching": sorted(self.watchers.watching)}}}

    def _build_status(self) -> dict:
        cfg = config.load_config()
        now = time.time()
        pending: list[tuple[dict, sqlite3.Row]] = []
        with self.db_lock:
            db = self.db()
            rows = db.execute("SELECT * FROM projects ORDER BY path").fetchall()
            nodes: dict[str, dict] = {}
            for p in rows:
                node = {"path": p["path"], "root": p["root"], "share_global": bool(p["share_global"]),
                        **activity.describe(p, cfg.window_hours, now),
                        "watching": p["path"] in self.watchers.watching,
                        "syncing": p["path"] in self._queued, "progress": self.progress.get(p["path"]),
                        "exclude": self._marker_excludes(p["root"]), "repos": [], "children": []}
                for r in db.execute("SELECT * FROM repos WHERE project_path=? ORDER BY path", (p["path"],)):
                    pending.append((node, r))
                nodes[p["path"]] = node
            tree: list[dict] = []
            for path, node in nodes.items():
                parts = path.split("/")
                parent = next(("/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)
                               if "/".join(parts[:i]) in nodes), None)
                (nodes[parent]["children"] if parent else tree).append(node)
            mem = {"total": 0, "archived": 0, "by_scope": {}}
            for r in db.execute("SELECT scope, superseded_by IS NOT NULL AS arch, count(*) n FROM memories "
                                "GROUP BY scope, arch"):
                if r["arch"]:
                    mem["archived"] += r["n"]
                else:
                    mem["total"] += r["n"]
                    mem["by_scope"][r["scope"]] = mem["by_scope"].get(r["scope"], 0) + r["n"]
            meta = {r[0]: r[1] for r in db.execute("SELECT key, value FROM meta")}
        if pending:   # heavy per-repo counts: cached, and computed on a read-only connection outside db_lock
            ro = self._ro_db()
            try:
                for node, r in pending:
                    node["repos"].append(self._repo_info(ro, r))
                with self._repo_stats_lock:   # forget removed repos / branches
                    live = {(r["id"], r["current_branch"], r["last_sync"]) for _, r in pending}
                    for k in [k for k in self._repo_stats_cache if k not in live]:
                        del self._repo_stats_cache[k]
            finally:
                ro.close()
        size = sum(f.stat().st_size for f in config.home().glob("index.db*") if f.is_file())
        return {
            "now": now,
            "tokens": {"enabled": cfg.tokens_enabled, **self.tokens_info},
            "projects": tree,
            "global": {
                "model": meta.get("model"), "dims": int(meta["dims"]) if meta.get("dims") else None,
                "db_size_bytes": size, "memories": mem,
                "config": {"model": cfg.model, "dims": cfg.dims, "window_hours": cfg.window_hours, "k": cfg.k,
                           "expand_top": cfg.expand_top, "expand_lines": cfg.expand_lines,
                           "max_memory_hits": cfg.max_memory_hits, "import_weight": cfg.import_weight,
                           "weight_softness": cfg.weight_softness, "weights": cfg.weights},
                "model_mismatch": bool(meta.get("model") and (meta.get("model") != cfg.model
                                                              or meta.get("dims") != str(cfg.dims))),
                "home": str(config.home()), "vault": self.vault_info(), "searches": self.search_stats(),
                "ollama": {"url": cfg.ollama_url, "reachable": self.ollama_reachable()},
                "daemon": {"port": self.port, "pid": os.getpid(), "started": self.started,
                           "watching": [], "window_hours": cfg.window_hours,   # live: set by _overlay_live
                           "debounce_s": self.sched.debounce, "safety_s": self.sched.safety,
                           "watch_backend": backend_name()},
                "user_home": str(Path.home()),
                "load": None,   # live: set by _overlay_live
            },
        }

    def start_load(self, cfg: config.Config) -> None:
        """Start the node load sampler thread (ADR-012); without psutil or with `load_enabled = false` it stays off."""
        if not cfg.load_enabled:
            return
        try:
            from myvec import nodeload
            self.load = nodeload.Runner(nodeload.SystemSource(cfg.load_temp_sensor), nodeload.ProjectMap(),
                                        interval=cfg.load_sample_s, raw_hours=cfg.load_raw_hours,
                                        minutes_days=cfg.load_minutes_days, alert_pct=cfg.load_alert_pct,
                                        alert_minutes=cfg.load_alert_minutes)
        except ImportError:
            self.load = None
            return
        self.load.start()

    def load_status(self) -> dict:
        if self.load is None:
            return {"enabled": False, "unavailable": True}
        return self.with_alert_run(self.load.status())

    def with_alert_run(self, d: dict) -> dict:
        """Attach `alert.run`: this host's running test run of the culprit project (already masked), else None."""
        a = d.get("alert")
        if not a or not a.get("project"):
            return d
        from myvec import testruns
        run = None
        try:
            with self.db_lock:
                for r in testruns.running(self.db(), regressions=False):
                    if r["project_path"] == a["project"]:
                        run = {k: r[k] for k in ("run_uid", "command", "label", "ticket_id", "agent")}
                        break
        except sqlite3.Error:
            pass
        return {**d, "alert": {**a, "run": run}}

    @staticmethod
    def _marker_excludes(root: str) -> list[str]:
        m = projects.try_read_marker(Path(root))
        return list(m.exclude) if m else []

    def vault_info(self) -> dict:
        """Vault location + newest commit time (cached: it shells out to git)."""
        ts, val = self._vault_cache
        if val is not None and time.time() - ts < 15:
            return val
        v = config.vault_dir()
        val = {"path": str(v), "git": (v / ".git").exists(), "last_commit": vaultgit.last_commit_time()}
        self._vault_cache = (time.time(), val)
        return val

    def search_stats(self) -> dict:
        """Searches seen on the bus during the last 24 h: count and latency percentiles."""
        evs = self._search_events(86400)
        return {"count_24h": len(evs), **_percentiles([e["ms"] for e in evs if isinstance(e.get("ms"), (int, float))])}

    def _search_events(self, window: float) -> list[dict]:
        cutoff = time.time() - window
        return [e for e in self.bus.recent(0) if e["type"] == "search" and e["ts"] >= cutoff]

    def project_searches(self, path: str, limit: int = 20) -> dict | None:
        """Recent searches of project `path` (and its children) from the event bus, newest first."""
        with self.db_lock:
            rows = [(r["path"], r["root"]) for r in self.project_rows()]
        if path not in {p for p, _ in rows}:
            return None

        def project_of(ev: dict) -> str | None:
            if ev.get("project"):
                return ev["project"]
            cwd = str(ev.get("cwd") or "").rstrip("/")   # events without a project (older CLI/daemon records)
            best = None
            for p, root in rows:
                r = root.rstrip("/")
                if (cwd == r or cwd.startswith(r + "/")) and (best is None or len(r) > len(best[1])):
                    best = (p, r)
            return best[0] if best else None

        evs = [e for e in self._search_events(86400)
               if (project_of(e) or "") == path or (project_of(e) or "").startswith(path + "/")]
        evs.sort(key=lambda e: e["id"], reverse=True)
        pct = _percentiles([e["ms"] for e in evs if isinstance(e.get("ms"), (int, float))])
        return {"searches": evs[:limit], "total_24h": len(evs), **pct}

    def project_detail(self, path: str) -> dict | None:
        """Everything the project page needs beyond /api/status: per-branch manifest stats, excludes, secret
        locations (file + counts only - values and key names are never stored) and watcher parameters."""
        cfg = config.load_config()
        now = time.time()
        with self.db_lock:
            db = self.db()
            p = db.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
            if p is None:
                return None
            repo_rows = db.execute("SELECT * FROM repos WHERE project_path=? ORDER BY path", (path,)).fetchall()
            desc = activity.describe(p, cfg.window_hours, now)
            root = p["root"]
            watching = path in self.watchers.watching
            syncing = path in self._queued
        ro = self._ro_db()   # heavy per-branch counts outside the db lock
        try:
            repos = [self._repo_detail(ro, r) for r in repo_rows]
        finally:
            ro.close()
        for r in repos:   # git calls outside the db lock
            self._branch_states(r)
        secrets = {"masked": sum(r["masked"] for r in repos), "dropped": sum(r["dropped"] for r in repos),
                   "files": [f for r in repos for f in r.pop("secret_files")][:20]}
        safety = self.sched.safety_in(path)
        return {
            "path": path, "root": root, "config_file": str(Path(root) / projects.MARKER), **desc,
            "watching": watching, "syncing": syncing, "progress": self.progress.get(path),
            "repos": repos, "secrets": secrets,
            "exclude": {"project": self._marker_excludes(root), "gitignore": _gitignore_lines(Path(root)),
                        "dirs": sorted(filters.DENY_DIRS),
                        "secrets": [g for g in filters.DENY_FILE_GLOBS if any(h in g for h in _SECRET_GLOB_HINTS)],
                        "files": [g for g in filters.DENY_FILE_GLOBS
                                  if not any(h in g for h in _SECRET_GLOB_HINTS)]},
            "watch": {"watching": watching, "repos": len(repos), "debounce_s": self.sched.debounce,
                      "safety_s": self.sched.safety,
                      "next_safety": (now + safety) if safety is not None else None},
        }

    def _repo_detail(self, db, r) -> dict:
        info = self._repo_info(db, r)
        rid, cur = r["id"], r["current_branch"]
        stats = {x["branch"]: x for x in db.execute(
            "SELECT branch, count(*) AS files, coalesce(sum(length(path) + length(blob_sha)), 0) AS mbytes, "
            "max(seen_at) AS updated FROM branch_files WHERE repo_id=? GROUP BY branch", (rid,))}
        chunk_by = dict(db.execute(
            "SELECT bf.branch, count(DISTINCT c.id) FROM branch_files bf JOIN chunks c ON c.blob_sha=bf.blob_sha "
            "WHERE bf.repo_id=? GROUP BY bf.branch", (rid,)).fetchall())
        unique = db.execute("SELECT count(DISTINCT c.id) FROM branch_files bf JOIN chunks c "
                            "ON c.blob_sha=bf.blob_sha WHERE bf.repo_id=?", (rid,)).fetchone()[0]
        branches = [{"branch": b, "files": st["files"], "manifest_bytes": st["mbytes"], "updated": st["updated"],
                     "chunks": chunk_by.get(b, 0), "current": b == cur, "state": "current" if b == cur else "known"}
                    for b, st in stats.items()]
        branches.sort(key=lambda b: (not b["current"], -(b["updated"] or 0), b["branch"]))
        files = [{"repo": r["path"], "path": x["path"], "masked": x["secret_masked"], "dropped": x["secret_skipped"]}
                 for x in db.execute(
                     "SELECT bf.path, b.secret_masked, b.secret_skipped FROM branch_files bf "
                     "JOIN blobs b ON b.sha=bf.blob_sha WHERE bf.repo_id=? AND bf.branch=? "
                     "AND (b.secret_masked>0 OR b.secret_skipped>0) ORDER BY bf.path LIMIT 20", (rid, cur))]
        cur_stat = stats.get(cur)
        info.pop("branches", None)
        return {**info, "files": cur_stat["files"] if cur_stat else 0,
                "manifest_bytes": cur_stat["mbytes"] if cur_stat else 0, "unique_chunks": unique,
                "branches": branches, "secret_files": files}

    @staticmethod
    def _branch_states(repo: dict) -> None:
        """Mark non-current branches: `gone` (no local branch any more), `merged` (into the default branch)."""
        d = repo["dir"]
        try:
            if not gitutil.is_repo(d):
                return
            local, default = gitutil.local_branches(d), gitutil.default_branch(d)
            for b in repo["branches"]:
                if b["current"]:
                    continue
                if b["branch"] not in local:
                    b["state"] = "gone"
                elif default and b["branch"] != default and gitutil.is_ancestor(d, b["branch"], default):
                    b["state"], b["merged_into"] = "merged", default
        except Exception:  # noqa: BLE001 - decoration only; a missing/broken checkout keeps "known"
            pass

    @staticmethod
    def _ro_db() -> sqlite3.Connection:
        """Short-lived read-only connection to index.db for heavy reads that must not hold `db_lock`."""
        db = sqlite3.connect(f"file:{config.db_path()}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _repo_info(self, db, r) -> dict:
        rid, br = r["id"], r["current_branch"]
        branches = [x[0] for x in db.execute("SELECT DISTINCT branch FROM branch_files WHERE repo_id=? ORDER BY branch", (rid,))]
        key = (rid, br, r["last_sync"])
        with self._repo_stats_lock:
            hit = self._repo_stats_cache.get(key)
        if hit is None:
            hit = self._repo_stats(db, rid, br)
            with self._repo_stats_lock:
                for k in [k for k in self._repo_stats_cache if k[0] == rid]:
                    del self._repo_stats_cache[k]
                self._repo_stats_cache[key] = hit
        blobs, chunks, masked, dropped = hit
        return {"path": r["path"], "dir": r["dir"], "current_branch": br, "branches": branches, "blobs": blobs,
                "chunks": chunks, "last_sync": r["last_sync"], "masked": masked, "dropped": dropped}

    @staticmethod
    def _repo_stats(db, rid, br) -> tuple:
        blobs = db.execute("SELECT count(DISTINCT blob_sha) FROM branch_files WHERE repo_id=? AND branch=?", (rid, br)).fetchone()[0]
        chunks = db.execute("SELECT count(DISTINCT c.id) FROM chunks c JOIN branch_files bf ON bf.blob_sha=c.blob_sha "
                            "WHERE bf.repo_id=? AND bf.branch=?", (rid, br)).fetchone()[0]
        masked, dropped = db.execute(
            "SELECT coalesce(sum(secret_masked),0), coalesce(sum(secret_skipped),0) FROM blobs WHERE sha IN "
            "(SELECT blob_sha FROM branch_files WHERE repo_id=? AND branch=?)", (rid, br)).fetchone()
        return blobs, chunks, masked, dropped

    # ---- memories
    def memory_item(self, r: sqlite3.Row) -> dict:
        return {"id": r["id"], "scope": r["scope"], "text": r["text"], "branch": r["branch"],
                "origin_branch": r["origin_branch"], "origin_repo": r["origin_repo"], "commit": r["commit"],
                "created": r["created"], "updated": r["updated"], "pinned": bool(r["pinned"]),
                "tags": [t for t in (r["tags"] or "").split(",") if t], "source": r["source"],
                "archived": r["superseded_by"] is not None, "vault_file": r["vault_file"],
                "related": self._related(r["text"])}

    @staticmethod
    def _related(text: str) -> list[dict]:
        """[[wikilinks]] of a memory body; `id` is the trailing 8-hex id of the vault file name, when there is one."""
        out = []
        for stem in dict.fromkeys(m.strip() for m in _WIKILINK.findall(text or "")):
            tail = stem.rsplit("-", 1)[-1]
            out.append({"stem": stem, "id": tail if _MEMORY_ID.fullmatch(tail) else None})
        return out


# ------------------------------------------------------------------ app

def _percentiles(xs: list[float]) -> dict:
    xs = sorted(xs)
    if not xs:
        return {"p50_ms": None, "p95_ms": None}

    def pct(p: float) -> float:
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)

    return {"p50_ms": pct(0.5), "p95_ms": pct(0.95)}


def _gitignore_lines(root: Path, limit: int = 8) -> list[str]:
    try:
        lines = [ln.strip() for ln in (root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return []
    return [ln for ln in lines if ln and not ln.startswith("#")][:limit]


def _bool(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes", "on")



class _RevalidatedStatic(StaticFiles):
    """UI files without Cache-Control were cached heuristically by the browser (ES modules included),
    so after `uv tool install --reinstall` the old UI kept running. `no-cache` = always revalidate via ETag."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

def create_app(state: DaemonState) -> Starlette:
    from myvec import ctx as ctxmod
    from myvec import memory
    from myvec import search as S
    from myvec.embed import EmbedError, get_embedder

    def err(msg: str, code: int = 400) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=code)

    async def body_json(request: Request) -> dict:
        try:
            b = await request.json()
            return b if isinstance(b, dict) else {}
        except ValueError:
            return {}

    # ---- pages
    async def index(request: Request):
        html = (UI_DIR / "index.html").read_text(encoding="utf-8").replace("{{CSRF_TOKEN}}", state.token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ---- status / projects
    def api_status(request: Request):
        try:
            return JSONResponse(state.status())
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    # ---- node load (ADR-012; read-only)
    def _load_off() -> JSONResponse:
        return JSONResponse({"enabled": False}, status_code=503)

    async def api_load(request: Request):
        if state.load is None:
            return _load_off()
        return JSONResponse(await asyncio.to_thread(lambda: state.with_alert_run(state.load.current())))

    async def api_load_series(request: Request):
        if state.load is None:
            return _load_off()
        period = request.query_params.get("period", "1h")
        if period not in ("1h", "24h", "30d"):
            return err("period must be 1h, 24h or 30d")
        try:
            return JSONResponse(await asyncio.to_thread(state.load.series, period))
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    async def api_load_projects(request: Request):
        if state.load is None:
            return _load_off()
        try:
            minutes = min(60.0, max(1.0, float(request.query_params.get("minutes", "1"))))
        except ValueError:
            return err("minutes must be a number")
        return JSONResponse(await asyncio.to_thread(state.load.projects, minutes))

    # ---- test runs (ADR-011, ADR-015; read-only)
    def _regression_rule() -> dict:
        c = config.load_config()
        return {"pct": c.test_regression_pct, "runs": c.test_regression_runs, "min_runs": c.test_regression_min_runs,
                "min_abs_s": c.test_regression_min_abs_s}

    async def api_tests_runs(request: Request):
        from myvec import testruns
        rule = _regression_rule()
        qp = request.query_params
        project = qp.get("project") or None
        try:
            limit = int(qp.get("limit", "50"))
            before = float(qp["before"]) if qp.get("before") else None
            if before is not None and not math.isfinite(before):
                raise ValueError
        except ValueError:
            return err("limit must be an integer and before a number")
        if not 1 <= limit <= 200:
            return err("limit must be 1..200")

        def work():
            with state.db_lock:
                db = state.db()
                if project is not None and state.project_row(project) is None:
                    return None
                out = {"runs": testruns.list_runs(db, project, limit, before, rule),
                       "regression_rule": rule}
                if project is not None:
                    out["summary"] = testruns.summary(db, project)
                return out
        try:
            d = await asyncio.to_thread(work)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown project {project!r}", 404)

    # ---- token spend (ADR-013, ADR-016; read-only, own read-only connections, outside db_lock)
    def tokens_head() -> dict:
        enabled = config.load_config().tokens_enabled
        info = {"enabled": enabled, "last_scan": None, "pending_bytes": None}
        if enabled and config.tokens_db_path().exists():
            info.update(last_scan=state.tokens_info.get("last_scan"), pending_bytes=state.tokens_info.get("pending_bytes"))
        return info

    def tokens_ready(info: dict) -> bool:
        return bool(info["enabled"]) and config.tokens_db_path().exists()

    def open_index_ro():
        return dbmod.connect(vec=False, readonly=True) if config.db_path().exists() else None

    async def api_tokens_project(request: Request):
        from myvec import tokens
        qp = request.query_params
        project = qp.get("project") or None
        try:
            days = int(qp.get("days", "14"))
        except ValueError:
            return err("days must be 7, 14 or 30")
        if days not in tokens.REPORT_DAYS:
            return err("days must be 7, 14 or 30")
        if project is None:
            return err("project is required")

        def work():
            if state.project_row(project) is None:
                return None
            head = tokens_head()
            if not tokens_ready(head):
                return {**head, **tokens.empty_report(days)}
            idx = open_index_ro()
            try:
                return {**head, **tokens.project_report(idx, project, days)}
            finally:
                if idx is not None:
                    idx.close()
        try:
            d = await asyncio.to_thread(work)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown project {project!r}", 404)

    async def api_search_usage(request: Request):
        from myvec import tokens
        try:
            days = int(request.query_params.get("days", "14"))
        except ValueError:
            return err("days must be 7, 14 or 30")
        if days not in tokens.REPORT_DAYS:
            return err("days must be 7, 14 or 30")
        try:
            return JSONResponse(await asyncio.to_thread(state.search_usage, days))
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    async def api_tokens_ticket(request: Request):
        from myvec import tokens
        qp = request.query_params
        project, tid = qp.get("project") or None, qp.get("id") or None
        if project is None or tid is None:
            return err("project and id are required")

        def work():
            if state.project_row(project) is None:
                return None
            head = tokens_head()
            card = None
            if tokens_ready(head):
                idx = open_index_ro()
                try:
                    card = tokens.ticket_card(idx, project, tid)
                finally:
                    if idx is not None:
                        idx.close()
            return {"enabled": head["enabled"], "card": card}
        try:
            d = await asyncio.to_thread(work)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown project {project!r}", 404)

    async def api_tests_current(request: Request):
        from myvec import testruns
        rule = _regression_rule()

        def work():
            with state.db_lock:
                db = state.db()
                return {"running": testruns.running(db, rule),
                        "recent": testruns.recent_finished(db, time.time() - 86400, 5, rule),
                        "regression_rule": rule}
        try:
            return JSONResponse(await asyncio.to_thread(work))
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    def _project(path: str):
        return state.project_row(path)

    async def api_project_detail(request: Request):
        path = request.path_params["path"]
        try:
            d = await asyncio.to_thread(state.project_detail, path)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown project {path!r}", 404)

    async def api_tickets(request: Request):
        def work():
            from myvec import ticketdb
            with state.db_lock:
                return {"projects": ticketdb.projects_summary(state.db(), include_empty=True, idle_min=_session_idle())}
        try:
            return JSONResponse(await asyncio.to_thread(work))
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    def _session_idle() -> float:
        try:
            return float(config.load_config().session_idle_minutes)
        except Exception:  # noqa: BLE001 - a broken config must not break ticket pages
            return 15.0

    async def api_tickets_tree(request: Request):
        path = request.path_params["path"]

        def work():
            from myvec import ticketdb
            idle = _session_idle()
            with state.db_lock:
                return ticketdb.project_tickets(state.db(), path, idle)
        try:
            d = await asyncio.to_thread(work)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown project {path!r}", 404)

    async def api_tickets_problems(request: Request):
        path = request.path_params["path"]

        def work():
            from myvec import ticketdb
            with state.db_lock:
                d = ticketdb.project_problems(state.db(), path, _session_idle())
            if d is not None:
                from myvec import problems
                problems.attach_branch(d)   # git outside db_lock, with a timeout
            return d
        try:
            d = await asyncio.to_thread(work)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown project {path!r}", 404)

    INBOX_BODY_MAX = 32 * 1024

    def _inbox_target(path: str, tid: str):
        """(root, tickets_dir) для відомого тікета або None."""
        with state.db_lock:
            db = state.db()
            row = db.execute("SELECT 1 FROM tickets WHERE project_path=? AND id=?", (path, tid)).fetchone()
            d = db.execute("SELECT dir FROM ticket_dirs WHERE project_path=?", (path,)).fetchone()
            p = db.execute("SELECT root FROM projects WHERE path=?", (path,)).fetchone()
        if row is None or d is None or p is None:
            return None
        return p["root"], d["dir"]

    async def api_inbox_get(request: Request):
        from myvec import filters, inbox
        path, tid = request.path_params["path"], request.path_params["tid"]
        try:
            tgt = await asyncio.to_thread(_inbox_target, path, tid)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        if tgt is None:
            return err(f"unknown ticket {tid!r} in {path!r}", 404)
        items = await asyncio.to_thread(inbox.pending, tgt[0], tgt[1], tid)
        for it in items:
            it["text"] = filters.mask_secrets(it["text"])[0]
        return JSONResponse({"items": items})

    async def api_inbox_post(request: Request):
        from myvec import inbox
        path, tid = request.path_params["path"], request.path_params["tid"]
        try:
            if int(request.headers.get("content-length") or 0) > INBOX_BODY_MAX:
                return err("тіло запиту завелике", 413)
        except ValueError:
            return err("bad content-length", 400)
        buf = bytearray()
        async for chunk in request.stream():   # не читаємо в пам'ять понад ліміт (chunked без Content-Length)
            buf += chunk
            if len(buf) > INBOX_BODY_MAX:
                return err("тіло запиту завелике", 413)
        try:
            b = json.loads(bytes(buf) or b"{}")
        except ValueError:
            return err("некоректний JSON", 400)
        if not isinstance(b, dict):
            return err("тіло має бути об'єктом", 400)
        try:
            tgt = await asyncio.to_thread(_inbox_target, path, tid)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        if tgt is None:
            return err(f"unknown ticket {tid!r} in {path!r}", 404)
        status = b.get("status")
        if status in ("", None):
            status = None
        elif not isinstance(status, str):
            return err("status має бути рядком", 400)
        try:
            item = await asyncio.to_thread(inbox.write, tgt[0], tgt[1], tid, b.get("text"), status)
        except inbox.InboxError as e:
            return err(e.message, {"bad_request": 400, "secret": 400, "full": 409, "unsafe_path": 400}.get(e.code, 500))
        state.bus.publish("inbox_write", project=path, ticket=tid, kind=item["kind"], text_len=len(item["text"]))
        return JSONResponse({"item": item}, status_code=201)

    async def api_tickets_get(request: Request):
        path, tid = request.path_params["path"], request.path_params["tid"]

        def work():
            from myvec import ticketdb
            idle = _session_idle()
            with state.db_lock:
                d = ticketdb.get_ticket(state.db(), path, tid, idle)
            if d is not None:
                _add_modes(d.get("sessions") or [])
            return d
        try:
            d = await asyncio.to_thread(work)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        return JSONResponse(d) if d is not None else err(f"unknown ticket {tid!r} in {path!r}", 404)

    # ---- sessions (ADR-010)
    def _add_modes(rows: list[dict]) -> None:
        """`mode` for ticket-card sessions (cached `cm ls`, no per-session cm calls)."""
        from myvec import cmctl, sessionact
        caps = cmctl.capabilities()
        ls_ok, ls_map = cmctl.ls() if caps["available"] else (False, {})
        procs = cmctl.process_session_ids() if caps["available"] else set()
        for s in rows:
            s["mode"] = sessionact.decide(sessionact.approx_who(s["session_id"], s, ls_ok, ls_map, procs), s, caps)
            s["answer_kind"] = sessionact.answer_kind(s, caps, s["mode"])

    def _sessions_payload(project: str | None, include_old: bool, only_waiting: bool = False) -> dict:
        import socket
        import time as _time

        from myvec import cmctl, sessionact
        from myvec import sessions as sess
        now = _time.time()
        caps = cmctl.capabilities()
        ls_ok, ls_map = cmctl.ls() if caps["available"] else (False, {})
        with state.db_lock:
            rows = sess.list_sessions(state.db(), now, _session_idle(), include_old_ended=include_old, project=project)
        procs = cmctl.process_session_ids() if caps["available"] else set()
        out = []
        for d in rows:
            if not sessionact.UUID_RE.fullmatch(d["session_id"]):
                continue
            waiting = d["effective_state"] == "waiting"
            if only_waiting and not waiting:
                continue
            d["wait_s"] = max(0.0, now - float(d["last_state_event_at"] or d["last_seen"] or now)) if waiting else None
            d["mode"] = sessionact.decide(sessionact.approx_who(d["session_id"], d, ls_ok, ls_map, procs), d, caps)
            d["answer_kind"] = sessionact.answer_kind(d, caps, d["mode"])
            out.append(d)
        if only_waiting:
            out.sort(key=lambda d: d["wait_s"], reverse=True)
        return {"node": socket.gethostname().split(".")[0], "cm": caps, "sessions": out}

    async def api_sessions(request: Request):
        q = request.query_params
        try:
            return JSONResponse(await asyncio.to_thread(
                _sessions_payload, q.get("project") or None, _bool(q.get("all"))))
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    async def api_sessions_waiting(request: Request):
        try:
            return JSONResponse(await asyncio.to_thread(_sessions_payload, None, False, True))
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)

    async def api_session_resume(request: Request):
        from myvec import sessionact
        sid = request.path_params["sid"]
        if not sessionact.UUID_RE.fullmatch(sid):
            return err("bad session id", 400)
        body = await body_json(request)
        text = body.get("text", "")
        bad = sessionact.valid_text(text)
        if bad:
            return err(bad, 400)
        option = body.get("option")
        if option is not None:
            if text:
                return err("передай або text, або option, не обидва", 400)
            if isinstance(option, bool) or not isinstance(option, int) or option < 1:
                return err("option має бути цілим числом від 1", 400)

        def lookup():
            from myvec import sessions as sess
            with state.db_lock:
                rows = sess.list_sessions(state.db(), idle_min=_session_idle(), include_old_ended=True)
            return next((d for d in rows if d["session_id"] == sid), None)
        try:
            row = await asyncio.to_thread(lookup)
        except sqlite3.Error as e:
            return err(f"database error: {e}", 500)
        if row is None:
            return err("unknown session", 404)
        qo = row.get("question_options")
        qat = body.get("question_at")
        if option is not None and qat is None:
            return err("для option потрібне question_at", 400)
        if qat is not None and (isinstance(qat, bool) or not isinstance(qat, (int, float))
                                or qat != row.get("question_at")):
            return err("питання змінилося, онови картку", 409)
        labels = (qo.get("labels") or []) if isinstance(qo, dict) else []
        if option is not None and labels and option > len(labels):
            return err(f"option поза діапазоном 1..{len(labels)}", 400)
        if option is not None and labels and body.get("label") != labels[option - 1]:
            return err("питання змінилося, онови картку", 409)
        res = await asyncio.to_thread(sessionact.execute, sid, row, text, option)
        state.bus.publish("session_action", session_id=sid, action=res["action"], mode=res["mode"],
                          exit_code=res["code"], ok=res["ok"], text_len=len(text),
                          answer_kind="auq" if res["action"] == "answer" else None, option=option)
        answered = text or (labels[option - 1] if option and option <= len(labels) else "")
        if res["ok"] and answered:
            try:
                sessionact.on_answer(row["ticket_project"], row["ticket_id"], sid, answered)
            except Exception:  # noqa: BLE001 - the E3 seam must not fail a delivered answer
                pass
        http = res.pop("http")
        return JSONResponse(res, status_code=http)

    async def api_project_searches(request: Request):
        path = request.path_params["path"]
        try:
            limit = max(1, min(int(request.query_params.get("limit", "20")), 100))
        except ValueError:
            limit = 20
        d = await asyncio.to_thread(state.project_searches, path, limit)
        return JSONResponse(d) if d is not None else err(f"unknown project {path!r}", 404)

    async def api_mode(request: Request):
        path = request.path_params["path"]
        b = await body_json(request)
        if _project(path) is None:
            return err(f"unknown project {path!r}", 404)

        def work():
            with state.db_lock:
                activity.set_mode(state.db(), path, str(b.get("mode", "")), b.get("window_hours"))
            state.refresh()
            return activity.describe(_project(path), config.load_config().window_hours)

        try:
            return JSONResponse(await asyncio.to_thread(work))
        except (ValueError, TypeError) as e:
            return err(str(e))

    async def api_sync(request: Request):
        path = request.path_params["path"]
        if await asyncio.to_thread(_project, path) is None:
            return err(f"unknown project {path!r}", 404)

        def work():
            activity.touch(path)
            return state.request_sync(path, "manual")

        return JSONResponse({"queued": await asyncio.to_thread(work), "project": path})

    async def api_activate(request: Request):
        path = request.path_params["path"]
        if await asyncio.to_thread(_project, path) is None:
            return err(f"unknown project {path!r}", 404)

        def work():
            activity.touch(path)
            state.bus.publish("project_activated", project=path)
            state.refresh()
            return activity.describe(_project(path), config.load_config().window_hours)

        return JSONResponse(await asyncio.to_thread(work))

    async def api_refresh(request: Request):
        await asyncio.to_thread(state.refresh)
        return JSONResponse({"ok": True})

    # ---- events
    def api_events_recent(request: Request):
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            limit = 100
        return JSONResponse({"events": state.bus.recent(max(1, min(limit, 500)))})

    async def api_events(request: Request):
        try:
            since = int(request.query_params.get("since") or request.headers.get("last-event-id") or 0)
        except ValueError:
            since = 0
        follow = request.query_params.get("follow", "1") != "0"
        loop = asyncio.get_running_loop()

        def fmt(ev: dict) -> str:
            return f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        async def stream():
            q, unsub = state.bus.subscribe(loop)
            nxt = None
            try:
                last = since
                for ev in state.bus.recent(0, since=since) if since else state.bus.recent(50):
                    last = max(last, ev["id"])
                    yield fmt(ev)
                if not follow:
                    return
                pinged = time.monotonic()
                nxt = None
                while not state.stopping.is_set():
                    if nxt is None:
                        nxt = asyncio.ensure_future(q.get())
                    await asyncio.wait({nxt}, timeout=SSE_POLL)
                    if not nxt.done():
                        if time.monotonic() - pinged >= 15:
                            pinged = time.monotonic()
                            yield ": ping\n\n"
                        continue
                    ev, nxt = nxt.result(), None
                    if ev["id"] > last:
                        last = ev["id"]
                        yield fmt(ev)
            finally:
                if nxt is not None:
                    nxt.cancel()
                unsub()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---- memories
    def api_memories(request: Request):
        qp = request.query_params
        scope, q = qp.get("scope") or None, (qp.get("q") or "").strip()
        archived = _bool(qp.get("include_archived"))
        try:
            limit = max(1, min(int(qp.get("limit", "50")), 500))
        except ValueError:
            limit = 50

        def in_scope(s: str) -> bool:
            return scope is None or s == scope or s.startswith(scope + "/")

        with state.db_lock:
            db = state.db()
            if q:  # hybrid search over memories, ignoring branch visibility (this is a management view)
                cfg = config.load_config()
                cfg.weights = {k: 1.0 for k in cfg.weights}   # a management view: relevance only, no scope weighting
                cfg.import_weight = 1.0
                try:
                    hits = S.search(db, cfg, get_embedder(cfg), q, None, k=limit * 4, scope="all", kind="memory",
                                    show_all_branches=True, respect_shared=False)
                except EmbedError as e:
                    return err(str(e), 502)
                items, seen = [], set()
                for h in hits:
                    r = memory.find_row_by_id(db, h.data["id"])
                    if r is not None and in_scope(r["scope"]) and (r["id"], r["source"]) not in seen:
                        seen.add((r["id"], r["source"]))
                        items.append({**state.memory_item(r), "score": round(h.score, 6)})
                if archived:
                    for r in db.execute("SELECT * FROM memories WHERE superseded_by IS NOT NULL AND text LIKE ? "
                                        "ORDER BY updated DESC LIMIT ?", (f"%{q}%", limit)):
                        if in_scope(r["scope"]):
                            items.append({**state.memory_item(r), "score": None})
                return JSONResponse({"memories": items[:limit]})
            rows = db.execute("SELECT * FROM memories " + ("" if archived else "WHERE superseded_by IS NULL ")
                              + "ORDER BY updated DESC, n DESC LIMIT ?", (limit * 4 if scope else limit,)).fetchall()
            items = [state.memory_item(r) for r in rows if in_scope(r["scope"])]
            return JSONResponse({"memories": items[:limit]})

    def api_memory_get(request: Request):
        with state.db_lock:
            row = memory.find(state.db(), request.path_params["mid"])
            item = state.memory_item(row) if row is not None else None
        return JSONResponse({"memory": item}) if item else err("memory not found", 404)

    def api_memory_action(request: Request):
        mid, action = request.path_params["mid"], request.path_params["action"]
        if action not in ("pin", "unpin", "archive", "promote"):
            return err("unknown action", 404)
        try:
            with state.db_lock:
                db = state.db()
                if action in ("pin", "unpin"):
                    m = memory.pin(db, mid, on=action == "pin")
                elif action == "archive":
                    m = memory.forget(db, mid)
                else:
                    m = memory.promote(db, mid)
                row = memory.find_row_by_id(db, m.id)
                item = state.memory_item(row) if row is not None else {"id": m.id}
        except ValueError as e:
            return err(str(e), 404 if "not found" in str(e) else 400)
        state.invalidate_status()
        state.bus.publish("memory_action", action=action, memory_id=m.id)
        return JSONResponse({"ok": True, "memory": item})

    # ---- search
    def _trigger(project_path: str, _root: str) -> None:
        state.request_sync(project_path, "stale-index")

    def _search(params: dict, *, side_effects: bool, session: str | None = None):
        """GET /api/search is read-only. Only the POST variant (CSRF-protected) may start a sync of a stale index
        or extend the project's activity window."""
        q = str(params.get("q") or "").strip()
        if not q:
            return err("q is required")
        scope, kind, mode = (str(params.get("scope") or "project"), str(params.get("kind") or "all"),
                             str(params.get("mode") or "auto"))
        if scope not in ("repo", "project", "all") or kind not in S.KINDS \
                or mode not in ("auto", "vector", "hybrid"):
            return err("scope must be repo|project|all, kind must be code|memory|docs|all, mode must be auto|vector|hybrid")
        cfg = config.load_config()
        try:
            k = max(1, min(int(params.get("k") or cfg.k), 100))
        except (TypeError, ValueError):
            return err("k must be an integer")
        cwd = str(params.get("cwd") or Path.home())
        t0 = time.perf_counter()
        qvec = S.embed_query(get_embedder(cfg), q)       # before the db lock: a slow Ollama must not block others
        try:
            with state.db_lock:
                db = state.db(check=True)
                c = ctxmod.current(db, cwd)
                hits = S.search(db, cfg, None, q, c, k=k, scope=scope, kind=kind, mode=mode, qvec=qvec)
                stale = kind != "memory" and bool(searchcmd.stale_repos(db, S._scope_repos(db, c, scope)))
                fresh = False
                if side_effects and kind != "memory":
                    fresh = searchcmd.check_freshness(db, S._scope_repos(db, c, scope), _trigger)
        except dbmod.ModelMismatch as e:
            return err(str(e), 409)
        if side_effects and c is not None:
            activity.touch(c.project_path)
        ms = (time.perf_counter() - t0) * 1000
        out = S.to_json(hits, breakdown=True)
        # exactly what the CLI prints for these hits (same budget, same line limits) and its token estimate
        lay = S.layout_hits(hits, cfg.output_budget_tokens, expand_top=cfg.expand_top, expand_lines=cfg.expand_lines)
        text = "\n".join(x["block"] for x in lay) or "no results"
        if fresh:
            text += "\n" + searchcmd.FRESH_NOTE
        tokens = len(text) // 4
        for i, d in enumerate(out):
            d["in_output"] = i < len(lay)
            d["shown"] = lay[i]["shown"] if i < len(lay) else None
            d["expanded"] = lay[i]["expanded"] if i < len(lay) else False
        route = S.effective_route(q, mode, qvec)
        searchlog.publish(state.bus, session=session, q=q, scope=scope, kind=kind, mode=mode, cwd=cwd,
                          ms=round(ms, 1), results=len(out), tokens=tokens, via="daemon", log_via="ui",
                          project=c.project_path if c else None, repo=c.repo_path if c else None,
                          branch=c.branch if c else None, route=route)
        return JSONResponse({"query": q, "scope": scope, "kind": kind, "mode": mode,
                             "route": route, "ms": round(ms, 1), "tokens": tokens, "index_refreshing": fresh,
                             "stale": stale, "expand_top": cfg.expand_top, "expand_lines": cfg.expand_lines,
                             "context": ({"project": c.project_path, "repo": c.repo_path, "branch": c.branch}
                                         if c else None),
                             "weights": cfg.weights, "import_weight": cfg.import_weight,
                             "weight_softness": cfg.weight_softness, "results": out})

    async def api_search(request: Request):
        return await asyncio.to_thread(_search, dict(request.query_params), side_effects=False)

    async def api_search_post(request: Request):
        return await asyncio.to_thread(_search, await body_json(request), side_effects=True)

    def _int_or_none(v, name: str, lo: int, hi: int) -> int | None:
        if v is None or v == "":
            return None
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise ValueError(f"{name} must be an integer")
        try:
            n = int(v)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
        if not lo <= n <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return n

    async def api_cli_search(request: Request):
        b = await body_json(request)
        q = str(b.get("q", ""))
        try:
            k = _int_or_none(b.get("k"), "k", 1, 100)
        except ValueError as e:
            return err(str(e))
        if b.get("project") not in (None, "") and not isinstance(b.get("project"), str):
            return err("project must be a string")
        t0 = time.perf_counter()

        def run():
            try:
                with state.db_lock:
                    db = state.db(check=True)
            except dbmod.ModelMismatch as e:
                return searchcmd.CliResult(stderr=f"error: {e}", exit_code=2)
            return searchcmd.run_search(
                q, str(b.get("cwd") or os.getcwd()), k=k, scope=str(b.get("scope", "project")),
                kind=str(b.get("kind", "all")), mode=str(b.get("mode", "auto")), full=bool(b.get("full")),
                json_out=bool(b.get("json")), via="daemon", db=db, trigger=_trigger, db_lock=state.db_lock,
                by_project=bool(b.get("by_project")), project=b.get("project") or None,
                compact=bool(b.get("compact")), total=bool(b.get("total")))

        res = await asyncio.to_thread(run)
        searchlog.publish(state.bus, session=request.headers.get("x-myvec-session"), q=q,
                          scope=b.get("scope", "project"), kind=b.get("kind", "all"), mode=b.get("mode", "auto"),
                          cwd=str(b.get("cwd") or ""), ms=round((time.perf_counter() - t0) * 1000, 1),
                          results=res.results, tokens=res.tokens, via="daemon", project=res.project,
                          repo=res.repo, branch=res.branch, route=res.route, fmt=res.fmt)
        return JSONResponse({**res.to_dict(), "cross": 1, "compact": 1})

    routes = [
        Route("/", index),
        Route("/api/status", api_status),
        Route("/api/load", api_load),
        Route("/api/load/series", api_load_series),
        Route("/api/load/projects", api_load_projects),
        Route("/api/tests/runs", api_tests_runs),
        Route("/api/tests/current", api_tests_current),
        Route("/api/tokens/project", api_tokens_project),
        Route("/api/search/usage", api_search_usage),
        Route("/api/tokens/ticket", api_tokens_ticket),
        Route("/api/projects/{path:path}/detail", api_project_detail),
        Route("/api/projects/{path:path}/searches", api_project_searches),
        Route("/api/projects/{path:path}/mode", api_mode, methods=["POST"]),
        Route("/api/projects/{path:path}/sync", api_sync, methods=["POST"]),
        Route("/api/projects/{path:path}/activate", api_activate, methods=["POST"]),
        Route("/api/tickets", api_tickets),
        Route("/api/tickets/{path:path}/tree", api_tickets_tree),
        Route("/api/tickets/{path:path}/problems", api_tickets_problems),
        Route("/api/tickets/{path:path}/ticket/{tid}/inbox", api_inbox_get, methods=["GET"]),
        Route("/api/tickets/{path:path}/ticket/{tid}/inbox", api_inbox_post, methods=["POST"]),
        Route("/api/tickets/{path:path}/ticket/{tid}", api_tickets_get),
        Route("/api/sessions", api_sessions),
        Route("/api/sessions/waiting", api_sessions_waiting),
        Route("/api/sessions/{sid}/resume", api_session_resume, methods=["POST"]),
        Route("/api/refresh", api_refresh, methods=["POST"]),
        Route("/api/events/recent", api_events_recent),
        Route("/api/events", api_events),
        Route("/api/memories", api_memories),
        Route("/api/memories/{mid}", api_memory_get),
        Route("/api/memories/{mid}/{action}", api_memory_action, methods=["POST"]),
        Route("/api/search", api_search),
        Route("/api/search", api_search_post, methods=["POST"]),
        Route("/api/cli/search", api_cli_search, methods=["POST"]),
        Mount("/static", _RevalidatedStatic(directory=str(UI_DIR)), name="static"),
    ]

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if state.background:
            _write_daemon_json(state)
            state.bus.publish("daemon_start", port=state.port, pid=os.getpid())
            state.start_load(config.load_config())
            await asyncio.to_thread(_cap_daemon_logs)
            await asyncio.to_thread(state.refresh)

            async def loop():
                n = 0
                while True:
                    await asyncio.sleep(1.0)
                    n += 1
                    try:
                        if n % 3600 == 0:
                            await asyncio.to_thread(_cap_daemon_logs)
                        if n % int(REFRESH_EVERY) == 0:
                            await asyncio.to_thread(state.refresh)
                        await asyncio.to_thread(state.tick)
                    except Exception as e:  # noqa: BLE001 - keep the scheduler alive
                        state.bus.publish("daemon_error", error=repr(e))

            task = asyncio.create_task(loop())
        try:
            yield
        finally:
            state.stopping.set()  # cancels a running sync even when the stop bypassed make_server.handle_exit
            if task is not None:
                task.cancel()
            state.watchers.stop_all()
            if state.load is not None:
                state.load.stop()
            state.executor.shutdown(wait=False, cancel_futures=True)
            state.ticket_executor.shutdown(wait=False, cancel_futures=True)
            state.tokens_executor.shutdown(wait=False, cancel_futures=True)
            # uvicorn re-raises SIGTERM after shutdown with the default handler (the process dies at once, threads
            # are not joined), so the sync thread must write its "cancelled" line before we return. A thread
            # keeps the loop free for the rest of the shutdown.
            await asyncio.to_thread(state.wait_sync_stopped)
            if state.background:
                _remove_daemon_json(state)
                state.bus.publish("daemon_stop")
            state.close()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[
        Middleware(GuardMiddleware, port_getter=lambda: state.port, token_getter=lambda: state.token)])
    app.state.daemon = state
    return app


# ------------------------------------------------------------------ daemon.json / serve

def _cap_daemon_logs() -> None:
    for name in ("daemon.err.log", "daemon.out.log"):
        logutil.cap_inplace(config.logs_dir() / name, ERR_LOG_MAX)


def _write_daemon_json(state: DaemonState) -> None:
    config.ensure_home()
    p = config.daemon_json_path()
    data = json.dumps({"port": state.port, "pid": os.getpid(), "token": state.token, "started": state.started})
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(p, 0o600)


def _remove_daemon_json(state: DaemonState) -> None:
    p = config.daemon_json_path()
    try:
        if json.loads(p.read_text()).get("pid") == os.getpid():
            p.unlink()
    except (OSError, ValueError):
        pass


def make_server(app, state: DaemonState, port: int, log_level: str = "warning"):
    """uvicorn.Server whose SIGTERM/SIGINT first wakes SSE streams; open ones would block shutdown
    (uvicorn waits for connections before lifespan shutdown), the timeout is the safety net."""
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            state.stopping.set()
            super().handle_exit(sig, frame)

    return _Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level, access_log=False,
                                  timeout_graceful_shutdown=GRACEFUL_STOP_S))


def serve(port: int | None = None) -> int:
    import sys

    cfg = config.load_config()
    port = port or cfg.daemon_port
    info = daemon_client.read_daemon()
    if info is not None:
        print(f"myvec daemon already running (pid {info['pid']}, port {info['port']})", file=sys.stderr)
        return 1
    config.ensure_home()
    state = DaemonState(port, background=True)
    app = create_app(state)
    print(f"myvec daemon on http://127.0.0.1:{port}  (home: {config.home()})", flush=True)
    server = make_server(app, state, port)
    server.run()
    return 0
