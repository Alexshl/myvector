"""sync.lock helpers (stdlib only; used by hooks too).

The lock is an exclusive `flock`. The holder also writes its pid into the lock file, so `is_locked()` can answer
"is a sync running?" from the pid alone (kill 0) and, in the common idle case, without ever touching the lock -
a probe that briefly took the flock made concurrent real syncs lose the race ("sync already running" for no reason).
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager

from myvec import config

POLL_S = 0.05


def _try_lock(f) -> bool:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@contextmanager
def sync_lock(wait: float = 0.0):
    """Exclusive flock. Yields True if acquired, False if someone else holds it (after waiting up to `wait` seconds)."""
    config.ensure_home()
    f = open(config.lock_path(), "a+")
    got = False
    try:
        deadline = time.monotonic() + wait
        while True:
            got = _try_lock(f)
            if got or time.monotonic() >= deadline:
                break
            time.sleep(POLL_S)
        if got:
            try:
                f.seek(0)
                f.truncate()
                f.write(str(os.getpid()))
                f.flush()
            except OSError:
                pass
        yield got
    finally:
        if got:
            try:
                f.truncate(0)
            except OSError:
                pass
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _holder_pid() -> int | None:
    try:
        txt = config.lock_path().read_text(encoding="utf-8").strip()
        return int(txt) if txt else None
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: exists, owned by someone else
        return True


def is_locked() -> bool:
    """True if a sync holds the lock. Does not take the lock unless a live process claims to hold it (then one
    non-blocking probe confirms it, which also covers a recycled pid)."""
    pid = _holder_pid()
    if pid is None or not _alive(pid):
        return False
    f = open(config.lock_path(), "a+")
    try:
        if _try_lock(f):
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        return True
    finally:
        f.close()
