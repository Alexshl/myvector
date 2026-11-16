"""GET /api/search/usage (E8-06, ADR-037): shape, 400s, cache, read-only, privacy. Tmp MYVEC_HOME, no network."""
import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from myvec import config, daemon, searchlog, searchstats, tokens as T
from myvec import db as dbmod
from test_tool_calls import bash, put, tool

MARKER = "ZQXQUERYMARKERZQX"
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
KEYS = {"sessions", "sessions_myvec", "myvec", "self", "self_sub", "share", "miss"}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def mk():
    made = []

    def make():
        st = daemon.DaemonState(7878, "t")
        made.append(st)
        return st, TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878")

    yield make
    for st in made:
        st.executor.shutdown(wait=True)
        st.close()


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "acme"
    b.mkdir()
    return str(b)


@pytest.fixture
def seeded(tmp_path, base):
    """index.db with a project, tokens.db with myvec/grep calls an hour ago, one searches.jsonl record."""
    idx = dbmod.open_ready(config.load_config())
    idx.execute("INSERT INTO projects(path, root) VALUES(?, ?)", (base, base))
    idx.commit()
    idx.close()
    root = tmp_path / "claude_projects"
    root.mkdir()
    t0 = time.time() - 3600
    put(root, "pr/s1.jsonl",
        bash("a1", "myvec search foo", cwd=base, sid="s1", ts=iso(t0)),
        tool("a2", "Grep", {"pattern": "x"}, cwd=base, sid="s1", ts=iso(t0 + 5)),
        bash("a3", "grep y", cwd=base, sid="s2", ts=iso(t0 + 9)))
    tdb = T.open_db()
    T.scan(tdb, root)
    tdb.close()
    searchlog.record(searchlog.shape(f"how does {MARKER} work", session=SID, ts=t0, via="cli", route="hybrid",
                                     results=0, ms=120, project=base))
    return base


def test_ok_shape_and_default_days(mk, seeded):
    _, c = mk()
    r = c.get("/api/search/usage?days=7")
    assert r.status_code == 200
    d = r.json()
    assert d["days"] == 7 and set(d) >= {"days", "projects", "total", "collector"}
    assert set(d["collector"]) >= {"enabled", "pending_bytes", "backfill"}
    p = next(x for x in d["projects"] if x["project"] == seeded)
    assert KEYS <= set(p) and (p["myvec"], p["self"]) == (1, 2) and p["sessions"] == 1 and p["sessions_myvec"] == 1
    assert d["total"]["myvec"] == 1 and d["total"]["self"] == 2
    assert c.get("/api/search/usage").json()["days"] == 14


@pytest.mark.parametrize("q", ["days=5", "days=abc", "days=", "days=0", "days=-7", "days=14.0", "days=31"])
def test_bad_days_is_400(mk, q):
    _, c = mk()
    r = c.get(f"/api/search/usage?{q}")
    assert r.status_code == 400 and "days" in r.json()["error"]


def test_values_match_searchstats_usage(mk, seeded):
    _, c = mk()
    body = c.get("/api/search/usage?days=14").json()
    idx = dbmod.connect(vec=False, readonly=True)
    tdb = T.open_db(readonly=True)
    try:
        exp = searchstats.usage(idx, tdb, 14)
    finally:
        idx.close()
        tdb.close()
    assert body["projects"] == exp["projects"] and body["total"] == exp["total"] and body["collector"] == exp["collector"]


def test_response_has_no_query_text(mk, seeded):
    _, c = mk()
    r = c.get("/api/search/usage?days=14")
    assert MARKER not in r.text
    p = next(x for x in r.json()["projects"] if x["project"] == seeded)
    assert p["logged"] == 1 and p["zero_results"] == 1 and p["p50_ms"] == 120    # the record counted, text absent
    for k in ("q", "q_hash", "command"):
        assert f'"{k}"' not in r.text


def test_empty_home_creates_no_databases(mk):
    _, c = mk()
    r = c.get("/api/search/usage?days=30")
    assert r.status_code == 200 and r.json()["projects"] == []
    assert not config.db_path().exists() and not config.tokens_db_path().exists()


def test_cache_per_days_and_expiry(mk, monkeypatch):
    st, c = mk()
    calls = []
    real = searchstats.usage

    def counting(idx, tdb, days, now=None):
        calls.append(days)
        return real(idx, tdb, days, now)

    monkeypatch.setattr(searchstats, "usage", counting)
    clock = [1000.0]
    monkeypatch.setattr(daemon, "_usage_clock", lambda: clock[0])
    assert c.get("/api/search/usage?days=14").status_code == 200
    assert c.get("/api/search/usage?days=14").status_code == 200
    assert calls == [14]
    assert c.get("/api/search/usage?days=7").status_code == 200 and calls == [14, 7]
    clock[0] += daemon.USAGE_TTL_S - 1
    c.get("/api/search/usage?days=14")
    assert calls == [14, 7]
    clock[0] += 1
    c.get("/api/search/usage?days=14")
    assert calls == [14, 7, 14]
    monkeypatch.setattr(searchstats, "usage", real)


def test_errors_are_not_cached(mk, monkeypatch):
    import sqlite3

    _, c = mk()
    n = []

    def boom(idx, tdb, days, now=None):
        n.append(1)
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(searchstats, "usage", boom)
    assert c.get("/api/search/usage?days=14").status_code == 500
    assert c.get("/api/search/usage?days=14").status_code == 500
    assert len(n) == 2


def test_wrong_host_is_rejected(mk):
    st, _ = mk()
    c = TestClient(daemon.create_app(st), base_url="http://evil.example:7878")
    assert c.get("/api/search/usage?days=14").status_code in (400, 403)
