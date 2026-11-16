"""E6-14: cooperative cancel of a long sync (daemon stopping, Ctrl+C in the CLI)."""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import write
from myvec import config, daemon, embed, indexer, lock as lockmod, projects
from myvec import db as dbmod
from myvec.commands import app
from myvec.embed import FakeEmbedder, get_embedder

runner = CliRunner()
H = {"X-CSRF-Token": "t"}


def _cancel_after_first_batch(monkeypatch):
    monkeypatch.setattr(indexer, "BATCH_CHUNKS", 1)  # one blob per batch
    stats = indexer.SyncStats()
    stats.cancel = lambda: stats.blobs_done >= 1
    return stats


def _run(db, cfg, root, stats=None, embedder=None):
    return indexer.sync_project(db, cfg, embedder or get_embedder(cfg), projects.find_marker(root), lambda s: None, stats)


def _snapshot(db):
    return ([tuple(r) for r in db.execute("SELECT * FROM branch_files ORDER BY 1,2,3")],
            [tuple(r) for r in db.execute("SELECT path, fingerprint FROM repos WHERE path='acme/site'")])


def _count(db, sql):
    return db.execute(sql).fetchone()[0]


def test_stats_check_without_cancel_is_noop():
    indexer.SyncStats().check()
    s = indexer.SyncStats(cancel=lambda: False)
    s.check()
    s.cancel = lambda: True
    with pytest.raises(indexer.SyncCancelled):
        s.check()


def test_cancel_after_first_batch_raises_and_keeps_state(ws, monkeypatch):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    _run(db, cfg, ws)
    _fresh_file(ws)
    before = _snapshot(db)
    blobs_before = _count(db, "SELECT count(*) FROM blobs")
    assert before[0] and all(fp for _, fp in before[1])
    stats = _cancel_after_first_batch(monkeypatch)
    with pytest.raises(indexer.SyncCancelled):
        _run(db, cfg, ws, stats)
    assert not db.in_transaction
    assert stats.blobs_done == 1
    assert _count(db, "SELECT count(*) FROM blobs") == blobs_before + 1  # first batch stored
    assert _snapshot(db) == before  # manifest and fingerprints are written only at the end of a repo


def test_resume_embeds_only_the_rest_and_matches_full_sync(ws, tmp_path, monkeypatch):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    stats = _cancel_after_first_batch(monkeypatch)
    with pytest.raises(indexer.SyncCancelled):
        _run(db, cfg, ws, stats)
    stored = stats.chunks_embedded
    assert stored > 0
    resumed = _run(db, cfg, ws)
    # a full sync on a fresh DB for comparison
    monkeypatch.setenv("MYVEC_HOME", str(tmp_path / "other"))
    config.ensure_home()
    (config.home() / "config.toml").write_text(config.default_config_text().replace("dims = 768", "dims = 64"))
    db2 = dbmod.open_ready(config.load_config())
    full = _run(db2, config.load_config(), ws)
    assert resumed.chunks_embedded == full.chunks_embedded - stored
    for table in ("blobs", "chunks", "branch_files"):
        assert _count(db, f"SELECT count(*) FROM {table}") == _count(db2, f"SELECT count(*) FROM {table}")
    assert _count(db, "SELECT count(*) FROM chunks_vec") == _count(db2, "SELECT count(*) FROM chunks_vec")
    assert db.execute("SELECT path FROM branch_files ORDER BY 1").fetchall() == \
        db2.execute("SELECT path FROM branch_files ORDER BY 1").fetchall()


def test_cancel_in_first_repo_does_not_sync_second_and_is_not_an_error(ws, monkeypatch):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    seen = []
    orig = indexer.sync_repo

    def spy(db_, emb, marker, spec, rid, nested, stats, log):
        seen.append(spec.dir.name)
        return orig(db_, emb, marker, spec, rid, nested, stats, log)

    monkeypatch.setattr(indexer, "sync_repo", spy)
    stats = _cancel_after_first_batch(monkeypatch)
    with pytest.raises(indexer.SyncCancelled):
        _run(db, cfg, ws, stats)
    assert len(seen) == 1  # the other two repos never started
    assert stats.errors == []


