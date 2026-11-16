import json
import os
import plistlib
import sys
import time

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import activity, config, daemon, daemon_client, db as dbmod, launchd, memory, searchcmd, systemd
from myvec.bus import EventBus
from myvec.commands import app as cli_app
from myvec.embed import get_embedder
from myvec.scheduler import Scheduler
from myvec.watcher import EventFilter

PORT = 7878
TOKEN = "test-token"
H = {"X-CSRF-Token": TOKEN}
runner = CliRunner()


@pytest.fixture
def state(ws):
    do_sync(ws)
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    yield st
    st.executor.shutdown(wait=True)
    st.close()


@pytest.fixture
def client(state):
    return TestClient(daemon.create_app(state), base_url=f"http://127.0.0.1:{PORT}")


def wait_event(client, type_, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        evs = client.get("/api/events/recent?limit=500").json()["events"]
        hit = [e for e in evs if e["type"] == type_]
        if hit:
            return hit[-1]
        time.sleep(0.1)
    raise AssertionError(f"no {type_} event: {[e['type'] for e in evs]}")


# ------------------------------------------------------------------ security

def test_host_header_guard(client):
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status", headers={"Host": f"localhost:{PORT}"}).status_code == 200
    for bad in ("evil.com", f"evil.com:{PORT}", "127.0.0.1:9999", f"127.0.0.1.evil.com:{PORT}", ""):
        r = client.get("/api/status", headers={"Host": bad})
        assert r.status_code == 403, bad
    assert client.get("/", headers={"Host": "evil.com"}).status_code == 403


def test_csrf_and_origin_guard(client):
    url = "/api/projects/acme/mode"
    body = {"mode": "always"}
    assert client.post(url, json=body).status_code == 403                                  # no token
    assert client.post(url, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    r = client.post(url, json=body, headers={**H, "Origin": "http://evil.com"})
    assert r.status_code == 403 and "cross-origin" in r.json()["detail"]
    assert client.post(url, json=body, headers={**H, "Origin": f"http://127.0.0.1:{PORT}"}).status_code == 200
    assert client.post(url, json=body, headers={**H, "Origin": f"http://localhost:{PORT}"}).status_code == 200
    assert client.post(url, json=body, headers={**H, "Origin": "null"}).status_code == 403
    assert client.post("/api/cli/search", json={"q": "x"}).status_code == 403
    assert client.get("/api/status").status_code == 200                                    # GET needs no token


def test_index_page_injects_csrf_and_static_dir(client):
    r = client.get("/")
    assert r.status_code == 200 and f'<meta name="csrf-token" content="{TOKEN}">' in r.text
    assert "{{CSRF_TOKEN}}" not in r.text and 'lang="uk"' in r.text
    assert client.get("/static/index.html").status_code == 200
    assert client.get("/static/nope.js").status_code == 404


# ------------------------------------------------------------------ status / projects

def test_status_tree(client, ws):
    child = ws / "child"
    write(child / "c.py", "def child_fn_zz():\n    return 1\n")
    write(child / ".myvector.toml", 'name = "child"\n')
    do_sync(ws)
    st = client.get("/api/status").json()
    assert st["global"]["model"] == "embeddinggemma" and st["global"]["dims"] == 64
    assert st["global"]["ollama"]["reachable"] is None                      # fake embedder: not probed
    assert st["global"]["db_size_bytes"] > 0 and st["global"]["memories"]["total"] == 0
    assert [p["path"] for p in st["projects"]] == ["acme"]
    acme = st["projects"][0]
    assert acme["mode"] == "auto" and acme["window_hours"] == 2.0 and acme["active"] is True
    assert acme["active_until"] > time.time() and acme["last_access"] > 0
    assert acme["syncing"] is False and acme["progress"] is None
    repos = {r["path"]: r for r in acme["repos"]}
    assert set(repos) == {"acme", "acme/site", "acme/libs"}
    site = repos["acme/site"]
    assert site["current_branch"] == "main" and site["branches"] == ["main"] and site["dir"].endswith("site")
    assert site["chunks"] > 0 and site["blobs"] > 0 and site["last_sync"] and site["dropped"] == 1 and site["masked"] == 0
    assert [c["path"] for c in acme["children"]] == ["acme/child"]            # sub-project nested in the tree


def test_mode_change_activate_and_events(client, state):
    r = client.post("/api/projects/acme/mode", json={"mode": "always", "window_hours": 5}, headers=H)
    assert r.status_code == 200 and r.json()["mode"] == "always" and r.json()["window_hours"] == 5
    assert dbmod.open_ready().execute("SELECT sync_mode, window_hours FROM projects").fetchone()[:] == ("always", 5.0)
    ev = wait_event(client, "mode_changed")
    assert ev["project"] == "acme" and ev["mode"] == "always"
    assert client.post("/api/projects/acme/mode", json={"mode": "weird"}, headers=H).status_code == 400
    assert client.post("/api/projects/acme/mode", json={"mode": "auto", "window_hours": -1}, headers=H).status_code == 400
    assert client.post("/api/projects/nope/mode", json={"mode": "auto"}, headers=H).status_code == 404
    client.post("/api/projects/acme/mode", json={"mode": "auto"}, headers=H)
    state.project_row  # noqa
    db = dbmod.open_ready()
    db.execute("UPDATE projects SET last_access=1")
    r = client.post("/api/projects/acme/activate", headers=H)
    assert r.status_code == 200 and r.json()["active"] is True
    assert time.time() - db.execute("SELECT last_access FROM projects").fetchone()[0] < 10
    assert client.post("/api/projects/nope/activate", headers=H).status_code == 404


def test_project_path_with_slash(client, ws):
    # sub-project path "acme/child" contains a slash
    write(ws / "child" / "c.py", "def child_fn_zz():\n    return 1\n")
    write(ws / "child" / ".myvector.toml", 'name = "child"\n')
    do_sync(ws)
    r = client.post("/api/projects/acme/child/mode", json={"mode": "off"}, headers=H)
    assert r.status_code == 200 and r.json()["mode"] == "off"


def test_refresh_follows_activity_windows(state, client):
    assert state.sched.active == {"acme"}
    db = dbmod.open_ready()
    db.execute("UPDATE projects SET last_access=1")          # window long expired
    state.refresh()
    assert state.sched.active == set()
    assert any(e["type"] == "watcher_stop" for e in state.bus.recent())
    db.execute("UPDATE projects SET sync_mode='always'")
    state.refresh()
    assert state.sched.active == {"acme"}
    db.execute("UPDATE projects SET sync_mode='off'")
    state.refresh()
    assert state.sched.active == set()
    # queued accesses from hooks are folded in
    db.execute("UPDATE projects SET sync_mode='auto', last_access=1")
    config.activity_path().write_text(f"{time.time()}\tacme\n")
    state.refresh()
    assert state.sched.active == {"acme"}


def test_manual_sync_emits_events(client, ws):
    write(ws / "site/fresh_file.py", "def fresh_file_fn():\n    return 'new'\n")
    r = client.post("/api/projects/acme/sync", headers=H)
    assert r.status_code == 200 and r.json() == {"queued": True, "project": "acme"}
    fin = wait_event(client, "sync_finished")
    assert fin["project"] == "acme" and fin["new_blobs"] >= 1 and fin["reason"] == "manual"
    types = [e["type"] for e in client.get("/api/events/recent?limit=500").json()["events"]]
    assert types.index("sync_started") < types.index("sync_finished")
    assert "synced acme" in (config.logs_dir() / "sync.log").read_text()
    db = dbmod.open_ready()
    assert db.execute("SELECT count(*) FROM branch_files WHERE path='fresh_file.py'").fetchone()[0] == 1
    assert client.post("/api/projects/nope/sync", headers=H).status_code == 404


def test_sync_busy_when_lock_held(client):
    from myvec import lock
    with lock.sync_lock() as got:
        assert got
        client.post("/api/projects/acme/sync", headers=H)
        wait_event(client, "sync_busy")
    # the scheduler wants a retry after the lock is released
    time.sleep(0.2)


# ------------------------------------------------------------------ events

def test_events_recent_and_sse(client, state):
    state.bus.publish("custom", n=1)
    state.bus.publish("custom", n=2)
    ev = client.get("/api/events/recent?limit=2").json()["events"]
    assert [e["n"] for e in ev] == [1, 2] and ev[0]["id"] < ev[1]["id"]
    r = client.get("/api/events?follow=0")
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in r.text.split("\n\n") if f.strip()]
    assert any("event: custom" in f and '"n": 2' in f for f in frames)
    first_id = ev[0]["id"]
    r = client.get(f"/api/events?follow=0&since={first_id}")
    assert '"n": 1' not in r.text and '"n": 2' in r.text


def test_event_bus_ring_and_rotation(tmp_path):
    p = tmp_path / "events.jsonl"
    bus = EventBus(p, ring=5, rotate_bytes=400)
    for i in range(30):
        bus.publish("x", i=i, pad="p" * 30)
    assert [e["i"] for e in bus.recent(100)] == list(range(25, 30))
    assert (tmp_path / "events.jsonl.1").exists() and p.stat().st_size <= 600
    bus2 = EventBus(p, ring=5)
    assert bus2.publish("y")["id"] > bus.recent(1)[0]["id"]            # ids continue after restart
    assert bus2.recent(1)[0]["type"] == "y"


# ------------------------------------------------------------------ memories

@pytest.fixture
def mems(state, ws):
    db = dbmod.open_ready()
    emb = get_embedder(config.load_config())
    a, _ = memory.remember(db, emb, "invoices are rounded to quarter cents", "acme", tags=["fin"])
    b, _ = memory.remember(db, emb, "the deploy pipeline is blue green", "acme/site")
    c, _ = memory.remember(db, emb, "always answer in Ukrainian", "global")
    d, _ = memory.remember(db, emb, "branch only zebra note", "acme", branch="feature", origin_branch="feature",
                           origin_repo="acme/site")
    return a, b, c, d


def test_memories_list_filter_and_search(client, mems):
    a, b, c, d = mems
    items = client.get("/api/memories").json()["memories"]
    assert {m["id"] for m in items} == {a.id, b.id, c.id, d.id}
    assert items[0]["id"] in {a.id, b.id, c.id, d.id} and {"text", "scope", "pinned", "tags", "archived"} <= set(items[0])
    assert {m["id"] for m in client.get("/api/memories?scope=acme").json()["memories"]} == {a.id, b.id, d.id}
    assert {m["id"] for m in client.get("/api/memories?scope=acme/site").json()["memories"]} == {b.id}
    assert len(client.get("/api/memories?limit=2").json()["memories"]) == 2
    hits = client.get("/api/memories?q=deploy pipeline blue green").json()["memories"]
    assert hits[0]["id"] == b.id and hits[0]["score"] > 0
    zebra = client.get("/api/memories?q=zebra").json()["memories"]            # branch memories are visible here
    assert zebra and zebra[0]["id"] == d.id and zebra[0]["branch"] == "feature"


def test_memory_actions(client, mems, env):
    a, b, c, d = mems
    r = client.post(f"/api/memories/{a.id}/pin")
    assert r.status_code == 403                                               # CSRF
    r = client.post(f"/api/memories/{a.id}/pin", headers=H)
    assert r.status_code == 200 and r.json()["memory"]["pinned"] is True
    assert client.post(f"/api/memories/{a.id}/unpin", headers=H).json()["memory"]["pinned"] is False
    r = client.post(f"/api/memories/{b.id}/promote", headers=H)
    assert r.json()["memory"]["scope"] == "global" and list((env / "vault/global").glob(f"*{b.id}.md"))
    assert client.post(f"/api/memories/{b.id}/promote", headers=H).status_code == 400
    r = client.post(f"/api/memories/{a.id}/archive", headers=H)
    assert r.json()["memory"]["archived"] is True
    assert a.id not in {m["id"] for m in client.get("/api/memories").json()["memories"]}
    assert a.id in {m["id"] for m in client.get("/api/memories?include_archived=1").json()["memories"]}
    assert a.id in {m["id"] for m in client.get("/api/memories?q=quarter&include_archived=true").json()["memories"]}
    assert client.post("/api/memories/nope/pin", headers=H).status_code == 404
    assert client.post(f"/api/memories/{c.id}/explode", headers=H).status_code == 404
    assert [e["action"] for e in client.get("/api/events/recent").json()["events"] if e["type"] == "memory_action"]


# ------------------------------------------------------------------ search

def test_search_breakdown(client, ws):
    cwd = str(ws / "site")
    r = client.get("/api/search", params={"q": "renderPdf", "cwd": cwd, "kind": "code"})
    assert r.status_code == 200
    j = r.json()
    assert j["route"] == "hybrid" and j["context"] == {"project": "acme", "repo": "acme/site", "branch": "main"}
    top = j["results"][0]
    assert top["symbol"] == "Invoice.renderPdf" or "renderPdf" in top["text"]
    bd = top["breakdown"]
    assert bd["route"] == "hybrid" and bd["vector_rank"] and bd["fts_rank"] == 1
    assert bd["weight"] == 1.0 and bd["weight_eff"] == 1.0 and bd["rrf"] > 0
    assert top["score"] == pytest.approx(bd["rrf"] / bd["rrf_max"] * bd["weight_eff"], rel=1e-3) and 0 < top["score"] <= 1
    assert "weights" in j and j["ms"] >= 0
    j = client.get("/api/search", params={"q": "where is the invoice total", "cwd": cwd, "kind": "code"}).json()
    assert j["route"] == "vector" and all(h["breakdown"]["fts_rank"] is None for h in j["results"])
    v = j["results"][0]
    assert v["breakdown"]["similarity"] is not None and v["breakdown"]["route"] == "vector"
    assert v["score"] == pytest.approx(v["breakdown"]["similarity"] * v["breakdown"]["weight_eff"], rel=1e-3)
    j = client.get("/api/search", params={"q": "where is the invoice total", "cwd": cwd, "mode": "hybrid"}).json()
    assert j["route"] == "hybrid"
    # sibling repo gets the sibling weight
    j = client.get("/api/search", params={"q": "libs_only_helper", "cwd": cwd, "kind": "code"}).json()
    assert j["results"][0]["breakdown"]["weight"] == 0.8 and j["results"][0]["breakdown"]["weight_eff"] == pytest.approx(0.9)
    assert client.get("/api/search").status_code == 400
    assert client.get("/api/search", params={"q": "x", "scope": "bogus"}).status_code == 400
    assert any(e["type"] == "search" and e["via"] == "daemon" for e in client.get("/api/events/recent").json()["events"])


CASES = [
    ["invoice total price qty"], ["renderPdf"], ["renderPdf", "--json"], ["invoice", "--kind", "code", "--k", "3"],
    ["deploy blue pipeline", "--kind", "memory"], ["renderPdf", "--full", "--k", "1"], ["zzz_nothing_here_zzz"],
    ["invoice", "--scope", "repo", "--mode", "vector"],
]


def test_cli_search_parity_direct_vs_daemon(client, ws, monkeypatch):
    monkeypatch.chdir(ws / "site")
    db = dbmod.open_ready()
    memory.remember(db, get_embedder(config.load_config()), "deploy runs through the blue pipeline", "acme")

    def via_daemon(method, path, body=None, **kw):
        r = client.post(path, json=body or {}, headers=H) if method == "POST" else client.get(path)
        return r.json() if r.status_code == 200 else None

    for args in CASES:
        monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)         # no daemon -> direct
        direct = runner.invoke(cli_app, ["search", *args])
        monkeypatch.setattr(daemon_client, "request", via_daemon)
        remote = runner.invoke(cli_app, ["search", *args])
        assert direct.exit_code == remote.exit_code == 0, (args, direct.output, remote.output)
        assert direct.stdout == remote.stdout and direct.stderr == remote.stderr, args
    # the daemon really answered
    assert [e for e in client.get("/api/events/recent?limit=500").json()["events"] if e["type"] == "search"]


