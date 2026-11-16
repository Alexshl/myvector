"""Persistent search log (ADR-029): no query text on disk, one record per search, three paths."""
import json
import os
import stat

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from conftest import do_sync
from myvec import config, daemon, daemon_client, searchcmd, searchlog
from myvec.bus import EventBus
from myvec.commands import app as cli_app

runner = CliRunner()
PORT = 7878
TOKEN = "test-token"
H = {"X-CSRF-Token": TOKEN}
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
HARD = "AKIAZQXSENTINEL12345"                 # a well-known token format: hard secret
LITERAL = "ZQXliteralvalue99"
HARD_Q = f"find {HARD} usage"
LIT_Q = f'password = "{LITERAL}" config'


def raw_files():
    out = {}
    for p in config.home().rglob("*"):
        if p.is_file():
            out[str(p)] = p.read_bytes()
    return out


def lines(p):
    return [json.loads(x) for x in p.read_text().splitlines()]


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


# ------------------------------------------------------------------ shape

def test_shape_has_no_query_text():
    r = searchlog.shape("where is the invoice total", via="direct", project="/p", results=3, ms=1.5)
    assert "invoice" not in json.dumps(r) and "q" not in r
    assert (r["q_len"], r["q_words"], r["q_ident"], r["q_secret"]) == (26, 5, False, False)
    assert r["v"] == 1 and r["via"] == "direct" and r["project"] == "/p" and r["results"] == 3 and r["ms"] == 1.5
    assert len(r["q_hash"]) == 16 and int(r["q_hash"], 16) >= 0


def test_shape_single_token_is_ident():
    assert searchlog.shape("renderPdf")["q_ident"] is True
    assert searchlog.shape("two words")["q_ident"] is False


def test_shape_empty_query_has_no_hash():
    r = searchlog.shape("")
    assert r["q_hash"] is None and r["q_len"] == 0 and r["q_words"] == 0


def test_shape_hard_secret_has_no_hash_and_no_text():
    r = searchlog.shape(HARD_Q, via="daemon")
    assert r["q_secret"] is True and r["q_hash"] is None
    assert HARD not in json.dumps(r)


def test_hash_stable_and_normalized():
    a = searchlog.shape("Render   PDF")["q_hash"]
    assert a == searchlog.shape("render pdf")["q_hash"]
    assert a != searchlog.shape("render pdfs")["q_hash"]


def test_hash_depends_on_salt():
    a = searchlog.shape("renderPdf")["q_hash"]
    searchlog.salt_path().write_bytes(os.urandom(32))
    assert searchlog.shape("renderPdf")["q_hash"] != a


def test_salt_created_once_with_mode_0600():
    assert not searchlog.salt_path().exists()
    searchlog.shape("x y")
    p = searchlog.salt_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and len(p.read_bytes()) == 32
    first = p.read_bytes()
    searchlog.shape("z")
    assert p.read_bytes() == first


@pytest.mark.parametrize("sid,ok", [(SID, True), (SID.upper(), True), ("not-a-uuid", False), ("", False),
                                    (SID + "x", False), ("../../etc/passwd", False), (None, False), (123, False)])
def test_session_only_when_uuid(sid, ok):
    assert searchlog.shape("q", session=sid)["session"] == (sid if ok else None)


def test_unknown_via_dropped():
    assert searchlog.shape("q", via="bogus")["via"] is None


# ------------------------------------------------------------------ mask_q

def test_mask_q():
    assert searchlog.mask_q(HARD_Q) == "•••"
    m = searchlog.mask_q(LIT_Q)
    assert LITERAL not in m and "•••" in m and "config" in m
    assert searchlog.mask_q("plain words") == "plain words" and searchlog.mask_q("") == ""


# ------------------------------------------------------------------ record / read

def test_record_file_mode_and_read_roundtrip():
    searchlog.record(searchlog.shape("a b", ts=100.0, via="ui"))
    searchlog.record(searchlog.shape("c", ts=200.0, via="direct"))
    assert stat.S_IMODE(searchlog.path().stat().st_mode) == 0o600
    assert [r["ts"] for r in searchlog.read(0)] == [100.0, 200.0]
    assert [r["ts"] for r in searchlog.read(150)] == [200.0]
    assert [r["ts"] for r in searchlog.read(0, 150)] == [100.0]


def test_read_skips_broken_lines_and_missing_files():
    assert searchlog.read(0) == []
    searchlog.record(searchlog.shape("a", ts=5.0))
    with open(searchlog.path(), "a") as f:
        f.write("{broken\n[1]\n" + json.dumps({"no_ts": 1}) + "\n")
    assert [r["ts"] for r in searchlog.read(0)] == [5.0]


