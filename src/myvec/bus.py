"""In-memory event bus: ring buffer (last ~500 events) + persisted logs/events.jsonl (rotated)."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

RING = 500
ROTATE_BYTES = 3 * 1024 * 1024


class EventBus:
    def __init__(self, path: Path | None = None, ring: int = RING, rotate_bytes: int = ROTATE_BYTES):
        self.path = path
        self.rotate_bytes = rotate_bytes
        self._ring: deque[dict] = deque(maxlen=ring)
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._next = 1
        self._load_tail()

    def _load_tail(self) -> None:
        """Warm the ring from the persisted log so history survives a daemon restart."""
        if not self.path or not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-self._ring.maxlen:]
        except OSError:
            return
        for ln in lines:
            try:
                ev = json.loads(ln)
                self._ring.append(ev)
                self._next = max(self._next, int(ev["id"]) + 1)
            except (ValueError, KeyError, TypeError):
                continue

    def publish(self, type_: str, **data) -> dict:
        with self._lock:
            ev = {**data, "id": self._next, "ts": round(time.time(), 3), "type": type_}  # reserved keys win
            self._next += 1
            self._ring.append(ev)
            self._persist(ev)
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:  # loop closed
                pass
        return ev

    def _persist(self, ev: dict) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.rotate_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def recent(self, limit: int = 100, since: int = 0) -> list[dict]:
        with self._lock:
            evs = [e for e in self._ring if e["id"] > since]
        return evs[-limit:] if limit else evs

    def subscribe(self, loop: asyncio.AbstractEventLoop):
        q: asyncio.Queue = asyncio.Queue()
        entry = (loop, q)
        with self._lock:
            self._subs.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                if entry in self._subs:
                    self._subs.remove(entry)

        return q, unsubscribe