def test_cli_search_errors_identical(client, ws, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                                                   # outside any project
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    d = runner.invoke(cli_app, ["search", "anything", "--kind", "memory"])
    monkeypatch.setattr(daemon_client, "request",
                        lambda m, p, b=None, **k: client.post(p, json=b, headers=H).json())
    r = runner.invoke(cli_app, ["search", "anything", "--kind", "memory"])
    assert d.stdout == r.stdout and d.stderr == r.stderr and "not inside a myvec project" in r.stderr
    bad = client.post("/api/cli/search", json={"q": "x", "scope": "nope"}, headers=H).json()
    assert bad["exit_code"] == 1 and "--scope" in bad["stderr"]


def test_direct_search_logs_query_and_daemon_tails_it(client, state, ws, monkeypatch):
    monkeypatch.chdir(ws / "site")
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    state._queries_pos = state._queries_size()
    assert runner.invoke(cli_app, ["search", "renderPdf"]).exit_code == 0
    rec = json.loads((config.logs_dir() / "queries.jsonl").read_text().splitlines()[-1])
    assert rec["q"] == "renderPdf" and rec["via"] == "direct" and rec["results"] > 0 and rec["tokens"] > 0 and rec["ms"] >= 0
    assert rec["scope"] == "project" and rec["cwd"].endswith("site")
    state.tick()
    ev = [e for e in state.bus.recent() if e["type"] == "search" and e["via"] == "direct"]
    assert ev and ev[-1]["q"] == "renderPdf"
    n = len([e for e in state.bus.recent(500) if e["type"] == "search"])
    state.tick()
    # not replayed twice (only search events: the first tick also starts an asynchronous initial sync)
    assert len([e for e in state.bus.recent(500) if e["type"] == "search"]) == n


def test_daemon_search_does_not_double_log(client, ws):
    client.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H)
    assert not (config.logs_dir() / "queries.jsonl").exists()