def test_rotation_into_dot_one_and_read_spans_both(monkeypatch):
    monkeypatch.setattr(searchlog, "MAX_BYTES", 600)
    for i in range(12):
        searchlog.record(searchlog.shape("q", ts=float(i + 1), via="direct"))
    p = searchlog.path()
    assert p.with_name(p.name + ".1").exists()
    assert [r["ts"] for r in searchlog.read(0)] == sorted(r["ts"] for r in searchlog.read(0))
    assert len(searchlog.read(0)) >= 6


def test_mode_stays_0600_after_rotation(monkeypatch):
    monkeypatch.setattr(searchlog, "MAX_BYTES", 600)
    old = os.umask(0o022)
    try:
        for i in range(12):
            searchlog.record(searchlog.shape("q", ts=float(i + 1), via="direct"))
    finally:
        os.umask(old)
    assert searchlog.path().with_name("searches.jsonl.1").exists()
    assert stat.S_IMODE(searchlog.path().stat().st_mode) == 0o600


def test_short_salt_file_is_regenerated():
    p = searchlog.salt_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    h = searchlog.shape("hello")["q_hash"]
    assert len(p.read_bytes()) == 32 and p.read_bytes() != b"\0" * 32
    assert searchlog.shape("hello")["q_hash"] == h


def test_record_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(searchlog.logutil, "append", boom)
    searchlog.record({"ts": 1})


def test_publish_masks_bus_event_and_records_without_text(tmp_path):
    bus = EventBus(config.logs_dir() / "events.jsonl")
    searchlog.publish(bus, session=SID, q=LIT_Q, scope="project", via="daemon", results=2, ms=3.0, project="/p")
    searchlog.publish(bus, session=None, q=HARD_Q, via="daemon", results=0)
    evs = [e for e in bus.recent() if e["type"] == "search"]
    assert evs[0]["q"] != LIT_Q and LITERAL not in evs[0]["q"] and evs[1]["q"] == "•••"
    recs = searchlog.read(0)
    assert len(recs) == 2 and recs[0]["session"] == SID and recs[0]["results"] == 2
    assert recs[1]["q_secret"] is True and recs[1]["q_hash"] is None
    for blob in raw_files().values():
        assert LITERAL.encode() not in blob and HARD.encode() not in blob


def test_publish_log_via_overrides_record_only():
    bus = EventBus(None)
    searchlog.publish(bus, session=None, q="x y", via="daemon", log_via="ui", results=1)
    assert [e["via"] for e in bus.recent() if e["type"] == "search"] == ["daemon"]
    assert searchlog.read(0)[0]["via"] == "ui"


def test_publish_survives_record_failure(monkeypatch):
    monkeypatch.setattr(searchlog, "shape", lambda *a, **k: 1 / 0)
    bus = EventBus(None)
    searchlog.publish(bus, session=None, q="x", via="daemon")
    assert [e for e in bus.recent() if e["type"] == "search"]


# ------------------------------------------------------------------ three paths, one record each

def test_daemon_cli_path_one_record_with_session(client, ws):
    r = client.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site")},
                    headers={**H, "X-Myvec-Session": SID})
    assert r.status_code == 200
    recs = searchlog.read(0)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["via"] == "daemon" and rec["session"] == SID and rec["project"] == "acme"
    assert rec["results"] > 0 and rec["ms"] >= 0 and "renderPdf" not in json.dumps(rec)
    assert not (config.logs_dir() / "queries.jsonl").exists()


def test_daemon_cli_path_invalid_session_header_dropped(client, ws):
    client.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site")},
                headers={**H, "X-Myvec-Session": "evil\"; drop"})
    assert searchlog.read(0)[0]["session"] is None


def test_ui_path_one_record_via_ui(client, ws):
    assert client.get("/api/search", params={"q": "renderPdf", "cwd": str(ws / "site")}).status_code == 200
    recs = searchlog.read(0)
    assert len(recs) == 1 and recs[0]["via"] == "ui" and recs[0]["session"] is None
    client.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H)
    assert [r["via"] for r in searchlog.read(0)] == ["ui", "ui"]


def test_direct_path_one_record_not_duplicated_by_tail(client, state, ws, monkeypatch):
    monkeypatch.chdir(ws / "site")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    state._queries_pos = state._queries_size()
    assert runner.invoke(cli_app, ["search", "renderPdf"]).exit_code == 0
    state.tick()
    state.tick()
    recs = searchlog.read(0)
    assert len(recs) == 1
    r = recs[0]
    assert r["via"] == "direct" and r["session"] == SID and r["results"] > 0 and r["ms"] >= 0
    assert r["project"] == "acme" and r["scope"] == "project"
    assert any(e["type"] == "search" and e["via"] == "direct" for e in state.bus.recent())


def test_direct_path_without_session_env(ws, monkeypatch):
    monkeypatch.chdir(ws / "site")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    do_sync(ws)
    assert runner.invoke(cli_app, ["search", "renderPdf"]).exit_code == 0
    assert searchlog.read(0)[0]["session"] is None