def test_cancel_does_not_continue_into_child_project(ws, monkeypatch):
    write(ws / "child" / "c.py", "def child_only_fn():\n    return 1\n")
    write(ws / "child" / ".myvector.toml", 'name = "child"\n')
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    from myvec import ticketdb

    parent_done = []
    real_refresh = ticketdb.refresh_marker

    def refresh(db_, marker):  # runs after all repos of a project, right before its children
        real_refresh(db_, marker)
        parent_done.append(marker.path)

    monkeypatch.setattr(ticketdb, "refresh_marker", refresh)
    stats = indexer.SyncStats(cancel=lambda: bool(parent_done))
    with pytest.raises(indexer.SyncCancelled):
        _run(db, cfg, ws, stats)
    assert len(parent_done) == 1 and stats.repos >= 1  # parent repos finished, cancel hit before the child
    assert stats.errors == []
    assert _count(db, "SELECT count(*) FROM chunks WHERE text LIKE '%child_only_fn%'") == 0


def test_cancel_before_first_repo_stores_nothing(ws):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    stats = indexer.SyncStats(cancel=lambda: True)
    with pytest.raises(indexer.SyncCancelled):
        _run(db, cfg, ws, stats)
    assert _count(db, "SELECT count(*) FROM blobs") == 0 and not db.in_transaction


def test_without_cancel_result_unchanged(ws):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    st = _run(db, cfg, ws)
    assert st.errors == [] and st.repos == 3 and st.cancel is None


# ------------------------------------------------------------------ embedder

def test_embed_docs_check_runs_between_sub_batches():
    cfg = config.load_config()
    cfg.batch_size = 2
    emb = FakeEmbedder(cfg)
    calls = []
    out = emb.embed_docs(["a", "b", "c", "d", "e"], check=lambda: calls.append(1))
    assert len(out) == 5 and len(calls) == 3


def test_embed_docs_check_raises_before_next_sub_batch():
    cfg = config.load_config()
    cfg.batch_size = 2
    emb = FakeEmbedder(cfg)
    embed.STATS.update(texts=0, calls=0)
    n = [0]

    def check():
        n[0] += 1
        if n[0] == 2:
            raise indexer.SyncCancelled()

    with pytest.raises(indexer.SyncCancelled):
        emb.embed_docs(["a", "b", "c", "d", "e"], check=check)
    assert embed.STATS["calls"] == 1 and embed.STATS["texts"] == 2


def test_embed_docs_without_check_and_empty():
    emb = FakeEmbedder(config.load_config())
    assert emb.embed_docs([]) == [] and len(emb.embed_docs(["x"])) == 1


def test_embed_query_has_no_check_parameter():
    import inspect
    assert "check" not in inspect.signature(embed.Embedder.embed_query).parameters


# ------------------------------------------------------------------ daemon

class SlowEmbedder(FakeEmbedder):
    def __init__(self, cfg, delay=0.2):
        super().__init__(cfg)
        self.delay = delay
        self.calls = 0
        self.started = threading.Event()

    def _raw(self, texts, timeout=embed.BATCH_TIMEOUT, url=None):
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        return super()._raw(texts, timeout, url)


@pytest.fixture
def dstate(ws):
    from conftest import do_sync  # noqa: F401 - registers the project in the DB
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    indexer.sync_project(db, cfg, get_embedder(cfg), projects.find_marker(ws))
    db.close()
    st = daemon.DaemonState(7878, "t")
    st.refresh()
    yield st
    st.executor.shutdown(wait=True)
    st.close()


def _types(st):
    return [e["type"] for e in st.bus.recent()]


def _fresh_file(ws):
    write(ws / "site/fresh_cancel.py", "def fresh_cancel_fn():\n    return 'x'\n" * 3)
    write(ws / "site/fresh_cancel2.py", "def fresh_cancel_two():\n    return 'y'\n")


def test_run_sync_stopping_before_start_does_nothing(dstate, ws, monkeypatch):
    _fresh_file(ws)
    emb = SlowEmbedder(config.load_config(), 0)
    monkeypatch.setattr(daemon, "get_embedder", lambda cfg: emb, raising=False)
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: emb)
    dstate.stopping.set()
    dstate._queued.add("acme")
    dstate._run_sync("acme", str(ws), "manual")
    types = _types(dstate)
    assert "sync_started" not in types and "sync_failed" not in types and "sync_cancelled" not in types
    assert emb.calls == 0
    assert "acme" not in dstate._queued and not lockmod.is_locked()