def test_daemon_search_flags_stale_index(client, ws):
    git(ws / "site", "checkout", "-q", "-b", "other")
    from conftest import TRIGGERED
    # only the POST variant may trigger a background sync (GET /api/search is read-only, see test_review_fixes)
    j = client.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H).json()
    assert j["index_refreshing"] is True
    wait_event(client, "sync_finished")                      # the daemon synced itself (no external trigger)
    assert not TRIGGERED


# ------------------------------------------------------------------ daemon.json / client

def test_daemon_json_and_client(state):
    daemon._write_daemon_json(state)
    p = config.daemon_json_path()
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    d = json.loads(p.read_text())
    assert d["port"] == PORT and d["token"] == TOKEN and d["pid"] == os.getpid()
    assert daemon_client.read_daemon()["token"] == TOKEN
    p.write_text(json.dumps({**d, "pid": 2 ** 30}))
    assert daemon_client.read_daemon() is None                                    # stale pid
    p.write_text("garbage")
    assert daemon_client.read_daemon() is None
    p.unlink()
    assert daemon_client.read_daemon() is None
    assert daemon_client.request("POST", "/api/cli/search", {"q": "x"}) is None   # nothing listening


def test_daemon_client_against_real_socket(state):
    import threading

    import uvicorn

    app = daemon.create_app(state)
    port = 17878
    state.port = port
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        info = {"port": port, "token": TOKEN, "pid": os.getpid()}
        r = daemon_client.request("GET", "/api/events/recent?limit=1", info=info)
        assert r is not None and "events" in r
        r = daemon_client.request("POST", "/api/refresh", {}, info=info)
        assert r == {"ok": True}
        assert daemon_client.request("POST", "/api/refresh", {}, info={**info, "token": "bad"}) is None
    finally:
        server.should_exit = True
        t.join(5)


