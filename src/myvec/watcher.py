"""File watching for active projects (watchdog -> FSEvents on macOS).

Ignores denied dirs / files and anything .gitignore'd, but DOES watch `.git/HEAD` and `.git/index`
(branch switch / commit / pull).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Callable

import pathspec

from myvec import filters, projects

GIT_FILES = {"HEAD", "index"}
OVERFLOW_DEBOUNCE = 30.0  # s: at most one "events lost" resync request per project

_log = logging.getLogger("myvec.watcher")


def backend_name() -> str:
    """What watchdog uses on this OS (shown in the UI)."""
    import sys

    return {"darwin": "FSEvents", "linux": "inotify"}.get(sys.platform, "watchdog")


# Only content-changing events. Without a filter watchdog's inotify mask includes IN_OPEN / IN_CLOSE_NOWRITE:
# every read under the watched trees (git status, rg, builds, our own sync) floods the kernel queue
# (16384 events), watchdog keeps re-walking the trees after "Inotify queue overflowed" and the daemon burns CPU.
def _watch_events() -> tuple:
    from watchdog.events import (DirCreatedEvent, DirDeletedEvent, DirMovedEvent, FileCreatedEvent,
                                 FileDeletedEvent, FileModifiedEvent, FileMovedEvent)
    return (FileCreatedEvent, FileModifiedEvent, FileDeletedEvent, FileMovedEvent,
            DirCreatedEvent, DirDeletedEvent, DirMovedEvent)


class EventFilter:
    """Decides whether a filesystem path under a project root is worth a sync."""

    def __init__(self, root: Path, repo_dirs: list[Path] | None = None, exclude=None):
        self.root = Path(root)
        self.exclude = exclude  # PathSpec of the marker's `exclude`
        self.repo_dirs = sorted({Path(d) for d in (repo_dirs or [])} | {self.root},
                                key=lambda p: len(p.parts), reverse=True)
        self._specs: dict[Path, pathspec.PathSpec | None] = {}
        self.reload_ignores()

    @classmethod
    def for_project(cls, root: Path) -> "EventFilter":
        m = projects.read_marker(root)
        exc = projects.exclude_spec(m) if m is not None else None
        dirs: list[Path] = []
        if m is not None:
            try:
                specs, _children = projects.discover(m)
                dirs = [s.dir for s in specs]
            except Exception:  # noqa: BLE001
                dirs = []
        return cls(root, dirs, exc)

    def reload_ignores(self) -> None:
        for d in self.repo_dirs:
            gi = d / ".gitignore"
            try:
                self._specs[d] = pathspec.PathSpec.from_lines("gitignore", gi.read_text(
                    encoding="utf-8", errors="replace").splitlines()) if gi.is_file() else None
            except OSError:
                self._specs[d] = None

    def relevant(self, path: str | Path) -> bool:
        p = Path(path)
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return False
        parts = rel.parts
        if not parts:
            return False
        if ".git" in parts:
            i = parts.index(".git")
            return parts[i + 1:] in [(n,) for n in GIT_FILES]
        if p.name == ".gitignore":
            self.reload_ignores()
            return True
        if any(filters.is_denied_dir(d) for d in parts[:-1]) or parts[-1] in filters.DENY_DIRS:
            return False
        if filters.is_denied_path(rel.as_posix()):
            return False
        if self.exclude is not None and self.exclude.match_file(rel.as_posix()):
            return False
        for d in self.repo_dirs:
            if d == p or d in p.parents:
                spec = self._specs.get(d)
                if spec is not None and spec.match_file(p.relative_to(d).as_posix()):
                    return False
                break  # deepest containing repo decides
        return True


    def watch_dir(self, path: str | Path) -> bool:
        """Whether a (recursive) inotify watch makes sense on this directory. Only `<repo>/.git` itself is
        watched (HEAD/index), never what is under it; denied, excluded and gitignored dirs are not."""
        p = Path(path)
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return False
        parts = rel.parts
        if not parts:
            return True
        if ".git" in parts:
            i = parts.index(".git")
            return i == len(parts) - 1 and not any(filters.is_denied_dir(d) for d in parts[:i])
        if any(filters.is_denied_dir(d) for d in parts):
            return False
        if self.exclude is not None and self.exclude.match_file(rel.as_posix() + "/"):
            return False
        for d in self.repo_dirs:
            if d == p or d in p.parents:
                spec = self._specs.get(d)
                if spec is not None and p != d and spec.match_file(p.relative_to(d).as_posix() + "/"):
                    return False
                break
        return True


_PRIVATE_API = (("InotifyWatchGroup", ("_add_all_callbacks", "_add_callback", "on_queue_overflow")),
                ("InotifyEmitter", ("on_thread_start",)))


def _pruned_observer_class(resolve: Callable[[str], "tuple[EventFilter, Callable[[], None]] | None"]):
    """Linux: an inotify observer whose recursive walk skips what `EventFilter.watch_dir` rejects (watchdog
    watches every directory, `.git/objects`, node_modules... and then logs megabyte-long repr lines, ADR-033).
    Relies on private watchdog 7.1 API; returns None when it is not there (the caller falls back to Observer)."""
    try:
        from watchdog.observers import inotify as ino
        for cls_name, attrs in _PRIVATE_API:
            cls = getattr(ino, cls_name)
            if not all(callable(getattr(cls, a, None)) for a in attrs):
                return None
        if not hasattr(ino, "InotifyFD") or not hasattr(ino.InotifyObserver, "on_thread_stop"):
            return None
        from watchdog.observers.api import BaseObserver
    except Exception:  # noqa: BLE001
        return None

    class _PrunedGroup(ino.InotifyWatchGroup):
        def _ctx(self):
            return resolve(os.fsdecode(self.path))

        def _add_all_callbacks(self, path: bytes) -> None:
            ctx = self._ctx()
            if ctx is None:
                return super()._add_all_callbacks(path)
            flt = ctx[0]
            if path != self.path and not flt.watch_dir(os.fsdecode(path)):
                return
            self._add_callback(path)
            if not (os.path.isdir(path) and self.is_recursive):
                return
            for root, dirnames, _ in os.walk(path, followlinks=self.follow_symlink):
                keep = []
                for d in dirnames:
                    full = os.path.join(root, d)
                    if not self.follow_symlink and os.path.islink(full):
                        continue
                    if not flt.watch_dir(os.fsdecode(full)):
                        continue
                    self._add_callback(full)
                    if d != b".git":
                        keep.append(d)
                dirnames[:] = keep

        def on_queue_overflow(self) -> None:
            super().on_queue_overflow()
            ctx = self._ctx()
            if ctx is not None:
                ctx[1]()

    class _PrunedEmitter(ino.InotifyEmitter):
        def on_thread_start(self) -> None:
            self._inotify = _PrunedGroup(
                self._inotify_fd, os.fsencode(self.watch.path), is_recursive=self.watch.is_recursive,
                event_mask=self.get_event_mask_from_filter(), follow_symlink=self.watch.follow_symlink)

    class _PrunedObserver(ino.InotifyObserver):
        def __init__(self) -> None:
            self._inotify_fd = ino.InotifyFD()
            self._inotify_fd.start()
            BaseObserver.__init__(self, _PrunedEmitter, inotify_fd=self._inotify_fd)

    return _PrunedObserver


class WatchManager:
    """One watchdog watch per active project. `on_event(project_key, path)` is called from watchdog threads."""

    def __init__(self, on_event: Callable[[str, str], None], on_overflow: Callable[[str, str], None] | None = None):
        self.on_event = on_event
        self.on_overflow_cb = on_overflow
        self._observer = None
        self._watches: dict[str, object] = {}
        self._lock = threading.Lock()
        self._ctx: dict[str, tuple[str, EventFilter]] = {}   # watched root -> (key, filter)
        self._last_overflow: dict[str, float] = {}
        self.overflows: dict[str, int] = {}
        self._clock = time.monotonic

    def _resolve(self, root: str):
        c = self._ctx.get(root)
        if c is None:
            return None
        key, flt = c
        return flt, lambda: self.on_overflow(key)

    def on_overflow(self, key: str) -> None:
        """Inotify dropped events: ask for one resync, at most every OVERFLOW_DEBOUNCE s per project."""
        now = self._clock()
        with self._lock:
            self.overflows[key] = self.overflows.get(key, 0) + 1
            last = self._last_overflow.get(key)
            if last is not None and now - last < OVERFLOW_DEBOUNCE:
                return
            self._last_overflow[key] = now
            root = next((r for r, (k, _f) in self._ctx.items() if k == key), "")
        _log.error("inotify queue overflowed, resync requested (%s)", key)
        (self.on_overflow_cb or self.on_event)(key, root)

    def watched_dirs(self, key: str) -> set[str]:
        """Directories with an inotify watch for the project (Linux; empty elsewhere). For tests/diagnostics."""
        out: set[str] = set()
        obs = self._observer
        w = self._watches.get(key)
        if obs is None or w is None:
            return out
        for em in list(getattr(obs, "emitters", ())):
            grp = getattr(em, "_inotify", None)
            if getattr(em, "watch", None) == w and grp is not None:
                out |= {os.fsdecode(p) for p in grp._active_callbacks_by_path}
        return out

    def _obs(self):
        if self._observer is None:
            from watchdog.observers import Observer

            warnings.filterwarnings("ignore", category=RuntimeWarning,
                                    message=r"Callback with id .* (already exists|does not exist) for watch")
            for name in ("watchdog.observers.inotify", "watchdog.observers.inotify_c",
                         "watchdog.observers.inotify_buffer"):
                logging.getLogger(name).setLevel(logging.ERROR)
            cls = _pruned_observer_class(self._resolve) if sys.platform == "linux" else None
            if sys.platform == "linux" and cls is None:
                _log.error("watchdog private API not found: stock Observer (every directory is watched)")
            self._observer = cls() if cls is not None else Observer()
            self._observer.daemon = True
            self._observer.start()
        return self._observer

    def start(self, key: str, root: Path) -> None:
        from watchdog.events import FileSystemEventHandler

        flt = EventFilter.for_project(root)
        on_event = self.on_event

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed_no_write", "closed"):
                    return
                if event.is_directory and event.event_type == "modified":
                    return
                for pth in (event.src_path, getattr(event, "dest_path", "")):
                    if pth and flt.relevant(pth):
                        on_event(key, str(pth))
                        return

        with self._lock:
            if key in self._watches:
                return
            self._ctx[str(root)] = (key, flt)
            self._watches[key] = self._obs().schedule(Handler(), str(root), recursive=True,
                                                      event_filter=list(_watch_events()))

    def stop(self, key: str) -> None:
        with self._lock:
            w = self._watches.pop(key, None)
            for r in [r for r, (k, _f) in self._ctx.items() if k == key]:
                del self._ctx[r]
            if w is not None and self._observer is not None:
                try:
                    self._observer.unschedule(w)
                except Exception:  # noqa: BLE001
                    pass

    def stop_all(self) -> None:
        for k in list(self._watches):
            self.stop(k)
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None

    @property
    def watching(self) -> set[str]:
        with self._lock:
            return set(self._watches)