def test_run_sync_cancelled_midway_frees_lock_and_queue(dstate, ws, monkeypatch):
    _fresh_file(ws)
    monkeypatch.setattr(indexer, "BATCH_CHUNKS", 1)
    emb = SlowEmbedder(config.load_config(), 0.3)
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: emb)
    dstate._queued.add("acme")
    th = threading.Thread(target=dstate._run_sync, args=("acme", str(ws), "manual"))
    t0 = time.time()
    th.start()
    assert emb.started.wait(10)
    dstate.stopping.set()
    th.join(10)
    assert not th.is_alive()
    assert time.time() - t0 < 5
    types = _types(dstate)
    assert "sync_cancelled" in types and "sync_failed" not in types and "sync_finished" not in types
    ev = [e for e in dstate.bus.recent() if e["type"] == "sync_cancelled"][-1]
    assert ev["project"] == "acme" and ev["reason"] == "daemon stopping"
    assert emb.calls <= 2  # at most the batch in flight (+ one) after the stop
    assert not lockmod.is_locked() and "acme" not in dstate._queued
    assert "cancelled: daemon stopping" in (config.logs_dir() / "sync.log").read_text()


def test_executor_shutdown_is_fast_after_stop_mid_sync(dstate, ws, monkeypatch):
    _fresh_file(ws)
    monkeypatch.setattr(indexer, "BATCH_CHUNKS", 1)
    emb = SlowEmbedder(config.load_config(), 0.3)
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: emb)
    dstate.executor.submit(dstate._run_sync, "acme", str(ws), "manual")
    assert emb.started.wait(10)
    dstate.stopping.set()
    t0 = time.time()
    dstate.executor.shutdown(wait=True)
    assert time.time() - t0 < 2
    assert "sync_cancelled" in _types(dstate)


def test_lifespan_exit_waits_for_running_sync_to_cancel(dstate, ws, monkeypatch):
    """uvicorn re-raises SIGTERM after lifespan shutdown (threads not joined): the cancel must be logged before."""
    from starlette.testclient import TestClient
    from myvec import nodeload
    _fresh_file(ws)
    monkeypatch.setattr(indexer, "BATCH_CHUNKS", 1)
    emb = SlowEmbedder(config.load_config(), 0.3)
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: emb)
    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": None, raising=False)
    st = daemon.DaemonState(7878, "t", background=True)
    monkeypatch.setattr(st, "start_load", lambda cfg: None)
    try:
        t0 = None
        with TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878"):
            assert st.request_sync("acme", "manual")
            assert emb.started.wait(10)
            t0 = time.time()
        assert time.time() - t0 < daemon.SYNC_STOP_WAIT_S
        types = _types(st)
        assert "sync_cancelled" in types and "sync_failed" not in types
        assert types.index("sync_cancelled") < types.index("daemon_stop")
        assert "cancelled: daemon stopping" in (config.logs_dir() / "sync.log").read_text()
        assert not lockmod.is_locked()
    finally:
        st.close()


def test_wait_sync_stopped_without_sync_returns_at_once(dstate):
    t0 = time.time()
    assert dstate.wait_sync_stopped(timeout=5) is True
    assert time.time() - t0 < 1


def test_wait_sync_stopped_times_out_when_sync_ignores_cancel(dstate):
    release = threading.Event()
    fut = dstate.executor.submit(release.wait, 30)
    dstate._sync_futures.add(fut)
    try:
        t0 = time.time()
        assert dstate.wait_sync_stopped(timeout=0.3) is False
        assert 0.25 < time.time() - t0 < 3
    finally:
        release.set()
        fut.result(10)


def test_wait_sync_stopped_ignores_cancelled_queued_futures(dstate):
    release = threading.Event()
    running = dstate.executor.submit(release.wait, 30)
    queued = dstate.executor.submit(lambda: None)
    assert queued.cancel()
    dstate._sync_futures.add(queued)
    release.set()
    running.result(10)
    t0 = time.time()
    assert dstate.wait_sync_stopped(timeout=5) is True
    assert time.time() - t0 < 1


def test_request_sync_future_is_dropped_after_done(dstate, ws, monkeypatch):
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: get_embedder(cfg))
    assert dstate.request_sync("acme", "manual")
    assert dstate.wait_sync_stopped(timeout=20)
    deadline = time.time() + 5
    while dstate._sync_futures and time.time() < deadline:
        time.sleep(0.02)
    assert not dstate._sync_futures