def _stop_with_open_sse(background):
    import signal
    import threading

    import httpx

    port = _free_port()
    state = daemon.DaemonState(port, TOKEN, background=background)
    server = daemon.make_server(daemon.create_app(state), state, port, log_level="error")
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(400):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    state.bus.publish("test_ping")          # the stream yields something right away
    got = threading.Event()
    done = threading.Event()

    def client():
        try:
            with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", timeout=30) as r:
                for _ in r.iter_lines():
                    got.set()
                    if done.is_set():
                        break
        except httpx.HTTPError:
            pass

    c = threading.Thread(target=client, daemon=True)
    c.start()
    assert got.wait(5)                      # the stream is open
    jsons = config.daemon_json_path()
    assert jsons.exists() == background
    sampler = state.load._thread if background and state.load is not None else None
    t0 = time.time()
    server.handle_exit(signal.SIGTERM, None)
    t.join(10)
    elapsed = time.time() - t0
    done.set()
    assert not t.is_alive(), "daemon did not stop with an open SSE client"
    assert elapsed < 6
    return jsons, sampler


def test_stops_fast_with_open_sse_client():
    _stop_with_open_sse(False)


def test_stops_fast_with_open_sse_background():
    jsons, sampler = _stop_with_open_sse(True)
    assert not jsons.exists()
    assert sampler is not None
    assert not sampler.is_alive()


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server():
    import threading

    port = _free_port()
    state = daemon.DaemonState(port, TOKEN, background=False)
    server = daemon.make_server(daemon.create_app(state), state, port, log_level="error")
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(400):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    return state, server, t, port


def _read_sse(port, lines, ended, stop_after=None):
    import httpx

    try:
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", timeout=30) as r:
            for line in r.iter_lines():
                lines.append(line)
                if stop_after and stop_after(lines):
                    break
    except httpx.HTTPError:
        pass
    ended.set()


def test_sse_ping_still_emitted(monkeypatch):
    import threading

    clock = {"t": 0.0}

    class _Time:
        def __getattr__(self, name):
            return getattr(time, name)

        def monotonic(self):
            clock["t"] += 20.0            # every check looks like 15 s passed
            return clock["t"]

    monkeypatch.setattr(daemon, "SSE_POLL", 0.05)
    monkeypatch.setattr(daemon, "time", _Time())
    state, server, t, port = _start_server()
    lines, ended = [], threading.Event()
    c = threading.Thread(target=_read_sse, args=(port, lines, ended, lambda ls: ": ping" in ls), daemon=True)
    c.start()
    try:
        assert ended.wait(10), "no ping within 10 s"
        assert ": ping" in lines
    finally:
        server.should_exit = True
        t.join(10)
    assert not t.is_alive()


def test_sse_stream_ends_when_stopping_set():
    import threading

    state, server, t, port = _start_server()
    try:
        state.bus.publish("test_ping")
        lines, ended = [], threading.Event()
        c = threading.Thread(target=_read_sse, args=(port, lines, ended), daemon=True)
        c.start()
        deadline = time.time() + 5
        while not lines and time.time() < deadline:
            time.sleep(0.05)
        assert lines
        assert not ended.is_set()
        state.stopping.set()
        assert ended.wait(5), "stream did not end after stopping was set"
    finally:
        server.should_exit = True
        t.join(10)
    assert not t.is_alive()


def test_stops_with_open_sse_when_only_should_exit_set():
    """The graceful timeout is the safety net: no handle_exit, no stopping flag."""
    import threading

    state, server, t, port = _start_server()
    lines, ended = [], threading.Event()
    c = threading.Thread(target=_read_sse, args=(port, lines, ended), daemon=True)
    c.start()
    deadline = time.time() + 5
    while not lines and time.time() < deadline:
        state.bus.publish("test_ping")
        time.sleep(0.1)
    assert lines and not state.stopping.is_set()
    t0 = time.time()
    server.should_exit = True
    t.join(12)
    assert not t.is_alive(), "server hung on an open SSE client"
    assert time.time() - t0 < 8
    assert server.config.timeout_graceful_shutdown == daemon.GRACEFUL_STOP_S


