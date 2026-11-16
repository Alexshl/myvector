"""Per-project activity window and sync mode. Stdlib only (hooks import this and must stay < 100 ms).

A project is *active* if `sync_mode='always'`, or `sync_mode='auto'` and `now < last_access + window`.
Any access (session-start / post-edit hooks, search, remember, UI actions) calls `touch()`.

`touch()` is one sqlite UPDATE with a short busy timeout. If the database is locked (a sync is writing) or
not migrated yet, the access is appended to `MYVEC_HOME/activity` (one `epoch<TAB>project_path` line) and
`fold_activity()` (run by the daemon every tick and by `status`) folds it into `projects.last_access`.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: no locking, as before
    fcntl = None  # type: ignore[assignment]

MODES = ("auto", "always", "off")
HOOK_BUSY_MS = 100


def _activity_file() -> Path:
    from myvec import config

    return config.activity_path()


@contextlib.contextmanager
def _queue_lock(src: Path, *, exclusive: bool):
    """flock on `activity.lock` next to the queue: writers hold it shared while they open+write, the folder holds
    it exclusively around the rename, so no writer still owns a descriptor of the renamed inode. Never raises;
    without fcntl or a writable directory it degrades to no locking."""
    fd = None
    try:
        if fcntl is not None:
            fd = os.open(str(src.with_name(src.name + ".lock")), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)  # closing releases the flock


def _append_queue(p: Path, text: str) -> None:
    with _queue_lock(p, exclusive=False):
        with open(p, "a", encoding="utf-8") as f:
            f.write(text)


def touch(project_path: str, *, now: float | None = None, db: sqlite3.Connection | None = None) -> str | None:
    """Record an access. Returns the project's sync_mode ('auto'|'always'|'off') or None if unknown."""
    now = time.time() if now is None else now
    try:
        own = db is None
        if own:
            from myvec import config

            p = config.db_path()
            if not p.exists():
                raise FileNotFoundError(p)
            db = sqlite3.connect(str(p), timeout=HOOK_BUSY_MS / 1000, isolation_level=None)
            db.execute(f"PRAGMA busy_timeout={HOOK_BUSY_MS}")
        try:
            db.execute("UPDATE projects SET last_access=? WHERE path=? AND coalesce(last_access, 0) < ?",
                       (now, project_path, now))
            r = db.execute("SELECT sync_mode FROM projects WHERE path=?", (project_path,)).fetchone()
            return (r[0] or "auto") if r else None
        finally:
            if own:
                db.close()
    except Exception:  # noqa: BLE001 - locked / old schema / no db yet: leave a note for the daemon
        try:
            p = _activity_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            _append_queue(p, f"{now:.3f}\t{project_path}\n")
        except OSError:
            pass
        return None


FOLD_STALE_S = 300.0   # a `.folding.*` file this old belongs to a folder that died: pick it up too


def _claim_pending(src: Path) -> list[Path]:
    """Atomically move the queue to a name nobody else uses (concurrent folders each get their own file), plus
    leftovers of folders that crashed before they could delete theirs."""
    claimed: list[Path] = []
    tmp = src.with_name(f"{src.name}.folding.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        with _queue_lock(src, exclusive=True):
            os.replace(src, tmp)
        claimed.append(tmp)
    except OSError:  # nothing queued, or another folder just took it
        pass
    now = time.time()
    try:
        for old in src.parent.glob(src.name + ".folding.*"):
            if old in claimed:
                continue
            try:
                if now - old.stat().st_mtime < FOLD_STALE_S:
                    continue
                mine = old.with_name(old.name + f".{os.getpid()}")
                os.replace(old, mine)  # whoever wins the rename owns it
                claimed.append(mine)
            except OSError:
                continue
    except OSError:
        pass
    return claimed


def fold_activity(db: sqlite3.Connection) -> int:
    """Apply queued accesses from the activity file to projects.last_access. Returns lines applied.
    Safe to call concurrently from several processes/threads (each claims the queue under a unique name)."""
    src = _activity_file()
    claimed = _claim_pending(src)
    latest: dict[str, float] = {}
    for tmp in claimed:
        try:
            for line in tmp.read_text(encoding="utf-8", errors="replace").splitlines():
                ts, _, path = line.partition("\t")
                try:
                    latest[path] = max(latest.get(path, 0.0), float(ts))
                except ValueError:
                    continue
        except OSError:
            continue
    try:
        for path, ts in latest.items():
            db.execute("UPDATE projects SET last_access=? WHERE path=? AND coalesce(last_access, 0) < ?",
                       (ts, path, ts))
    except sqlite3.Error:
        try:  # database busy: put the accesses back for the next fold
            _append_queue(src, "".join(f"{ts:.3f}\t{path}\n" for path, ts in latest.items()))
        except OSError:
            pass
        raise
    finally:
        for tmp in claimed:
            try:
                tmp.unlink()
            except OSError:
                pass
    return len(latest)


def window_hours(row, default: float) -> float:
    w = row["window_hours"]
    return float(w) if w is not None else float(default)


def mode_of(row) -> str:
    m = row["sync_mode"]
    return m if m in MODES else "auto"


def active_until(row, default_window: float) -> float | None:
    """End of the activity window (auto mode); None for always/off."""
    if mode_of(row) != "auto":
        return None
    return float(row["last_access"] or 0) + window_hours(row, default_window) * 3600


def is_active(row, default_window: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    mode = mode_of(row)
    if mode == "always":
        return True
    if mode == "off":
        return False
    return now < (active_until(row, default_window) or 0)


def describe(row, default_window: float, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "mode": mode_of(row),
        "window_hours": window_hours(row, default_window),
        "last_access": float(row["last_access"] or 0) or None,
        "active_until": active_until(row, default_window),
        "active": is_active(row, default_window, now),
    }


def set_mode(db: sqlite3.Connection, project_path: str, mode: str, window: float | None = None) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if window is not None and window <= 0:
        raise ValueError("window_hours must be > 0")
    r = db.execute("SELECT 1 FROM projects WHERE path=?", (project_path,)).fetchone()
    if r is None:
        raise ValueError(f"unknown project {project_path!r}")
    db.execute("UPDATE projects SET sync_mode=? WHERE path=?", (mode, project_path))
    if window is not None:
        db.execute("UPDATE projects SET window_hours=? WHERE path=?", (window, project_path))


def resolve_project(db: sqlite3.Connection, arg: str | None, cwd: str | Path) -> str:
    """Project path from an explicit `--project` (project path or filesystem path) or from cwd's marker."""
    from myvec import projects

    if arg:
        r = db.execute("SELECT path FROM projects WHERE path=?", (arg,)).fetchone()
        if r:
            return r[0]
        p = Path(arg).expanduser()
        if p.exists():
            m = projects.find_marker(p)
            if m is not None:
                return m.path
        raise ValueError(f"unknown project {arg!r}")
    m = projects.find_marker(cwd)
    if m is None:
        raise ValueError("not inside a myvec project (use --project)")
    return m.path