def test_lifespan_exit_is_bounded_when_sync_ignores_cancel(dstate, monkeypatch):
    from starlette.testclient import TestClient
    from myvec import nodeload
    monkeypatch.setattr(daemon, "SYNC_STOP_WAIT_S", 0.5)
    monkeypatch.setattr(daemon.DaemonState, "wait_sync_stopped",
                        lambda self, timeout=0.5: _orig_wait(self, 0.5))
    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": None, raising=False)
    st = daemon.DaemonState(7878, "t", background=True)
    monkeypatch.setattr(st, "start_load", lambda cfg: None)
    release = threading.Event()
    fut = st.executor.submit(release.wait, 30)
    st._sync_futures.add(fut)
    try:
        with TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878"):
            t0 = time.time()
        assert 0.4 < time.time() - t0 < 5
    finally:
        release.set()
        fut.result(10)
        st.close()


def test_lifespan_exit_without_sync_does_not_wait(dstate, monkeypatch):
    from starlette.testclient import TestClient
    from myvec import nodeload
    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": None, raising=False)
    st = daemon.DaemonState(7878, "t", background=True)
    monkeypatch.setattr(st, "start_load", lambda cfg: None)
    try:
        with TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878"):
            t0 = time.time()
        assert time.time() - t0 < 3
    finally:
        st.close()


_orig_wait = daemon.DaemonState.wait_sync_stopped


def test_cancelled_daemon_sync_resumes_next_time(dstate, ws, monkeypatch):
    _fresh_file(ws)
    monkeypatch.setattr(indexer, "BATCH_CHUNKS", 1)
    emb = SlowEmbedder(config.load_config(), 0.2)
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: emb)
    th = threading.Thread(target=dstate._run_sync, args=("acme", str(ws), "manual"))
    th.start()
    assert emb.started.wait(10)
    dstate.stopping.set()
    th.join(10)
    dstate.stopping.clear()
    dstate._run_sync("acme", str(ws), "manual")
    assert "sync_finished" in _types(dstate)
    db = dbmod.open_ready()
    assert _count(db, "SELECT count(*) FROM branch_files WHERE path='fresh_cancel2.py'") == 1


# ------------------------------------------------------------------ CLI

class InterruptingEmbedder(FakeEmbedder):
    def _raw(self, texts, timeout=embed.BATCH_TIMEOUT, url=None):
        raise KeyboardInterrupt


def test_cli_ctrl_c_exits_130_logs_and_frees_lock(ws, monkeypatch):
    monkeypatch.chdir(ws)
    real = embed.get_embedder
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: InterruptingEmbedder(cfg))
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 130
    assert "Traceback" not in (r.output or "")
    assert "interrupted (Ctrl+C)" in (config.logs_dir() / "sync.log").read_text()
    assert not lockmod.is_locked()
    monkeypatch.setattr(embed, "get_embedder", real)
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 0 and "synced acme" in r.stdout


def test_cli_ctrl_c_quiet_prints_nothing_but_exits_130(ws, monkeypatch):
    monkeypatch.chdir(ws)
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: InterruptingEmbedder(cfg))
    r = runner.invoke(app, ["sync", "--quiet"])
    assert r.exit_code == 130 and not r.output.strip()
    assert "interrupted" in (config.logs_dir() / "sync.log").read_text()


# ------------------------------------------------------------------ UI (node)

VIEWS = Path(__file__).parent.parent / "src" / "myvec" / "ui" / "views.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _js(expr: str):
    code = f"import * as v from {json.dumps(VIEWS.as_uri())}; console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@node
def test_ui_sync_cancelled_is_neutral_journal_row():
    d = _js("(({cats, tag, tagCls, danger, title, info}) => ({cats, tag, tagCls, danger, title: String(title), info}))"
            "(v.describeEvent({type: 'sync_cancelled', project: 'acme', reason: 'daemon stopping'}))")
    assert d["cats"] == ["sync"] and d["danger"] is False and d["tagCls"] == "t-primary"
    assert "перервано" in d["title"] and "наступний синк продовжить" in d["info"]
    assert "error" not in d["cats"]


@node
def test_ui_sync_cancelled_is_listed_in_journal():
    assert _js("v.isListed({type: 'sync_cancelled'})") is True


@node
def test_ui_sync_cancelled_ends_live_progress_in_journal():
    # liveProgress is private; the journal row of sync_started must stop showing progress once a terminal event follows
    src = VIEWS.read_text()
    terminal = src.split("function liveProgress")[1].split("function journalRow")[0]
    assert "'sync_cancelled'" in terminal and "'sync_finished'" in terminal