def test_make_server_handle_exit_sets_stopping():
    import signal

    state = daemon.DaemonState(_free_port(), TOKEN, background=False)
    server = daemon.make_server(daemon.create_app(state), state, 0)
    assert not state.stopping.is_set()
    server.handle_exit(signal.SIGTERM, None)
    assert state.stopping.is_set() and server.should_exit


def test_plist_has_no_stop_timeout_key():
    data = plistlib.loads(launchd.render_plist(["/x/myvec", "serve"]))
    assert "TimeoutStopSec" not in data and "ExitTimeOut" not in data


# ------------------------------------------------------------------ scheduler (fake clock) / watcher filter

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_scheduler_debounce():
    c = Clock()
    s = Scheduler(debounce=10, safety=900, clock=c)
    assert s.set_active({"a", "b"}) == (["a", "b"], [])
    assert s.due() == ["a", "b"]                                  # newly active: immediate check
    for k in ("a", "b"):
        s.started(k)
        s.finished(k)
    assert s.due() == []
    s.notify("a")
    c.t += 9
    assert s.due() == []
    s.notify("a")                                                 # another event: debounce restarts
    c.t += 9.9
    assert s.due() == []
    c.t += 0.2
    assert s.due() == ["a"]
    s.started("a")
    assert s.due() == []                                          # never two syncs of one project
    s.notify("a")                                                 # event while syncing
    s.finished("a")
    assert s.due() == []
    c.t += 11
    assert s.due() == ["a"]


def test_scheduler_safety_net_and_inactive_projects():
    c = Clock()
    s = Scheduler(debounce=10, safety=900, clock=c)
    s.set_active({"a"})
    s.started("a"); s.finished("a")
    c.t += 899
    assert s.due() == []
    c.t += 2
    assert s.due() == ["a"]
    s.started("a"); s.finished("a")
    assert s.due() == []
    s.notify("ghost")                                             # events for unknown projects are ignored
    assert s.due() == []
    assert s.set_active(set()) == ([], ["a"]) and s.due() == [] and s.active == set()
    s.notify("a")
    assert s.due() == []
    assert s.set_active({"a"}) == (["a"], [])                     # re-activation: immediate check again
    assert s.due() == ["a"]


def test_scheduler_retry_when_lock_busy():
    c = Clock()
    s = Scheduler(debounce=10, safety=900, clock=c)
    s.set_active({"a"})
    s.started("a")
    s.finished("a", retry=True)
    assert s.due() == []
    c.t += 10
    assert s.due() == ["a"]


def test_events_attributed_to_deepest_active_project(state, ws):
    child = str(ws / "child")
    state._roots = {"acme": str(ws), "acme/child": child}
    state.sched.set_active({"acme", "acme/child"})
    for k in ("acme", "acme/child"):
        state.sched.started(k); state.sched.finished(k)
    state._on_fs_event("acme", child + "/c.py")
    assert state.sched._p["acme/child"].dirty and not state.sched._p["acme"].dirty
    state._on_fs_event("acme", str(ws / "site/a.py"))
    assert state.sched._p["acme"].dirty
    state._on_fs_event("acme", "/elsewhere/x.py")                 # outside every root: ignored


def test_event_filter(ws):
    write(ws / "site/.gitignore", "build_out/\n*.log\n")
    f = EventFilter.for_project(ws)
    ok = [ws / "site/src/Invoice.php", ws / "site/.git/HEAD", ws / "site/.git/index", ws / "README.md",
          ws / "site/.gitignore", ws / "newdir/file.py"]
    bad = [ws / "site/.git/objects/ab/cdef", ws / "site/.git/index.lock", ws / "site/.git/refs/heads/main",
           ws / "site/node_modules/x/y.js", ws / "site/vendor/a.php", ws / "site/build_out/a.js",
           ws / "site/app.log", ws / "site/logo.png", ws / "site/.env", ws / "site/.idea/x.xml",
           ws / "site/composer.lock", ws.parent / "outside.py", ws]
    for p in ok:
        assert f.relevant(p), p
    for p in bad:
        assert not f.relevant(p), p
    assert f.relevant(ws / "libs/anything.log")               # libs has no .gitignore: .log is fine there


def test_event_filter_reloads_gitignore(ws):
    f = EventFilter.for_project(ws)
    assert f.relevant(ws / "site/gen/a.py")
    write(ws / "site/.gitignore", "gen/\n")
    assert f.relevant(ws / "site/.gitignore")                  # the event itself reloads the rules
    assert not f.relevant(ws / "site/gen/a.py")


def test_real_watchdog_smoke(ws, tmp_path):
    """A single real FSEvents/inotify round trip (generous timeout); the logic is covered above."""
    from myvec.watcher import WatchManager
    got = []
    wm = WatchManager(lambda k, p: got.append((k, p)))
    wm.start("acme", ws)
    try:
        time.sleep(0.5)
        write(ws / "site/src/watched_new.py", "x = 1\n")
        for _ in range(60):
            if any("watched_new.py" in p for _, p in got):
                break
            time.sleep(0.25)
        assert any("watched_new.py" in p for _, p in got)
        # One write yields created+modified; under load the second event can arrive later.
        # Wait until the stream settles, then check that the ignored dir adds nothing.
        prev = -1
        for _ in range(20):
            if len(got) == prev:
                break
            prev = len(got)
            time.sleep(0.5)
        write(ws / "site/node_modules/pkg/index.js", "x\n")
        time.sleep(1.0)
        assert not any("node_modules" in p for _, p in got), got
        assert wm.watching == {"acme"}
    finally:
        wm.stop_all()


# ------------------------------------------------------------------ launchd

