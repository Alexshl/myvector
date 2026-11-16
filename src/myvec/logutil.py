"""Log files that cannot grow without bound (stdlib only: hooks import this and must stay fast).

`rotate(path)` keeps the last ~MAX_BYTES in `path` and the previous generation in `path.1`. It is one `stat()`
call, so it is cheap enough to run before every append, also from hooks.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024


def rotate(path: str | Path, max_bytes: int = MAX_BYTES) -> None:
    """If `path` is bigger than `max_bytes`, move it to `path.1` (replacing the older backup). Never raises."""
    p = Path(path)
    try:
        if p.stat().st_size <= max_bytes:
            return
        os.replace(p, p.with_name(p.name + ".1"))
    except OSError:  # missing, or another process rotated it first
        pass


def append(path: str | Path, line: str, *, stamp: bool = True, max_bytes: int = MAX_BYTES) -> None:
    """Append one line (with an ISO timestamp unless `stamp=False`) after rotating if needed. Never raises."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        rotate(p, max_bytes)
        with open(p, "a", encoding="utf-8") as f:
            f.write((f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n") if stamp else line + "\n")
    except OSError:
        pass


def open_append(path: str | Path, max_bytes: int = MAX_BYTES):
    """Open `path` for appending (binary-safe text handle for subprocess redirects), rotating first."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rotate(p, max_bytes)
    return open(p, "a")


def cap_inplace(path: str | Path, max_bytes: int, keep_tail: int = 1024 * 1024) -> None:
    """If `path` is bigger than `max_bytes`: keep its last `keep_tail` bytes in `path.1` and truncate `path` to 0.
    In place, not rename: systemd `append:` / launchd hold an O_APPEND fd that keeps writing to the same inode.
    Never raises."""
    p = Path(path)
    try:
        size = p.stat().st_size
        if size <= max_bytes:
            return
        n = min(keep_tail, size)
        with open(p, "rb") as f:
            f.seek(size - n)
            tail = f.read(n)
        bak = p.with_name(p.name + ".1")
        with open(bak, "wb") as f:
            f.write(tail)
        os.truncate(p, 0)
    except OSError:
        pass