def _fake_conn(monkeypatch, seen):
    import http.client

    class Resp:
        status = 200

        def read(self):
            return b"{}"

    class Conn:
        sock = type("S", (), {"settimeout": lambda self, t: None})()

        def __init__(self, *a, **k):
            pass

        def connect(self):
            pass

        def request(self, method, path, body=None, headers=None):
            seen.update(headers or {})

        def getresponse(self):
            return Resp()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)


def test_daemon_client_sends_session_header(monkeypatch):
    seen = {}
    _fake_conn(monkeypatch, seen)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert daemon_client.request("POST", "/api/cli/search", {"q": "x"}, info={"port": 1, "token": "t"}) == {}
    assert seen["X-Myvec-Session"] == SID and seen["X-CSRF-Token"] == "t"


def test_daemon_client_drops_malformed_session(monkeypatch):
    seen = {}
    _fake_conn(monkeypatch, seen)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc\nX-Evil: 1")
    daemon_client.request("POST", "/api/cli/search", {"q": "x"}, info={"port": 1, "token": "t"})
    assert "X-Myvec-Session" not in seen


def test_daemon_client_omits_session_header_without_env(monkeypatch):
    seen = {}
    _fake_conn(monkeypatch, seen)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    daemon_client.request("POST", "/api/cli/search", {"q": "x"}, info={"port": 1, "token": "t"})
    assert "X-Myvec-Session" not in seen


# ------------------------------------------------------------------ sentinel never on disk

def test_secret_sentinels_never_in_any_file_all_paths(client, state, ws, monkeypatch):
    cwd = str(ws / "site")
    for q in (HARD_Q, LIT_Q):
        client.post("/api/cli/search", json={"q": q, "cwd": cwd}, headers={**H, "X-Myvec-Session": SID})
        client.get("/api/search", params={"q": q, "cwd": cwd})
        client.post("/api/search", json={"q": q, "cwd": cwd}, headers=H)
    monkeypatch.chdir(ws / "site")
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    for q in (HARD_Q, LIT_Q):
        runner.invoke(cli_app, ["search", q])
    state.tick()
    blobs = raw_files()
    assert any(p.endswith("searches.jsonl") for p in blobs) and any(p.endswith("events.jsonl") for p in blobs)
    assert any(p.endswith("queries.jsonl") for p in blobs)
    for p, b in blobs.items():
        assert HARD.encode() not in b and LITERAL.encode() not in b, p
    recs = searchlog.read(0)
    assert len(recs) == 8
    hard = [r for r in recs if r["q_secret"]]
    assert len(hard) == 4 and all(r["q_hash"] is None for r in hard)
    q = [json.loads(x) for x in (config.logs_dir() / "queries.jsonl").read_text().splitlines()]
    assert sorted(x["q"] for x in q if x["q"] == "•••") == ["•••"]


# ------------------------------------------------------------------ E8-08: the route actually taken

MULTI_Q = "invoice total amount"


@pytest.fixture
def embed_down(monkeypatch):
    from myvec import embed

    def boom(self, text):
        raise embed.EmbedError("timeout")
    monkeypatch.setattr(embed.Embedder, "embed_query", boom)


def test_route_vector_with_working_embedder(client, ws):
    client.post("/api/cli/search", json={"q": MULTI_Q, "cwd": str(ws / "site")}, headers=H)
    assert searchlog.read(0)[0]["route"] == "vector"


def test_route_fts_fallback_daemon_cli(client, ws, embed_down):
    client.post("/api/cli/search", json={"q": MULTI_Q, "cwd": str(ws / "site")}, headers=H)
    recs = searchlog.read(0)
    assert len(recs) == 1 and recs[0]["route"] == "fts-fallback" and MULTI_Q not in json.dumps(recs[0])


def test_route_fts_fallback_ui(client, ws, embed_down):
    r = client.get("/api/search", params={"q": MULTI_Q, "cwd": str(ws / "site")})
    assert r.status_code == 200 and r.json()["route"] == "fts-fallback"
    recs = searchlog.read(0)
    assert recs[0]["route"] == "fts-fallback" and MULTI_Q not in json.dumps(recs[0])


def test_route_fts_fallback_direct(ws, monkeypatch, embed_down):
    monkeypatch.chdir(ws / "site")
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    do_sync(ws)
    assert runner.invoke(cli_app, ["search", MULTI_Q]).exit_code == 0
    recs = searchlog.read(0)
    assert recs[0]["route"] == "fts-fallback" and MULTI_Q not in json.dumps(recs[0])
    assert MULTI_Q not in (config.logs_dir() / "searches.jsonl").read_text()


def test_route_fts_fallback_by_project(ws, monkeypatch, embed_down):
    monkeypatch.chdir(ws / "site")
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    do_sync(ws)
    assert runner.invoke(cli_app, ["search", MULTI_Q, "--scope", "all", "--by-project"]).exit_code == 0
    assert searchlog.read(0)[0]["route"] == "fts-fallback"