def test_plist_rendering(tmp_path):
    data = plistlib.loads(launchd.render_plist(["/home/u/.local/bin/myvec", "serve"], ["/home/u/.local/bin", "/opt/homebrew/bin"]))
    assert data["Label"] == "com.myvec.serve" and data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["ProgramArguments"] == ["/home/u/.local/bin/myvec", "serve"]
    path = data["EnvironmentVariables"]["PATH"].split(":")
    assert path[0] == "/home/u/.local/bin" and "/opt/homebrew/bin" in path and len(path) == len(set(path))
    logs = str(config.logs_dir())
    assert data["StandardOutPath"].startswith(logs) and data["StandardErrorPath"].startswith(logs)
    assert data["EnvironmentVariables"]["MYVEC_HOME"] == str(config.home())


def test_default_plist_path_includes_uv_tool_bin(monkeypatch):
    monkeypatch.setattr(launchd, "uv_tool_bin", lambda: "/custom/uv/bin")
    data = plistlib.loads(launchd.render_plist(["/x/myvec", "serve"]))
    assert "/custom/uv/bin" in data["EnvironmentVariables"]["PATH"].split(":")


def test_install_and_uninstall_never_call_launchctl_in_tests(tmp_path, monkeypatch):
    import subprocess
    real_run = subprocess.run

    def guard(cmd, *a, **k):
        assert cmd[0] != "launchctl", cmd
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", guard)
    monkeypatch.setattr(sys, "platform", "darwin")                                # the launchd path on any OS
    agents = tmp_path / "LaunchAgents"
    assert launchd.plist_path() == agents / "com.myvec.serve.plist"
    r = runner.invoke(cli_app, ["install-daemon"])
    assert r.exit_code == 0, r.output
    assert (agents / "com.myvec.serve.plist").is_file() and "loaded" not in r.stdout
    assert plistlib.loads((agents / "com.myvec.serve.plist").read_bytes())["Label"] == "com.myvec.serve"
    r = runner.invoke(cli_app, ["uninstall-daemon"])
    assert r.exit_code == 0 and "removed" in r.stdout and not (agents / "com.myvec.serve.plist").exists()
    assert "not installed" in runner.invoke(cli_app, ["uninstall-daemon"]).stdout
    assert not (tmp_path / "userhome/Library").exists()                          # the real location is untouched


# ------------------------------------------------------------------ systemd

def test_unit_rendering():
    text = systemd.render_unit(["/home/u/.local/bin/myvec", "serve"], ["/home/u/.local/bin", "/usr/bin", "/home/u/.local/bin"])
    lines = text.splitlines()
    assert "ExecStart=/home/u/.local/bin/myvec serve" in lines
    assert "Restart=always" in lines and "RestartSec=5" in lines and "TimeoutStopSec=15" in lines and "WantedBy=default.target" in lines
    assert "Environment=PATH=/home/u/.local/bin:/usr/bin" in lines
    logs = config.logs_dir()
    assert f"StandardOutput=append:{logs / 'daemon.out.log'}" in lines
    assert f"StandardError=append:{logs / 'daemon.err.log'}" in lines
    assert f"Environment=MYVEC_HOME={config.home()}" in lines
    assert 'ExecStart="/opt/my dir/myvec" serve' in systemd.render_unit(["/opt/my dir/myvec", "serve"], []).splitlines()


def test_default_unit_path_includes_uv_tool_bin(monkeypatch):
    monkeypatch.setattr(systemd, "uv_tool_bin", lambda: "/custom/uv/bin")
    assert "/custom/uv/bin" in systemd.render_unit(["/x/myvec", "serve"]).split("PATH=")[1].split("\n")[0].split(":")


def test_systemd_install_and_uninstall_never_call_systemctl_in_tests(tmp_path, monkeypatch):
    import subprocess
    import sys
    real_run = subprocess.run

    def guard(cmd, *a, **k):
        assert cmd[0] not in ("systemctl", "launchctl"), cmd
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", guard)
    monkeypatch.setattr(sys, "platform", "linux")
    unit = tmp_path / "systemd" / "myvec.service"
    assert systemd.unit_path() == unit
    r = runner.invoke(cli_app, ["install-daemon"])
    assert r.exit_code == 0, r.output
    assert unit.is_file() and "loaded" not in r.stdout and "ExecStart=" in unit.read_text()
    r = runner.invoke(cli_app, ["uninstall-daemon"])
    assert r.exit_code == 0 and "removed" in r.stdout and not unit.exists()
    assert "not installed" in runner.invoke(cli_app, ["uninstall-daemon"]).stdout
    assert not (tmp_path / "userhome/.config/systemd").exists()                  # the real location is untouched


