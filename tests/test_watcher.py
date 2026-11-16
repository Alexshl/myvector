"""E6-12 (ADR-033): pruned inotify watches, overflow debounce, warning silencing, daemon log cap."""
import os
import sys
import time
import warnings
from pathlib import Path

import pytest
from conftest import git, write

from myvec import daemon, logutil, watcher
from myvec.watcher import EventFilter, WatchManager

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="inotify only")


def _wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for d in (".git/objects/ab", "node_modules/a/b", ".wrangler/state/x", ".worktrees/w/src", ".venv/lib",
              "out", "skipme/inner", "src/pkg", "docs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    write(root / ".gitignore", "out/\n")
    write(root / ".myvector.toml", 'name = "proj"\ninclude = []\nexclude = ["skipme"]\n')
    write(root / "src/pkg/x.py", "x = 1\n")
    return root


def _rels(wm, key, root):
    return {Path(p).relative_to(root).as_posix() for p in wm.watched_dirs(key)}


ALLOWED = {".", ".git", "src", "src/pkg", "docs"}
FORBIDDEN_PREFIXES = (".git/", "node_modules", ".wrangler", ".worktrees", ".venv", "out", "skipme")


def _assert_pruned(rels):
    assert rels == ALLOWED, rels
    assert not any(r.startswith(FORBIDDEN_PREFIXES) for r in rels if r != ".git")


# ------------------------------------------------------------------ EventFilter.watch_dir (pure)

def test_watch_dir_rules(proj):
    f = EventFilter.for_project(proj)
    assert f.watch_dir(proj)
    assert f.watch_dir(proj / ".git")
    assert not f.watch_dir(proj / ".git/objects")
    assert not f.watch_dir(proj / ".git/objects/ab")
    for d in ("node_modules", "src/node_modules", ".wrangler/state", ".worktrees/w", ".venv/lib", "out", "skipme",
              "skipme/inner"):
        assert not f.watch_dir(proj / d), d
    assert f.watch_dir(proj / "src") and f.watch_dir(proj / "src/pkg") and f.watch_dir(proj / "src/new/deep")
    assert not f.watch_dir(proj.parent / "elsewhere")


def test_watch_dir_nested_repo_git_dir_only_top(proj):
    sub = proj / "sub"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    f = EventFilter(proj, [sub])
    assert f.watch_dir(sub / ".git")
    assert not f.watch_dir(sub / ".git/refs")


# ------------------------------------------------------------------ AC 1-2: real inotify

@linux_only
def test_watched_dirs_exclude_denied_ignored_and_excluded(proj):
    wm = WatchManager(lambda k, p: None)
    wm.start("p", proj)
    try:
        assert _wait(lambda: _rels(wm, "p", proj) == ALLOWED)
        _assert_pruned(_rels(wm, "p", proj))
    finally:
        wm.stop_all()


@linux_only
def test_events_for_git_head_index_and_allowed_files_reach_callback(proj):
    got = []
    wm = WatchManager(lambda k, p: got.append(p))
    wm.start("p", proj)
    try:
        assert _wait(lambda: _rels(wm, "p", proj) == ALLOWED)
        write(proj / ".git/HEAD", "ref: refs/heads/main\n")
        assert _wait(lambda: any(p.endswith(".git/HEAD") for p in got))
        write(proj / ".git/index", "i")
        assert _wait(lambda: any(p.endswith(".git/index") for p in got))
        write(proj / "src/pkg/x.py", "x = 2\n")
        assert _wait(lambda: any(p.endswith("src/pkg/x.py") for p in got))
    finally:
        wm.stop_all()


@linux_only
def test_new_deep_dir_is_watched_and_its_file_event_arrives(proj):
    got = []
    wm = WatchManager(lambda k, p: got.append(p))
    wm.start("p", proj)
    try:
        assert _wait(lambda: _rels(wm, "p", proj) == ALLOWED)
        (proj / "src/new/deep").mkdir(parents=True)
        assert _wait(lambda: "src/new/deep" in _rels(wm, "p", proj))
        time.sleep(0.3)
        write(proj / "src/new/deep/f.py", "y = 1\n")
        assert _wait(lambda: any(p.endswith("src/new/deep/f.py") for p in got))
    finally:
        wm.stop_all()


@linux_only
def test_new_denied_dirs_are_not_watched(proj):
    wm = WatchManager(lambda k, p: None)
    wm.start("p", proj)
    try:
        assert _wait(lambda: _rels(wm, "p", proj) == ALLOWED)
        (proj / "node_modules/z").mkdir()
        (proj / "src/node_modules/q").mkdir(parents=True)
        (proj / "src/marker").mkdir()
        assert _wait(lambda: "src/marker" in _rels(wm, "p", proj))
        rels = _rels(wm, "p", proj)
        assert not any("node_modules" in r for r in rels), rels
    finally:
        wm.stop_all()


# ------------------------------------------------------------------ AC 3: overflow

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@linux_only
def test_queue_overflow_keeps_pruning_and_debounces_resync(proj):
    got, ov = [], []
    wm = WatchManager(lambda k, p: got.append((k, p)), on_overflow=lambda k, r: ov.append((k, r)))
    clock = FakeClock()
    wm._clock = clock
    wm.start("p", proj)
    try:
        assert _wait(lambda: _rels(wm, "p", proj) == ALLOWED)
        grp = next(em._inotify for em in wm._obs().emitters if getattr(em, "_inotify", None) is not None)
        grp.on_queue_overflow()
        _assert_pruned(_rels(wm, "p", proj))
        clock.t += 10
        grp.on_queue_overflow()
        _assert_pruned(_rels(wm, "p", proj))
        assert len(ov) == 1 and ov[0][0] == "p" and ov[0][1] == str(proj)
        assert wm.overflows["p"] == 2
        clock.t += 30
        grp.on_queue_overflow()
        assert len(ov) == 2
    finally:
        wm.stop_all()


def test_overflow_without_dedicated_callback_falls_back_to_on_event():
    got = []
    wm = WatchManager(lambda k, p: got.append((k, p)))
    wm._clock = FakeClock()
    wm.on_overflow("k")
    wm.on_overflow("k")
    assert [k for k, _ in got] == ["k"]


def test_overflow_debounce_is_per_project():
    ov = []
    wm = WatchManager(lambda k, p: None, on_overflow=lambda k, r: ov.append(k))
    wm._clock = FakeClock()
    wm.on_overflow("a")
    wm.on_overflow("b")
    wm.on_overflow("a")
    assert ov == ["a", "b"]


# ------------------------------------------------------------------ AC 4: warning silencing

@linux_only
def test_duplicate_callback_warning_is_silenced(proj, capfd):
    wm = WatchManager(lambda k, p: None)
    wm.start("p", proj)
    try:
        assert _wait(lambda: _rels(wm, "p", proj) == ALLOWED)
        grp = next(em._inotify for em in wm._obs().emitters if getattr(em, "_inotify", None) is not None)
        with warnings.catch_warnings(record=True) as rec:
            grp._add_callback(os.fsencode(proj / "src"))
            grp._add_callback(os.fsencode(proj / "src"))
        assert not [w for w in rec if "Callback with id" in str(w.message)]
        assert "Callback with id" not in capfd.readouterr().err
    finally:
        wm.stop_all()


# ------------------------------------------------------------------ AC 5: sentinel + guard

@linux_only
def test_sentinel_watchdog_private_api_present():
    """Fails loudly when a watchdog upgrade removes what the pruned observer relies on (ADR-033)."""
    from watchdog.observers import inotify as ino
    for cls_name, attrs in watcher._PRIVATE_API:
        cls = getattr(ino, cls_name)
        for a in attrs:
            assert callable(getattr(cls, a, None)), f"{cls_name}.{a}"
    assert hasattr(ino, "InotifyFD") and hasattr(ino.InotifyObserver, "on_thread_stop")
    group_fields = ino.InotifyWatchGroup.__dataclass_fields__ if hasattr(
        ino.InotifyWatchGroup, "__dataclass_fields__") else {}
    assert "_active_callbacks_by_path" in group_fields
    assert watcher._pruned_observer_class(lambda root: None) is not None


@linux_only
def test_guard_falls_back_to_stock_observer_when_private_api_missing(proj, monkeypatch):
    from watchdog.observers import inotify as ino
    monkeypatch.delattr(ino.InotifyWatchGroup, "_add_all_callbacks")
    assert watcher._pruned_observer_class(lambda r: None) is None
    wm = WatchManager(lambda k, p: None)
    try:
        obs = wm._obs()
        assert type(obs) is ino.InotifyObserver
    finally:
        wm.stop_all()


@linux_only
def test_pruned_observer_is_used_when_api_present():
    from watchdog.observers import inotify as ino
    wm = WatchManager(lambda k, p: None)
    try:
        obs = wm._obs()
        assert type(obs) is not ino.InotifyObserver and isinstance(obs, ino.InotifyObserver)
    finally:
        wm.stop_all()


# ------------------------------------------------------------------ AC 6: cap_inplace

def test_cap_inplace_truncates_and_keeps_tail(tmp_path):
    p = tmp_path / "e.log"
    data = os.urandom(3 * 1024 * 1024)
    p.write_bytes(data)
    logutil.cap_inplace(p, 2 * 1024 * 1024, keep_tail=1000)
    assert p.stat().st_size == 0
    assert (tmp_path / "e.log.1").read_bytes() == data[-1000:]


def test_cap_inplace_replaces_previous_backup(tmp_path):
    p = tmp_path / "e.log"
    (tmp_path / "e.log.1").write_bytes(b"old" * 100)
    p.write_bytes(b"x" * 500)
    logutil.cap_inplace(p, 100, keep_tail=50)
    assert (tmp_path / "e.log.1").read_bytes() == b"x" * 50


def test_cap_inplace_small_keep_tail_larger_than_file(tmp_path):
    p = tmp_path / "e.log"
    p.write_bytes(b"abc" * 10)
    logutil.cap_inplace(p, 5, keep_tail=1024 * 1024)
    assert (tmp_path / "e.log.1").read_bytes() == b"abc" * 10 and p.stat().st_size == 0


def test_cap_inplace_under_limit_untouched(tmp_path):
    p = tmp_path / "e.log"
    p.write_bytes(b"x" * 100)
    logutil.cap_inplace(p, 100)
    assert p.read_bytes() == b"x" * 100 and not (tmp_path / "e.log.1").exists()


def test_cap_inplace_missing_file_no_exception(tmp_path):
    logutil.cap_inplace(tmp_path / "nope.log", 1)
    assert not (tmp_path / "nope.log.1").exists()


def test_cap_inplace_unwritable_backup_does_not_raise(tmp_path):
    p = tmp_path / "e.log"
    p.write_bytes(b"x" * 100)
    (tmp_path / "e.log.1").mkdir()   # open(..., "wb") fails with IsADirectoryError (an OSError)
    logutil.cap_inplace(p, 10)
    assert p.read_bytes() == b"x" * 100   # nothing lost when the backup could not be written


def test_cap_inplace_append_fd_writes_from_zero_without_hole(tmp_path):
    p = tmp_path / "e.log"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(fd, b"a" * 5000)
        logutil.cap_inplace(p, 1000, keep_tail=100)
        os.write(fd, b"after\n")
    finally:
        os.close(fd)
    assert p.read_bytes() == b"after\n"


# ------------------------------------------------------------------ AC 7: lifespan

def test_lifespan_background_caps_oversized_daemon_logs(env, monkeypatch):
    from starlette.testclient import TestClient

    from myvec import config, nodeload

    class FakeSrc:
        def system(self):
            return {"cores": [1.0], "cpu_pct": 1.0, "load": (0, 0, 0), "mem_used": 1, "mem_total": 2,
                    "swap_used": 0, "freq_mhz": None, "temp_c": None}

        def procs(self):
            return []

        def host_info(self):
            return {"hostname": "h", "cpu_model": "", "cores": 1, "ram_total": 2}

    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": FakeSrc())
    monkeypatch.setattr(daemon, "ERR_LOG_MAX", 1000)
    logs = config.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "daemon.err.log").write_bytes(b"E" * 5000)
    (logs / "daemon.out.log").write_bytes(b"O" * 5000)
    other = logs / "other.log"
    other.write_bytes(b"z" * 5000)
    st = daemon.DaemonState(7897, "tok", background=True)
    try:
        with TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7897"):
            pass
    finally:
        st.close()
    assert (logs / "daemon.err.log").stat().st_size == 0
    assert (logs / "daemon.out.log").stat().st_size == 0
    assert (logs / "daemon.err.log.1").read_bytes() == b"E" * 5000
    assert other.stat().st_size == 5000


def test_cap_daemon_logs_uses_err_log_max_constant(env, monkeypatch):
    from myvec import config
    logs = config.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "daemon.err.log").write_bytes(b"E" * 3000)
    (logs / "daemon.out.log").write_bytes(b"O" * 10)
    monkeypatch.setattr(daemon, "ERR_LOG_MAX", 2000)
    daemon._cap_daemon_logs()
    assert (logs / "daemon.err.log").stat().st_size == 0
    assert (logs / "daemon.out.log").read_bytes() == b"O" * 10
    assert daemon.ERR_LOG_MAX == 2000
