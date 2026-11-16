"""Pure scheduling logic of the daemon (no I/O, injectable clock -> unit-testable).

For every *active* project: a file event marks it dirty; it is synced `debounce` seconds after its last
relevant event (default 10 s). A safety-net sync happens when `safety` seconds (default 15 min) passed since
the last one. A project never has two syncs queued/running at once.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _State:
    last_sync: float
    last_event: float = 0.0
    dirty: bool = False
    request_now: bool = False
    running: bool = False


class Scheduler:
    def __init__(self, debounce: float = 10.0, safety: float = 900.0, clock: Callable[[], float] = time.monotonic):
        self.debounce = debounce
        self.safety = safety
        self.clock = clock
        self._lock = threading.Lock()
        self._p: dict[str, _State] = {}

    @property
    def active(self) -> set[str]:
        with self._lock:
            return set(self._p)

    def set_active(self, keys: set[str]) -> tuple[list[str], list[str]]:
        """Replace the active set. Returns (started, stopped); newly started projects get an immediate sync."""
        now = self.clock()
        with self._lock:
            started = sorted(k for k in keys if k not in self._p)
            stopped = sorted(k for k in self._p if k not in keys)
            for k in started:
                self._p[k] = _State(last_sync=now, request_now=True)
            for k in stopped:
                del self._p[k]
        return started, stopped

    def notify(self, key: str) -> None:
        """A relevant file event for `key`."""
        with self._lock:
            p = self._p.get(key)
            if p is not None:
                p.dirty = True
                p.last_event = self.clock()

    def request(self, key: str) -> None:
        with self._lock:
            p = self._p.get(key)
            if p is not None:
                p.request_now = True

    def due(self) -> list[str]:
        now = self.clock()
        out = []
        with self._lock:
            for k, p in self._p.items():
                if p.running:
                    continue
                if p.request_now or (p.dirty and now - p.last_event >= self.debounce) \
                        or now - p.last_sync >= self.safety:
                    out.append(k)
        return sorted(out)

    def safety_in(self, key: str) -> float | None:
        """Seconds until the next safety-net sync of an active project (None if it is not active)."""
        with self._lock:
            p = self._p.get(key)
            return None if p is None else max(0.0, self.safety - (self.clock() - p.last_sync))

    def started(self, key: str) -> None:
        with self._lock:
            p = self._p.get(key)
            if p is not None:
                p.running, p.dirty, p.request_now = True, False, False

    def finished(self, key: str, retry: bool = False) -> None:
        """Sync done. `retry`: it could not run (lock busy) -> try again after the debounce."""
        now = self.clock()
        with self._lock:
            p = self._p.get(key)
            if p is None:
                return
            p.running = False
            if retry:
                p.dirty, p.last_event = True, now
            else:
                p.last_sync = now
