"""E6-11: /api/status heavy per-repo counts are cached, shared between tabs and run outside db_lock."""
import threading
import time

import pytest

from conftest import do_sync, write
from myvec import daemon

PORT = 7878


@pytest.fixture
def st(ws):
    do_sync(ws)
    s = daemon.DaemonState(PORT, "t")
    s.refresh()
    yield s
    s.executor.shutdown(wait=True)
    s.close()


def _count_stats(monkeypatch):
    calls = []
    real = daemon.DaemonState._repo_stats

    def spy(db, rid, br):
        calls.append(rid)
        return real(db, rid, br)

    monkeypatch.setattr(daemon.DaemonState, "_repo_stats", staticmethod(spy))
    return calls


def test_repo_stats_cached_and_invalidated_by_sync(st, ws, monkeypatch):
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 0.0)   # payload cache off: isolate the per-repo cache
    calls = _count_stats(monkeypatch)
    first = st.status()
    n = len(calls)
    assert n >= 1
    for _ in range(3):
        assert st.status()["projects"] == first["projects"]
    assert len(calls) == n
    write(ws / "new_file.py", "def fresh():\n    return 1\n")
    time.sleep(0.01)
    do_sync(ws)
    st.status()
    assert len(calls) > n


def test_payload_cache_shared_within_ttl(st, monkeypatch):
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    built = []
    real = st._build_status
    monkeypatch.setattr(st, "_build_status", lambda: built.append(1) or real())
    out = []
    ts = [threading.Thread(target=lambda: out.append(st.status())) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(built) == 1 and out[0]["projects"] == out[1]["projects"]


def test_live_fields_not_stale(st, monkeypatch):
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    path = st.status()["projects"][0]["path"]
    first = st.status()
    st.progress[path] = {"done": 1, "total": 2}
    st._queued.add(path)
    time.sleep(0.01)
    out = st.status()
    assert out["projects"][0]["progress"] == {"done": 1, "total": 2}
    assert out["projects"][0]["syncing"] is True
    assert out["now"] > first["now"]


def test_mode_change_visible_immediately(st, monkeypatch):
    from myvec import activity
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    p = st.status()["projects"][0]
    other = "off" if p["mode"] != "off" else "always"
    st.status()
    with st.db_lock:
        activity.set_mode(st.db(), p["path"], other, None)
    st.refresh()
    assert st.status()["projects"][0]["mode"] == other


def test_mode_post_then_status(st, monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    c = TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
    p = c.get("/api/status").json()["projects"][0]
    other = "off" if p["mode"] != "off" else "always"
    r = c.post(f"/api/projects/{p['path']}/mode", json={"mode": other}, headers={"X-CSRF-Token": "t"})
    assert r.status_code == 200
    assert c.get("/api/status").json()["projects"][0]["mode"] == other


def test_removed_repo_dropped_from_cache(st, monkeypatch):
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 0.0)
    st.status()
    st._repo_stats_cache[(9999, "gone", 1.0)] = (0, 0, 0, 0)
    st.status()
    assert not any(k[0] == 9999 for k in st._repo_stats_cache)


def test_project_detail_db_lock_free_during_repo_queries(st, monkeypatch):
    path = st.status()["projects"][0]["path"]
    seen = []
    real = daemon.DaemonState._repo_detail

    def spy(self, db, r):
        # RLock: another thread can take it only if this one does not hold it
        got = []

        def probe():
            got.append(st.db_lock.acquire(timeout=0.5))
            if got[0]:
                st.db_lock.release()   # must be released by the acquiring thread

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        seen.append(got[0])
        return real(self, db, r)

    monkeypatch.setattr(daemon.DaemonState, "_repo_detail", spy)
    assert st.project_detail(path)["repos"]
    assert seen and all(seen)    # db_lock was free while the heavy per-repo queries ran


def test_parallel_status_counts_once_and_db_lock_free(st, monkeypatch):
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    entered, release = threading.Event(), threading.Event()
    calls = []
    real = daemon.DaemonState._repo_stats

    def slow(db, rid, br):
        calls.append(rid)
        entered.set()
        assert release.wait(10)
        return real(db, rid, br)

    monkeypatch.setattr(daemon.DaemonState, "_repo_stats", staticmethod(slow))
    out = []
    ts = [threading.Thread(target=lambda: out.append(st.status())) for _ in range(2)]
    [t.start() for t in ts]
    assert entered.wait(10)
    t0 = time.monotonic()
    got = st.db_lock.acquire(timeout=1)       # another route is not blocked by the heavy query
    dt = time.monotonic() - t0
    if got:
        st.db_lock.release()
    release.set()
    [t.join() for t in ts]
    assert got and dt < 0.1
    n_repos = sum(len(p["repos"]) for p in out[0]["projects"])
    assert len(calls) == n_repos and len(out) == 2


def test_post_endpoints_invalidate_status(st, monkeypatch):
    """Every POST that changes what /api/status shows drops the cached payload (mode, window_hours, activate, refresh)."""
    from starlette.testclient import TestClient
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    c = TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
    h = {"X-CSRF-Token": "t"}
    p = c.get("/api/status").json()["projects"][0]
    r = c.post(f"/api/projects/{p['path']}/mode", json={"mode": "auto", "window_hours": 7}, headers=h)
    assert r.status_code == 200
    node = c.get("/api/status").json()["projects"][0]
    assert node["mode"] == "auto" and node["window_hours"] == 7
    for url in (f"/api/projects/{p['path']}/activate", "/api/refresh"):
        st._status_cache = (time.monotonic(), {"stale": True})
        assert c.post(url, headers=h).status_code == 200
        assert "stale" not in st._status_cache[1]
        c.get("/api/status")


def test_memory_action_invalidates_status(st, monkeypatch):
    from starlette.testclient import TestClient
    from myvec import config, db as dbmod, memory
    from myvec.embed import get_embedder
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    with st.db_lock:
        m, _ = memory.remember(dbmod.open_ready(), get_embedder(config.load_config()), "status cache note", "global")
    c = TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
    before = c.get("/api/status").json()["global"]["memories"]["total"]
    r = c.post(f"/api/memories/{m.id}/archive", headers={"X-CSRF-Token": "t"})
    assert r.status_code == 200
    assert c.get("/api/status").json()["global"]["memories"]["total"] == before - 1


def test_invalidate_during_build_is_not_lost(st, monkeypatch):
    monkeypatch.setattr(daemon, "STATUS_TTL_S", 60.0)
    real = st._build_status

    def build():
        out = real()
        st.invalidate_status()      # e.g. a POST landed while this build was running
        return out

    monkeypatch.setattr(st, "_build_status", build)
    st.status()
    assert st._status_cache[1] == {}   # the stale payload was not stored