def test_systemd_install_calls_systemctl_in_order(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.delenv("MYVEC_SYSTEMD_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    path, loaded = systemd.install()
    assert loaded and path == tmp_path / "xdg/systemd/user/myvec.service" and path.is_file()
    assert [c[2:] for c in calls if c[0] == "systemctl"] == [["daemon-reload"], ["enable", "--now", "myvec.service"], ["restart", "myvec.service"]]
    calls.clear()
    systemd.uninstall()
    assert [c[2:] for c in calls if c[0] == "systemctl"] == [["disable", "--now", "myvec.service"], ["daemon-reload"]] and not path.exists()


def test_watcher_inotify_mask_excludes_reads(tmp_path, monkeypatch):
    """Reads must not reach inotify at all (IN_OPEN/IN_CLOSE_NOWRITE flooded the queue on Linux); writes still do."""
    import sys
    import time

    from myvec import watcher

    if sys.platform != "linux":
        pytest.skip("inotify only")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    seen = []
    w = watcher.WatchManager(lambda key, path: seen.append(path))
    w.start("p", root)
    try:
        time.sleep(0.3)
        emitter = next(iter(w._obs().emitters))
        from watchdog.observers.inotify_c import InotifyConstants as C
        mask = emitter.get_event_mask_from_filter()
        assert not mask & C.IN_OPEN and not mask & C.IN_CLOSE_NOWRITE and not mask & C.IN_ACCESS
        assert mask & C.IN_MODIFY and mask & C.IN_CREATE and mask & C.IN_DELETE and mask & C.IN_MOVE
        (root / "b.py").write_text("y = 2\n")
        for _ in range(30):
            if seen:
                break
            time.sleep(0.1)
        assert any(p.endswith("b.py") for p in seen)
    finally:
        w.stop_all()


# ------------------------------------------------------------------ node load (ADR-012)

def test_load_endpoints_and_status(client, state):
    assert client.get("/api/load").status_code == 503     # no sampler thread without `background`
    assert state.load is None and state.status()["global"]["load"]["enabled"] is False

    class FakeLoad:
        def current(self): return {"enabled": True, "snapshot": {"cpu_pct": 7.0}}
        def series(self, period): return {"period": period, "points": []}
        def projects(self, minutes): return {"minutes": minutes, "projects": []}
        def status(self): return {"enabled": True, "interval_s": 5.0, "sampler_cpu_pct": 0.2, "last_ts": 1.0}
        def set_roots(self, roots): pass

    state.load = FakeLoad()
    assert client.get("/api/load").json()["snapshot"]["cpu_pct"] == 7.0
    assert client.get("/api/load/series?period=30d").json()["period"] == "30d"
    assert client.get("/api/load/series?period=7d").status_code == 400
    assert client.get("/api/load/projects?minutes=5").json()["minutes"] == 5.0
    assert client.get("/api/status").json()["global"]["load"]["sampler_cpu_pct"] == 0.2


def _fake_runner_cls():
    from myvec import nodeload

    class FakeSrc:
        def system(self):
            return {"cores": [1.0], "cpu_pct": 1.0, "load": (0, 0, 0), "mem_used": 1, "mem_total": 2,
                    "swap_used": 0, "freq_mhz": None, "temp_c": None}

        def procs(self):
            return []

        def host_info(self):
            return {"hostname": "h", "cpu_model": "", "cores": 1, "ram_total": 2}

    return nodeload, FakeSrc


def test_lifespan_starts_and_stops_sampler(ws, monkeypatch):
    nodeload, FakeSrc = _fake_runner_cls()
    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": FakeSrc())
    st = daemon.DaemonState(PORT, TOKEN, background=True)
    try:
        with TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}") as c:
            assert st.load is not None and st.load._thread.is_alive()
            assert c.get("/api/status").json()["global"]["load"]["enabled"] is True
            thread = st.load._thread
        assert not thread.is_alive()
    finally:
        st.close()


def test_load_disabled_and_no_psutil(ws, monkeypatch):
    nodeload, _ = _fake_runner_cls()
    st = daemon.DaemonState(PORT, TOKEN)
    cfg = config.load_config()
    cfg.load_enabled = False
    st.start_load(cfg)
    assert st.load is None
    cfg.load_enabled = True

    def no_psutil(sensor=""):
        raise ImportError("psutil")

    monkeypatch.setattr(nodeload, "SystemSource", no_psutil)
    st.start_load(cfg)
    assert st.load is None and st.load_status()["enabled"] is False
    st.close()


# ------------------------------------------------------------------ test runs API (E6-05)

def _add_run(st, uid, started, project="acme", **kw):
    row = dict(run_uid=uid, source="wrapper", state="done", host=__import__("socket").gethostname(), pid=1, project_path=project, cwd="/secret",
               session_id="s", command="pytest -q", started_at=started, ended_at=started + 5, wall_s=5.0, cpu_s=9.0,
               exit_code=0, interrupted=0)
    row.update(kw)
    with st.db_lock:
        st.db().execute(f"INSERT INTO test_runs({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


def test_tests_runs_api(client, state):
    assert client.get("/api/tests/runs").json()["runs"] == []
    now = time.time()
    _add_run(state, "a", now - 100)
    _add_run(state, "b", now - 50, exit_code=1)
    _add_run(state, "c", now - 10, state="running", ended_at=None, wall_s=None, exit_code=None)
    r = client.get("/api/tests/runs?project=acme&limit=2")
    assert r.status_code == 200
    d = r.json()
    assert [x["run_uid"] for x in d["runs"]] == ["c", "b"]
    assert d["runs"][0]["outcome"] == "running" and d["runs"][1]["outcome"] == "failed" and d["runs"][1]["regression"] is None
    assert "cwd" not in d["runs"][0] and "session_id" not in d["runs"][0]
    assert d["summary"]["n"] == 3
    assert [x["run_uid"] for x in client.get(f"/api/tests/runs?before={now - 60}").json()["runs"]] == ["a"]
    assert "summary" not in client.get("/api/tests/runs").json()


def test_tests_runs_errors(client):
    assert client.get("/api/tests/runs?project=nope").status_code == 404
    for bad in ("0", "201", "x"):
        assert client.get(f"/api/tests/runs?limit={bad}").status_code == 400
    for bad in ("abc", "nan", "inf"):
        assert client.get(f"/api/tests/runs?before={bad}").status_code == 400


def test_tests_runs_project_with_slash(client, state, ws):
    write(ws / "child" / "c.py", "def child_fn_zz():\n    return 1\n")
    write(ws / "child" / ".myvector.toml", 'name = "child"\n')
    do_sync(ws)
    state.refresh()
    _add_run(state, "k", time.time(), project="acme/child")
    d = client.get("/api/tests/runs?project=acme%2Fchild").json()
    assert [x["run_uid"] for x in d["runs"]] == ["k"]


def test_tests_current(client, state):
    now = time.time()
    _add_run(state, "old", now - 3 * 86400)
    _add_run(state, "fresh", now - 60)
    _add_run(state, "live", now - 5, state="running", ended_at=None, wall_s=None, exit_code=None)
    d = client.get("/api/tests/current").json()
    assert [x["run_uid"] for x in d["running"]] == ["live"]
    assert [x["run_uid"] for x in d["recent"]] == ["fresh"]


# ------------------------------------------------------------------ alerts and regressions API (E6-06, ADR-018)

class AlertLoad:
    def __init__(self, project="acme"):
        self.project = project

    def _alert(self):
        return {"since": 1.0, "minutes": 7.2, "pct": 85.0, "project": self.project, "top_name": "php", "top_pid": 5, "cores": 6.1}

    def current(self): return {"enabled": True, "snapshot": {"cpu_pct": 91.0}, "alert": self._alert(), "alert_rule": {"pct": 85.0, "minutes": 5.0}}
    def status(self): return {"enabled": True, "interval_s": 5.0, "sampler_cpu_pct": 0.2, "last_ts": 1.0, "alert": self._alert(),
                              "alert_rule": {"pct": 85.0, "minutes": 5.0}}
    def set_roots(self, roots): pass


def test_alert_run_is_attached_and_masked_in_load_and_status(client, state):
    state.load = AlertLoad()
    secret = "API_TOKEN=abcdef1234567890SECRETVALUE"
    _add_run(state, "live", time.time() - 5, state="running", ended_at=None, wall_s=None, exit_code=None,
             command=f"php artisan test {secret}", label="full", ticket_id="E1-14", agent="tester")
    for a in (client.get("/api/load").json()["alert"], client.get("/api/status").json()["global"]["load"]["alert"]):
        assert a["project"] == "acme" and a["cores"] == 6.1 and a["top_name"] == "php"
        run = a["run"]
        assert run["run_uid"] == "live" and run["ticket_id"] == "E1-14" and run["agent"] == "tester" and run["label"] == "full"
        assert "SECRETVALUE" not in run["command"] and run["command"].startswith("php artisan test")
        assert set(run) == {"run_uid", "command", "label", "ticket_id", "agent"}


def test_alert_run_null_without_running_test_of_that_project(client, state):
    state.load = AlertLoad()
    assert client.get("/api/load").json()["alert"]["run"] is None
    _add_run(state, "other", time.time() - 5, project="beta", state="running", ended_at=None, wall_s=None, exit_code=None)
    _add_run(state, "done", time.time() - 50)
    assert client.get("/api/status").json()["global"]["load"]["alert"]["run"] is None


def test_alert_without_project_has_no_run_lookup(client, state):
    state.load = AlertLoad(project=None)
    _add_run(state, "live", time.time() - 5, state="running", ended_at=None, wall_s=None, exit_code=None)
    a = client.get("/api/load").json()["alert"]
    assert a["project"] is None and "run" not in a


def test_no_alert_leaves_load_unchanged(client, state):
    class Calm(AlertLoad):
        def current(self): return {"enabled": True, "snapshot": {"cpu_pct": 7.0}, "alert": None, "alert_rule": {"pct": 85.0, "minutes": 5.0}}
    state.load = Calm()
    d = client.get("/api/load").json()
    assert d["alert"] is None and d["alert_rule"] == {"pct": 85.0, "minutes": 5.0}


def test_start_load_passes_thresholds_from_config(ws, monkeypatch):
    nodeload, FakeSrc = _fake_runner_cls()
    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": FakeSrc())
    (config.home() / "config.toml").write_text("load_alert_pct = 60\nload_alert_minutes = 2\n")
    st = daemon.DaemonState(PORT, TOKEN)
    try:
        st.start_load(config.load_config())
        assert (st.load.alert_pct, st.load.alert_minutes) == (60.0, 2.0)
        assert st.load_status()["alert_rule"] == {"pct": 60.0, "minutes": 2.0}
    finally:
        st.close()


def test_lifespan_background_with_alerts_has_single_sampler_thread(ws, monkeypatch):
    nodeload, FakeSrc = _fake_runner_cls()
    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": FakeSrc())
    st = daemon.DaemonState(PORT, TOKEN, background=True)
    try:
        with TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}") as c:
            d = c.get("/api/status").json()["global"]["load"]
            assert d["alert"] is None and d["alert_rule"] == {"pct": 85.0, "minutes": 5.0}
            assert st.load._thread.is_alive()
        assert not st.load._thread.is_alive()
    finally:
        st.close()


def _seed_regression(st, n=3):
    now = time.time()
    for i in range(n):
        _add_run(st, f"base{i}", now - 1000 + i, label="full", wall_s=100.0, cpu_s=200.0)
    return now


def test_tests_runs_api_has_regression_and_rule(client, state):
    now = _seed_regression(state)
    _add_run(state, "slow", now - 10, label="full", wall_s=300.0, cpu_s=200.0)
    d = client.get("/api/tests/runs?project=acme").json()
    assert d["regression_rule"] == {"pct": 25.0, "runs": 10, "min_runs": 3, "min_abs_s": 2.0}
    got = {x["run_uid"]: x["regression"] for x in d["runs"]}
    assert got["slow"]["wall_pct"] == 200 and got["base0"] is None


def test_tests_runs_api_rule_follows_config(client, state):
    now = _seed_regression(state)
    _add_run(state, "mid", now - 10, label="full", wall_s=140.0, cpu_s=200.0)
    assert client.get("/api/tests/runs").json()["runs"][0]["regression"]["wall_pct"] == 40
    (config.home() / "config.toml").write_text("test_regression_pct = 50\ntest_regression_min_runs = 5\n")
    d = client.get("/api/tests/runs").json()
    assert d["regression_rule"]["pct"] == 50.0 and d["regression_rule"]["min_runs"] == 5
    assert d["runs"][0]["regression"] is None


def test_tests_current_has_regression_for_running_and_rule(client, state):
    now = _seed_regression(state)
    _add_run(state, "live", now - 400, label="full", state="running", ended_at=None, wall_s=None, cpu_s=None, exit_code=None)
    d = client.get("/api/tests/current").json()
    assert d["regression_rule"]["runs"] == 10
    assert d["running"][0]["regression"]["wall_pct"] >= 300
