"""Token usage collection from Claude Code transcripts (ADR-013). Only synthetic transcripts in tmp."""
import fcntl
import json
import os
import time

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from myvec import config, daemon, relocate, tokens as T
from myvec import db as dbmod
from myvec.commands import app

runner = CliRunner()
SENTINEL = "ZQXSENTINELPHRASEZQX"
TS = "2026-11-09T10:00:00.000Z"


def rec(mid, req="r1", *, cwd="/p/acme", branch="main", inp=10, out=20, cr=100, cw=5, model="claude-sonnet-5-5",
        ts=TS, effort="medium", content=SENTINEL, extra=None):
    d = {"type": "assistant", "requestId": req, "requestedModel": "sonnet", "effort": effort, "cwd": cwd,
         "gitBranch": branch, "sessionId": "s", "timestamp": ts,
         "message": {"id": mid, "model": model, "content": [{"type": "text", "text": content}],
                     "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw,
                               "cache_creation": {"ephemeral_1h_input_tokens": 2}}}}
    d.update(extra or {})
    return (json.dumps(d) + "\n").encode()


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "claude_projects"
    r.mkdir()
    return r


@pytest.fixture
def tdb():
    db = T.open_db()
    yield db
    db.close()


def put(root, rel, *lines, mode="ab"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode) as f:
        for ln in lines:
            f.write(ln)
    return p


def n_rows(tdb):
    return tdb.execute("SELECT count(*) FROM token_usage").fetchone()[0]


def sums(tdb):
    r = tdb.execute("SELECT sum(input), sum(output), sum(cache_read), sum(cache_write) FROM token_usage").fetchone()
    return tuple(r)


def set_claude_dir(root):
    p = config.home() / "config.toml"
    p.write_text(p.read_text().replace('claude_projects_dir = ""', f'claude_projects_dir = "{root}"'))


# ------------------------------------------------------------------ parsing

def test_parse_line_whitelist_and_filters():
    r = T.parse_line(rec("m1", effort="high", extra={"perTurnEffort": "low"}))
    assert r["msg_id"] == "m1" and r["request_id"] == "r1" and r["effort"] == "low"
    assert (r["input"], r["output"], r["cache_read"], r["cache_write"], r["cache_write_1h"]) == (10, 20, 100, 5, 2)
    assert r["model"] == "claude-sonnet-5-5" and r["requested_model"] == "sonnet"
    assert r["ts"] > 0 and SENTINEL not in json.dumps(r)
    assert T.parse_line(rec("m2", model="<synthetic>")) is None
    assert T.parse_line(rec("m3", inp=0, out=0, cr=0, cw=0)) is None
    assert T.parse_line(b'{"type": "user", "usage": 1}\n') is None
    assert T.parse_line(b'{"type": "assistant", "usage": {broken\n') is None
    assert T.parse_line(b'{"type": "assistant"}\n') is None


def test_parse_meta(tmp_path):
    p = tmp_path / "agent-1.meta.json"
    p.write_text(json.dumps({"agentType": "coder", "description": f"E7-01 and E2-05 {SENTINEL} X-1",
                             "effort": "medium", "spawnDepth": 1}))
    m = T.parse_meta(p)
    assert m == {"agent_type": "coder", "desc_tickets": "E7-01 E2-05", "req_model": None, "req_effort": "medium",
                 "spawn_depth": 1}
    assert T.parse_meta(tmp_path / "none.json")["agent_type"] == "unknown"
    p.write_text("{broken")
    assert T.parse_meta(p)["agent_type"] == "unknown"


# ------------------------------------------------------------------ scanning

def test_dedup_within_file_and_across_files(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1"), rec("m1"), rec("m2", "r2"))
    put(root, "pr/s2.jsonl", rec("m1"), rec("m3", "r3"))   # fork repeats m1
    T.scan(tdb, root)
    assert n_rows(tdb) == 3
    assert sums(tdb) == (30, 60, 300, 15)
    T.scan(tdb, root)
    assert n_rows(tdb) == 3


def test_incremental_and_partial_tail(root, tdb):
    p = put(root, "pr/s1.jsonl", rec("m1"))
    st = T.scan(tdb, root)
    assert st.new_rows == 1 and st.files == 1
    # a half-written last line is left for the next pass
    full = rec("m2", "r2")
    with open(p, "ab") as f:
        f.write(full[:40])
    assert T.scan(tdb, root).new_rows == 0 and n_rows(tdb) == 1
    with open(p, "ab") as f:
        f.write(full[40:])
        f.write(rec("m3", "r3"))
    assert T.scan(tdb, root).new_rows == 2 and n_rows(tdb) == 3
    off = tdb.execute('SELECT "offset", size FROM token_files').fetchone()
    assert off[0] == off[1] == p.stat().st_size
    assert T.scan(tdb, root).bytes_read == 0


def test_rotation_truncation_gone(root, tdb):
    p = put(root, "pr/s1.jsonl", rec("m1"), rec("m2", "r2"))
    T.scan(tdb, root)
    # truncation: smaller file from scratch, old rows stay, no duplicates
    p.write_bytes(rec("m1"))
    T.scan(tdb, root)
    assert n_rows(tdb) == 2
    # rotation: new inode with an old and a new request
    os.remove(p)
    put(root, "pr/s1.jsonl", rec("m1"), rec("m2", "r2"), rec("m9", "r9"))
    T.scan(tdb, root)
    assert n_rows(tdb) == 3
    # disappearing file keeps its rows
    os.remove(p)
    T.scan(tdb, root)
    assert n_rows(tdb) == 3
    assert tdb.execute("SELECT gone FROM token_files").fetchone()[0] == 1
    put(root, "pr/s1.jsonl", rec("m1"))
    T.scan(tdb, root)
    assert tdb.execute("SELECT gone FROM token_files").fetchone()[0] == 0


def test_budget_spreads_over_passes(root, tdb, monkeypatch):
    monkeypatch.setattr(T, "BLOCK", 600)
    lines = [rec(f"m{i}", f"r{i}") for i in range(30)]
    put(root, "pr/big.jsonl", *lines)
    size = (root / "pr/big.jsonl").stat().st_size
    passes = 0
    while True:
        st = T.scan(tdb, root, budget_bytes=1500)
        passes += 1
        if st.pending_bytes == 0:
            break
        assert passes < 50
    assert passes > 2 and n_rows(tdb) == 30 and sums(tdb) == (300, 600, 3000, 150)
    assert tdb.execute('SELECT "offset" FROM token_files').fetchone()[0] == size


def test_line_longer_than_block(root, tdb, monkeypatch):
    monkeypatch.setattr(T, "BLOCK", 100)
    put(root, "pr/s.jsonl", rec("m1"), rec("m2", "r2"))
    T.scan(tdb, root)
    assert n_rows(tdb) == 2


def test_skips_garbage_lines(root, tdb):
    put(root, "pr/s.jsonl", b"not json at all \"usage\"\n", rec("m1"), b'{"type":"user"}\n', rec("m2", model="<synthetic>"),
        rec("m3", "r3", inp=0, out=0, cr=0, cw=0))
    T.scan(tdb, root)
    assert n_rows(tdb) == 1


def test_subagents_meta_and_workflows(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1"))
    sub = "pr/s1/subagents"
    put(root, f"{sub}/agent-a1.jsonl", rec("a1"))
    (root / sub / "agent-a1.meta.json").write_text(json.dumps(
        {"agentType": "coder", "description": f"E7-01: {SENTINEL}", "effort": "medium"}))
    put(root, f"{sub}/workflows/wf_1/agent-w1.jsonl", rec("w1", "rw"))
    (root / sub / "workflows/wf_1/agent-w1.meta.json").write_text(json.dumps({"agentType": "reviewer", "model": "opus"}))
    put(root, f"{sub}/agent-nometa.jsonl", rec("n1", "rn"))
    put(root, f"{sub}/workflows/wf_1/journal.jsonl", rec("j1", "rj"))   # not an agent transcript
    T.scan(tdb, root)
    rows = {r["agent_id"]: dict(r) for r in tdb.execute("SELECT * FROM token_files")}
    assert rows[None]["agent_type"] == "main" and rows[None]["session_id"] == "s1"
    assert rows["a1"]["agent_type"] == "coder" and rows["a1"]["desc_tickets"] == "E7-01"
    assert rows["a1"]["session_id"] == "s1" and rows["a1"]["req_effort"] == "medium" and rows["a1"]["req_model"] is None
    assert rows["w1"]["agent_type"] == "reviewer" and rows["w1"]["req_model"] == "opus"
    assert rows["nometa"]["agent_type"] == "unknown"
    assert len(rows) == 4 and n_rows(tdb) == 4
    # a meta that appears later is picked up
    (root / sub / "agent-nometa.meta.json").write_text(json.dumps({"agentType": "tester"}))
    put(root, f"{sub}/agent-nometa.jsonl", rec("n2", "rn2"))
    T.scan(tdb, root)
    assert tdb.execute("SELECT agent_type FROM token_files WHERE agent_id='nometa'").fetchone()[0] == "tester"


def test_no_message_text_stored(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1", extra={"lastPrompt": SENTINEL, "toolUseResult": {"stdout": SENTINEL},
                                              "prompt": SENTINEL}),
        (json.dumps({"type": "user", "message": {"content": SENTINEL}}) + "\n").encode())
    put(root, "pr/s1/subagents/agent-a.jsonl", rec("a1", "ra", content=SENTINEL))
    (root / "pr/s1/subagents/agent-a.meta.json").write_text(
        json.dumps({"agentType": "coder", "description": f"{SENTINEL} E7-01 {SENTINEL}"}))
    T.scan(tdb, root)
    assert n_rows(tdb) == 2
    tdb.execute("PRAGMA wal_checkpoint(FULL)")
    for f in config.home().glob("tokens.db*"):
        assert SENTINEL.encode() not in f.read_bytes(), f.name
    cols = {r[1] for r in tdb.execute("PRAGMA table_info(token_usage)")}
    assert "text" not in cols and "content" not in cols and "description" not in cols


def test_lock_busy_skips_scan(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1"))
    p = config.tokens_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        st = T.scan(tdb, root)
        assert st.locked and n_rows(tdb) == 0
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
    assert not T.scan(tdb, root).locked and n_rows(tdb) == 1


def test_index_db_untouched(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1"))
    T.scan(tdb, root)
    assert not config.db_path().exists() or "token_usage" not in dbmod.connect(vec=False).execute(
        "SELECT group_concat(name) FROM sqlite_master").fetchone()[0]
    assert tdb.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(T.SCHEMA_VERSION)


# ------------------------------------------------------------------ attribution + read API

@pytest.fixture
def proj(tmp_path):
    """Project (path == root) with tickets E7-01, E2-05 in index.db."""
    base = tmp_path / "acme"
    base.mkdir()
    idx = dbmod.open_ready(config.load_config())
    idx.execute("INSERT INTO projects(path, root) VALUES(?, ?)", (str(base), str(base)))
    for tid in ("E7-01", "E2-05", "E1-06"):
        idx.execute("INSERT INTO tickets(project_path, id, title, status) VALUES(?, ?, 't', 'todo')", (str(base), tid))
    yield idx, str(base)
    idx.close()


def by(idx, tdb, *keys, **kw):
    return {tuple(r[k] for k in keys): r for r in T.usage(idx, tdb=tdb, by=keys, **kw)}


def test_attribution_order(root, tdb, proj):
    idx, base = proj
    wt = f"{base}/.worktrees/E1-06"
    # desc > claim/branch/worktree
    put(root, "pr/s1.jsonl", rec("m1", cwd=base, branch="main"))                      # main, no ticket
    put(root, "pr/s1/subagents/agent-d.jsonl", rec("d1", "rd", cwd=wt, branch="t/E1-06-x"))
    (root / "pr/s1/subagents/agent-d.meta.json").write_text(json.dumps({"agentType": "coder", "description": "E7-01 work"}))
    put(root, "pr/s2.jsonl", rec("b1", "rb", cwd=base, branch="t/E2-05-slug"))          # branch
    put(root, "pr/s3.jsonl", rec("w1", "rw", cwd=f"{wt}/src", branch="feature"))        # worktree (cwd deleted)
    put(root, "pr/s4.jsonl", rec("c1", "rc", cwd=base, branch="main"))                  # claim
    put(root, "pr/s5.jsonl", rec("u1", "ru", cwd=base, branch="t/ZZ-99-x"))             # unknown id
    put(root, "pr/s6.jsonl", rec("o1", "ro", cwd="/elsewhere", branch="main"))          # outside projects
    (config.home() / "state").mkdir(exist_ok=True)
    (config.home() / "state" / "ticket_claims.jsonl").write_text(
        json.dumps({"session_id": "s4", "ticket_id": "E7-01", "project_root": base}) + "\n")
    T.scan(tdb, root)
    r = by(idx, tdb, "ticket", "agent")
    assert r[("E7-01", "coder")]["requests"] == 1          # description beats branch t/E1-06
    assert r[("E2-05", "main")]["requests"] == 1           # branch
    assert r[("E1-06", "main")]["requests"] == 1           # worktree
    assert r[("E7-01", "main")]["requests"] == 1           # claim
    # main on main + unknown id (project acme), outside projects (no project)
    assert sum(x["requests"] for x in T.usage(idx, tdb=tdb, by=("ticket",)) if x["ticket"] == T.ORCH) == 3
    only_acme = by(idx, tdb, "ticket", project=base)
    assert set(k[0] for k in only_acme) == {"E7-01", "E2-05", "E1-06", T.ORCH}
    assert {x["agent"] for x in T.usage(idx, tdb=tdb, ticket="E7-01", by=("agent",))} == {"coder", "main"}
    tt = T.ticket_totals(idx, base, tdb=tdb)
    assert set(tt) == {"E7-01", "E2-05", "E1-06"} and tt["E7-01"]["totals"]["requests"] == 2
    assert {a["agent"] for a in tt["E7-01"]["by_agent"]} == {"coder", "main"}


def test_subagent_without_ticket_is_separate_bucket(root, tdb, proj):
    idx, base = proj
    put(root, "pr/s1/subagents/agent-x.jsonl", rec("x1", cwd=base))
    T.scan(tdb, root)
    rows = T.usage(idx, tdb=tdb, by=("ticket",))
    assert [(r["ticket"], r["project"]) for r in rows] == [(T.NO_TICKET, base)]


def test_usage_since_by_model_day_session(root, tdb, proj):
    idx, base = proj
    put(root, "pr/s1.jsonl", rec("m1", ts="2026-11-01T10:00:00Z", model="opus"),
        rec("m2", "r2", ts="2026-11-08T10:00:00Z", effort="high"))
    T.scan(tdb, root)
    assert len(T.usage(idx, tdb=tdb, by=("model",))) == 2
    since = T.parse_since("2026-11-05")
    rows = T.usage(idx, tdb=tdb, since=since, by=("effort",))
    assert [r["effort"] for r in rows] == ["high"] and rows[0]["total"] == 10 + 20 + 100 + 5
    assert 0 < rows[0]["cache_share"] < 1
    assert len({r["day"] for r in T.usage(idx, tdb=tdb, by=("day",))}) == 2
    assert len(T.usage(idx, tdb=tdb, session="s1", by=("session",))) == 1
    assert T.usage(idx, tdb=tdb, session="zzz", by=("session",)) == []
    assert T.parse_since("7d", now=1000 + 7 * 86400) == 1000
    with pytest.raises(ValueError):
        T.parse_since("yesterday")
    with pytest.raises(ValueError):
        T.usage(idx, tdb=tdb, by=("bogus",))


def test_cli_tokens(root, tdb, proj):
    idx, base = proj
    idx.close()
    set_claude_dir(root)
    put(root, "pr/s1.jsonl", rec("m1", cwd=base), rec("m2", "r2", cwd=base, ts="2026-01-01T00:00:00Z"))
    res = runner.invoke(app, ["tokens", "--by", "agent", "--project", base])
    assert res.exit_code == 0, res.output
    assert "оркестратор" in res.output and "усього" in res.output
    res = runner.invoke(app, ["tokens", "--by", "model,effort", "--since", "2026-06-01", "--json", "--no-scan"])
    data = json.loads(res.output)
    assert len(data) == 1 and data[0]["requests"] == 1 and data[0]["model"] == "claude-sonnet-5-5"
    res = runner.invoke(app, ["tokens", "--since", "oops"])
    assert res.exit_code == 1
    res = runner.invoke(app, ["tokens", "--by", "ticket", "--no-scan"])
    assert res.exit_code == 0 and T.ORCH in res.output
    from myvec import tokencli
    assert tokencli.compact(1234567) == "1,2 млн" and tokencli.compact(999) == "999" and tokencli.compact(12300) == "12,3 тис."


def test_relocate_rewrites_tokens_cwd(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1", cwd="/Users/me/p/acme"), rec("m2", "r2", cwd="/srv/other"))
    T.scan(tdb, root)
    idx = dbmod.open_ready(config.load_config())
    counts = relocate.relocate(idx, "/Users/me", "/home/me", dry_run=True)
    assert counts["tokens"] == 1
    assert {r[0] for r in tdb.execute("SELECT cwd FROM token_usage")} == {"/Users/me/p/acme", "/srv/other"}
    counts = relocate.relocate(idx, "/Users/me", "/home/me")
    assert counts["tokens"] == 1
    assert {r[0] for r in tdb.execute("SELECT cwd FROM token_usage")} == {"/home/me/p/acme", "/srv/other"}
    idx.close()


def test_config_defaults(tmp_path):
    cfg = config.load_config()
    assert cfg.tokens_enabled and cfg.tokens_scan_s == 60.0 and cfg.tokens_scan_budget_mb == 512
    assert config.claude_projects_root(cfg).name == "projects"
    cfg.claude_projects_dir = str(tmp_path / "x")
    assert config.claude_projects_root(cfg) == tmp_path / "x"


# ------------------------------------------------------------------ daemon

def test_daemon_foreground_does_not_scan(root):
    set_claude_dir(root)
    put(root, "pr/s1.jsonl", rec("m1"))
    st = daemon.DaemonState(7878, "t")
    try:
        st.tick()
        assert st._tokens_future is None and not config.tokens_db_path().exists()
    finally:
        st.close()


def test_lifespan_background_scans_and_stops(root, ws):
    set_claude_dir(root)
    put(root, "pr/s1.jsonl", rec("m1"), rec("m2", "r2"))
    st = daemon.DaemonState(7878, "t", background=True)
    try:
        with TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878") as c:
            deadline = time.time() + 15
            tok = {}
            while time.time() < deadline:
                tok = c.get("/api/status").json()["tokens"]
                if tok["requests"] == 2:
                    break
                time.sleep(0.2)
            assert tok["enabled"] is True and tok["requests"] == 2 and tok["files"] == 1
            assert tok["pending_bytes"] == 0 and tok["last_scan"]
    finally:
        st.close()


def test_daemon_scan_error_is_logged(root, monkeypatch):
    set_claude_dir(root)
    st = daemon.DaemonState(7878, "t", background=True)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(T, "scan", boom)
    try:
        st._tick_tokens()
        st._tokens_future.result(timeout=10)
        assert "tokens scan failed" in (config.logs_dir() / "sync.log").read_text()
        st._last_tokens_scan = float("-inf")
        st._tick_tokens()   # the daemon keeps going
    finally:
        st.close()


# ------------------------------------------------------------------ review fixes

def test_claude_root_order_and_tests_isolated(tmp_path, monkeypatch):
    assert "CLAUDE_CONFIG_DIR" not in os.environ          # conftest removes it: no test sees real transcripts
    root = config.claude_projects_root(config.load_config())
    assert str(root).startswith(str(tmp_path)) and root.name == "projects"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real"))
    assert config.claude_projects_root(config.load_config()) == tmp_path / "claude" / "projects"  # MYVEC_CLAUDE_DIR wins
    monkeypatch.delenv("MYVEC_CLAUDE_DIR")
    assert config.claude_projects_root(config.load_config()) == tmp_path / "real" / "projects"


def test_compact_rounding():
    from myvec import tokencli

    assert tokencli.compact(999_950) == "1,0 млн"
    assert tokencli.compact(999_949) == "999,9 тис."
    assert tokencli.compact(999_950_000) == "1,0 млрд"


def test_cli_empty_by_rejected(root):
    set_claude_dir(root)
    res = runner.invoke(app, ["tokens", "--by", "", "--no-scan"])
    assert res.exit_code == 1 and "--by" in res.output


def test_db_files_private(root, tdb):
    put(root, "pr/s1.jsonl", rec("m1"))
    T.scan(tdb, root)
    files = list(config.home().glob("tokens.db*"))
    assert any(f.name.endswith("-wal") for f in files)
    for f in files:
        assert f.stat().st_mode & 0o777 == 0o600, f.name


def test_status_before_first_scan():
    st = daemon.DaemonState(7878, "t")
    try:
        tok = st.status()["tokens"]
        assert tok["state"] == "ще не проскановано" and tok["requests"] is None
    finally:
        st.close()


def test_ticket_regex_ignores_adr_and_hashes(tmp_path):
    p = tmp_path / "agent-1.meta.json"
    p.write_text(json.dumps({"agentType": "coder", "description":
                             "ADR-013 abc9E7-01x deadBEEF-12 see (ZX-99) plan commit 3fE7-02 then E7-01, E2-05; ADR-014"}))
    # only bounded ids; real E<n>-<nn> ones go first, ADR dropped
    assert T.parse_meta(p)["desc_tickets"] == "E7-01 E2-05 ZX-99"


def test_duplicate_request_goes_to_earliest_record(root, tdb):
    a = put(root, "pr/a.jsonl", rec("m1", ts="2026-11-09T10:00:00Z", cwd="/early"))
    b = put(root, "pr/b.jsonl", rec("m1", ts="2026-11-09T11:00:00Z", cwd="/late"))
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))        # newer file is scanned first
    T.scan(tdb, root)
    assert n_rows(tdb) == 1
    assert tdb.execute("SELECT cwd FROM token_usage").fetchone()[0] == "/early"


def test_duplicate_request_tie_prefers_smaller_path(root, tdb):
    a = put(root, "pr/a.jsonl", rec("m1", cwd="/a"))
    z = put(root, "pr/z.jsonl", rec("m1", cwd="/z"))
    os.utime(a, (1000, 1000))
    os.utime(z, (2000, 2000))        # z scanned first, a must still win
    T.scan(tdb, root)
    assert tdb.execute("SELECT cwd FROM token_usage").fetchone()[0] == "/a"


def _two_runs(root, base):
    put(root, "pr/s1.jsonl", rec("m1", cwd=base, branch="t/E7-01-x", model="claude-opus-5-1", effort="high"))
    put(root, "pr/s1/subagents/agent-a.jsonl", rec("a1", "ra", cwd=base, model="claude-sonnet-5-5"),
        rec("a2", "rb", cwd=base, model="claude-sonnet-5-5", effort="medium"))
    put(root, "pr/s1/subagents/agent-b.jsonl", rec("b1", "rc", cwd=base, model="claude-sonnet-5-5"))
    for n in ("a", "b"):
        (root / f"pr/s1/subagents/agent-{n}.meta.json").write_text(
            json.dumps({"agentType": "coder", "description": "E7-01 work"}))


def test_runs_summary_and_line(root, tdb, proj):
    idx, base = proj
    _two_runs(root, base)
    T.scan(tdb, root)
    d = T.ticket_summary(idx, base, "E7-01", tdb=tdb)
    assert d["runs"] == 3 and d["requests"] == 4 and d["total"] == 4 * 135 and d["output"] == 80
    assert d["top_agent"] == "coder" and d["top_share"] == 0.75
    assert d["models"] == ["Sonnet", "Opus"] and d["efforts"] == ["medium", "high"]
    line = T.summary_line(d)
    assert line == "tokens: 540 · output 80 · coder 75% · Sonnet/Opus · medium/high · запусків 3"
    assert base not in line and "s1" not in line
    assert T.ticket_summary(idx, base, "E2-05", tdb=tdb) is None
    assert T.compact(10_600_000) == "10,6 млн"
    assert T.summary_line({**d, "efforts": []}).count("effort -") == 1
    assert T._family("weird") == "weird" and T._family("claude-haiku-4-5") == "Haiku"


def test_cli_summary(root, tdb, proj):
    idx, base = proj
    idx.commit()
    set_claude_dir(root)
    _two_runs(root, base)
    args = ["tokens", "--project", base]
    assert runner.invoke(app, [*args, "--summary"]).exit_code == 2
    res = runner.invoke(app, [*args, "--ticket", "E7-01", "--summary"])
    assert res.exit_code == 0, res.output
    assert res.output.strip().startswith("tokens: 540 · output 80 · coder 75%") and len(res.output.splitlines()) == 1
    assert runner.invoke(app, [*args, "--ticket", "E7-01", "--summary", "--no-scan"]).output == res.output
    res = runner.invoke(app, [*args, "--ticket", "E7-01", "--summary", "--json"])
    assert set(json.loads(res.output)) == {"total", "output", "top_agent", "top_share", "models", "efforts",
                                           "runs", "requests"}
    res = runner.invoke(app, [*args, "--ticket", "E2-05", "--summary"])
    assert res.exit_code == 1 and "no usage found" in res.output


def test_main_agent_shown_as_orchestrator():
    d = {"total": 100, "output": 10, "top_agent": "main", "top_share": 0.59, "models": ["Opus"],
         "efforts": ["medium"], "runs": 5, "requests": 3}
    assert "оркестратор 59%" in T.summary_line(d) and "main" not in T.summary_line(d)


# ------------------------------------------------------------------ UI reports (E7-03, ADR-016)

NOW = 1_794_312_000.0      # 2026-11-10 12:00 UTC


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _report_fixture(root, tdb, base):
    wt = f"{base}/.worktrees/E1-06"
    day1, day2 = NOW - 86400, NOW - 3 * 86400
    put(root, "pr/s1.jsonl",
        rec("m1", cwd=base, branch="main", ts=_iso(day1), model="claude-opus-5-5"),                    # orchestrator, no ticket
        rec("m2", "r2", cwd=wt, branch="main", ts=_iso(day1), model="claude-opus-5-5", out=50))        # orchestrator in a worktree
    put(root, "pr/s1/subagents/agent-a.jsonl", rec("a1", "ra", cwd=base, ts=_iso(day1), out=500),
        rec("a2", "rb", cwd=base, ts=_iso(day2), effort="high", model="claude-opus-5-5", out=500))
    (root / "pr/s1/subagents/agent-a.meta.json").write_text(json.dumps({"agentType": "coder", "description": "E7-01 work"}))
    put(root, "pr/s1/subagents/agent-n.jsonl", rec("n1", "rn", cwd=base, ts=_iso(day1), out=1))         # subagent, no ticket
    put(root, "pr/s1/subagents/agent-old.jsonl", rec("o1", "ro", cwd=base, ts=_iso(NOW - 40 * 86400)))   # outside the period
    (root / "pr/s1/subagents/agent-old.meta.json").write_text(json.dumps({"agentType": "coder", "description": "E7-01 old"}))
    T.scan(tdb, root)


def test_project_report(root, tdb, proj):
    idx, base = proj
    idx.execute("UPDATE tickets SET title='Хуки сесій' WHERE id='E7-01'")
    _report_fixture(root, tdb, base)
    r = T.project_report(idx, base, 7, tdb=tdb, now=NOW)
    days = [d["day"] for d in r["days"]]
    assert len(days) == 7 and days == sorted(days) and all(d["ts"] > 0 for d in r["days"])
    assert sum(1 for d in r["days"] if d["total"]) == 2                                        # the other five are zeros
    assert r["totals"]["total"] == 135 + 165 + 615 + 615 + 116 == sum(d["total"] for d in r["days"])
    assert r["totals"]["total"] == sum(a["total"] for a in r["agents"]) and r["totals"]["output"] == 20 + 50 + 500 + 500 + 1
    assert r["days"][-2]["total"] == 135 + 165 + 615 + 116 and r["days"][-4]["total"] == 615   # local days, oldest first
    rows = {(a["agent"], a["orch"]): a for a in r["agents"]}
    assert set(rows) == {("main", True), ("main", False), ("coder", False), ("unknown", False)}
    assert rows[("main", True)]["total"] == 135 and rows[("main", False)]["total"] == 165     # worktree request is the ticket's
    assert rows[("unknown", False)]["runs"] == 1                                              # subagent without ticket: own row
    assert rows[("coder", False)]["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]         # full ids, equal shares by name
    assert rows[("coder", False)]["efforts"] == ["high", "medium"] and rows[("coder", False)]["runs"] == 1
    assert rows[("coder", False)]["requests"] == 2 and rows[("coder", False)]["output"] == 1000
    assert rows[("main", True)]["efforts"] == ["medium"] and 0 < rows[("coder", False)]["cache_share"] < 1
    assert [a["total"] for a in r["agents"]] == sorted((a["total"] for a in r["agents"]), reverse=True)
    assert [t["id"] for t in r["tickets"]] == ["E7-01", "E1-06"]
    top = {t["id"]: t for t in r["tickets"]}
    assert top["E7-01"]["title"] == "Хуки сесій" and top["E7-01"]["top_agent"] == "coder" and top["E7-01"]["top_share"] == 1.0
    assert top["E1-06"]["title"] == "t" and top["E1-06"]["top_agent"] == "main"
    assert not {T.ORCH, T.NO_TICKET} & set(top) and r["top_ticket"] == r["tickets"][0]
    assert T.project_report(idx, base, 30, tdb=tdb, now=NOW)["totals"]["total"] == r["totals"]["total"]   # 40 days old: out
    blob = json.dumps(r, ensure_ascii=False)
    assert "cwd" not in blob and "session" not in blob and "git_branch" not in blob and SENTINEL not in blob and base not in blob


def test_project_report_empty_and_top_limit(root, tdb, proj):
    idx, base = proj
    r = T.project_report(idx, base, 14, tdb=tdb, now=NOW)
    assert r["totals"]["total"] == 0 and r["agents"] == [] and r["tickets"] == [] and r["top_ticket"] is None
    assert len(r["days"]) == 14 and all(d["total"] == 0 for d in r["days"])
    assert r == T.empty_report(14, now=NOW)
    for i in range(10):
        idx.execute("INSERT INTO tickets(project_path, id, title, status) VALUES(?, ?, 't', 'todo')", (base, f"E9-{i:02d}"))
        put(root, f"pr/s{i}.jsonl", rec(f"m{i}", f"r{i}", cwd=base, branch=f"t/E9-{i:02d}-x", ts=_iso(NOW - 3600), out=i + 1))
    T.scan(tdb, root)
    r = T.project_report(idx, base, 7, tdb=tdb, now=NOW)
    assert len(r["tickets"]) == 7 and r["tickets"][0]["id"] == "E9-09"
    assert T.project_report(None, base, 7, tdb=tdb, now=NOW)["tickets"] == []     # no index: nothing is attributed


def test_ticket_card(root, tdb, proj):
    idx, base = proj
    _two_runs(root, base)
    T.scan(tdb, root)
    c = T.ticket_card(idx, base, "E7-01", tdb=tdb)
    assert c["line"] == T.summary_line(T.ticket_summary(idx, base, "E7-01", tdb=tdb)) == "tokens: 540 · output 80 · coder 75% · Sonnet/Opus · medium/high · запусків 3"
    assert [a["agent"] for a in c["by_agent"]] == ["coder", "main"]
    coder = c["by_agent"][0]
    assert coder["runs"] == 2 and coder["total"] == 3 * 135 and coder["output"] == 60 and coder["models"] == ["claude-sonnet-5-5"]
    assert coder["efforts"] == ["medium"] and 0 < c["cache_share"] < 1
    assert "cwd" not in json.dumps(c) and "session" not in json.dumps(c) and "git_branch" not in json.dumps(c)
    assert T.ticket_card(idx, base, "E2-05", tdb=tdb) is None


# ------------------------------------------------------------------ API (E7-03)

@pytest.fixture
def api(ws, root):
    from conftest import do_sync

    do_sync(ws)
    set_claude_dir(root)
    st = daemon.DaemonState(7878, "t")
    st.refresh()
    with st.db_lock:
        st.db().execute("INSERT INTO tickets(project_path, id, title, status) VALUES('acme', 'E7-01', 'Хуки <b>', 'todo')")
    yield st, TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878")
    st.executor.shutdown(wait=True)
    st.close()


def test_tokens_api(api, root):
    st, c = api
    base = str(st.project_row("acme")["root"])
    ts = _iso(time.time() - 3600)
    put(root, "pr/s1.jsonl", rec("m1", cwd=base, ts=ts, content=SENTINEL))
    put(root, "pr/s1/subagents/agent-a.jsonl", rec("a1", "ra", cwd=base, ts=ts, out=500))
    (root / "pr/s1/subagents/agent-a.meta.json").write_text(json.dumps({"agentType": "coder", "description": f"E7-01 {SENTINEL}"}))
    tdb = T.open_db()
    T.scan(tdb, root)
    tdb.close()
    r = c.get("/api/tokens/project?project=acme&days=7")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is True and len(d["days"]) == 7 and d["totals"]["total"] == 135 + 615
    assert {a["agent"] for a in d["agents"]} == {"main", "coder"} and d["tickets"][0]["id"] == "E7-01"
    raw_text = r.text
    for bad in ("cwd", "session_id", "git_branch", SENTINEL, base):
        assert bad not in raw_text
    assert len(c.get("/api/tokens/project?project=acme").json()["days"]) == 14             # default period
    card = c.get("/api/tokens/ticket?project=acme&id=E7-01").json()
    assert card["enabled"] is True and card["card"]["line"].startswith("tokens: 615")
    assert SENTINEL not in json.dumps(card) and "cwd" not in json.dumps(card)
    assert c.get("/api/tokens/ticket?project=acme&id=E2-05").json() == {"enabled": True, "card": None}


def test_tokens_api_errors_and_empty(api, monkeypatch):
    st, c = api
    for bad in ("0", "15", "x", "-7"):
        assert c.get(f"/api/tokens/project?project=acme&days={bad}").status_code == 400
    assert c.get("/api/tokens/project?project=nope&days=7").status_code == 404
    assert c.get("/api/tokens/project?days=7").status_code == 400
    assert c.get("/api/tokens/ticket?project=acme").status_code == 400
    assert c.get("/api/tokens/ticket?project=nope&id=E1-01").status_code == 404
    assert not config.tokens_db_path().exists()                                          # nothing to read yet
    d = c.get("/api/tokens/project?project=acme&days=7")
    assert d.status_code == 200 and d.json()["enabled"] is True and d.json()["agents"] == [] and len(d.json()["days"]) == 7
    assert c.get("/api/tokens/ticket?project=acme&id=E7-01").json() == {"enabled": True, "card": None}
    p = config.home() / "config.toml"
    p.write_text(p.read_text().replace("tokens_enabled = true", "tokens_enabled = false"))
    d = c.get("/api/tokens/project?project=acme&days=30").json()
    assert d["enabled"] is False and d["totals"]["total"] == 0 and len(d["days"]) == 30
    assert c.get("/api/tokens/ticket?project=acme&id=E7-01").json() == {"enabled": False, "card": None}


def test_project_report_runs_count_distinct_files(root, tdb, proj):
    idx, base = proj
    for i in (1, 2):
        idx.execute("INSERT INTO tickets(project_path, id, title, status) VALUES(?, ?, 't', 'todo')", (base, f"E9-{i:02d}"))
    put(root, "pr/one.jsonl",
        rec("m1", "r1", cwd=base, branch="t/E9-01-x", ts=_iso(NOW - 3600)),
        rec("m2", "r2", cwd=base, branch="t/E9-02-x", ts=_iso(NOW - 3000)))
    # a subagent file with requests of a ticket (cwd in its worktree) and without one
    put(root, "pr/one/subagents/agent-a.jsonl",
        rec("a1", "ra", cwd=f"{base}/.worktrees/E9-01", ts=_iso(NOW - 2000)),
        rec("a2", "rb", cwd=base, ts=_iso(NOW - 1000)))
    (root / "pr/one/subagents/agent-a.meta.json").write_text(json.dumps({"agentType": "coder", "description": "work"}))
    T.scan(tdb, root)
    r = T.project_report(idx, base, 7, tdb=tdb, now=NOW)
    rows = {(a["agent"], a["orch"]): a for a in r["agents"]}
    assert rows[("main", False)]["requests"] == 2 and rows[("main", False)]["runs"] == 1     # one file, two tickets
    assert rows[("coder", False)]["requests"] == 2 and rows[("coder", False)]["runs"] == 1   # ticket + no ticket
    assert r["totals"]["total"] == sum(d["total"] for d in r["days"])


def test_project_report_ignores_future_requests(root, tdb, proj):
    idx, base = proj
    put(root, "pr/f.jsonl", rec("m1", "r1", cwd=base, ts=_iso(NOW - 3600)), rec("m2", "r2", cwd=base, ts=_iso(NOW + 3 * 86400)))
    T.scan(tdb, root)
    r = T.project_report(idx, base, 7, tdb=tdb, now=NOW)
    assert r["totals"]["total"] == 135 == sum(d["total"] for d in r["days"])


# ------------------------------------------------------------------ E6-10 idle CPU

def _count_selects(tdb, fn):
    sel = []
    tdb.set_trace_callback(lambda q: sel.append(q) if "FROM token_files" in q and q.lstrip().startswith("SELECT")
                           else None)
    try:
        fn()
    finally:
        tdb.set_trace_callback(None)
    return sel


def test_full_scan_reads_file_state_with_one_select(root, tdb):
    for i in range(20):
        put(root, f"pr/s{i}.jsonl", rec(f"m{i}", f"r{i}"))
    T.scan(tdb, root)
    sel = _count_selects(tdb, lambda: T.scan(tdb, root))
    assert [q for q in sel if "SELECT *" in q] == ["SELECT * FROM token_files"]   # one state read, none per path
    assert not [q for q in sel if "path=" in q]
    assert len(sel) <= 3   # plus the two aggregates of stats()
    assert n_rows(tdb) == 20


def test_light_scan_skips_discovery_and_picks_up_growth(root, tdb, monkeypatch):
    p = put(root, "pr/s1.jsonl", rec("m1"))
    T.scan(tdb, root)
    calls = []
    real = T._discover
    monkeypatch.setattr(T, "_discover", lambda r: (calls.append(r), real(r))[1])
    put(root, "pr/s1.jsonl", rec("m2", "r2"))
    put(root, "pr/new.jsonl", rec("m3", "r3"))
    st = T.scan(tdb, root, full=False)
    assert calls == [] and n_rows(tdb) == 2   # the grown file is read, the new one waits for a full pass
    assert st.files == 1
    T.scan(tdb, root, full=True)
    assert len(calls) == 1 and n_rows(tdb) == 3
    assert p.exists()


def test_light_scan_does_not_mark_gone_and_finishes_big_files(root, tdb, monkeypatch):
    p = put(root, "pr/s1.jsonl", *[rec(f"m{i}", f"r{i}") for i in range(5)])
    monkeypatch.setattr(T, "BLOCK", 100)
    T.scan(tdb, root, budget_bytes=100)
    assert tdb.execute('SELECT "offset" < size FROM token_files').fetchone()[0] == 1
    p2 = put(root, "pr/other.jsonl", rec("x", "rx"))
    T.scan(tdb, root, full=False)   # pending file continues without discovery
    T.scan(tdb, root, full=True)    # registers the new file
    p2.unlink()
    T.scan(tdb, root, full=False)
    assert tdb.execute("SELECT count(*) FROM token_files WHERE gone=1").fetchone()[0] == 0
    T.scan(tdb, root, full=True)
    assert tdb.execute("SELECT count(*) FROM token_files WHERE gone=1").fetchone()[0] == 1


def test_tick_full_scan_every_five_minutes_and_config_not_every_tick(root, monkeypatch):
    set_claude_dir(root)
    put(root, "pr/s1.jsonl", rec("m1"))
    st = daemon.DaemonState(7878, "t", background=True)
    fulls, loads = [], []
    real_load = config.load_config
    monkeypatch.setattr(T, "scan", lambda tdb, r, **k: fulls.append(k.get("full")) or T.ScanStats())
    monkeypatch.setattr(config, "load_config", lambda: loads.append(1) or real_load())
    clock = [1000.0]
    class _Time:   # only the daemon module sees the fake clock
        def __getattr__(self, n):
            return getattr(time, n)

        monotonic = staticmethod(lambda: clock[0])

    monkeypatch.setattr(daemon, "time", _Time())
    try:
        for step in range(0, 700, 1):
            clock[0] = 1000.0 + step
            st._tick_tokens()
            if st._tokens_future is not None:
                st._tokens_future.result(timeout=10)
        assert fulls[0] is True and fulls[1:5] == [False] * 4
        assert fulls.count(True) == 3   # t=0, 300, 600
        assert len(fulls) == 12         # every 60 s
        assert 10 <= len(loads) <= 13   # about once per 60 s, not every second
    finally:
        st.close()


def test_light_scan_active_sessions_before_unfinished_backlog(root, tdb, monkeypatch):
    for i in range(3):
        put(root, f"pr/old{i}.jsonl", *[rec(f"o{i}{k}", f"r{i}{k}") for k in range(3)])
    monkeypatch.setattr(T, "BLOCK", 100)
    T.scan(tdb, root, budget_bytes=1)   # the backlog stays unfinished
    monkeypatch.setattr(T, "LIGHT_LIMIT", 2)
    rows = T._light_targets(tdb, time.time())
    assert len(rows) >= 2 and all(r["mtime"] is not None for r in rows[:2])
    assert [r["mtime"] for r in rows[:2]] == sorted((r["mtime"] for r in rows[:2]), reverse=True)


# ------------------------------------------------------------------ session context (E0-05)

def _ctx_session(root):
    put(root, "pr/s1.jsonl",
        rec("m1", "r1", inp=1, cr=10, cw=1, ts="2026-11-09T10:00:00.000Z"),
        rec("m2", "r2", inp=3, cr=200, cw=7, model="claude-opus-5-1", ts="2026-11-09T11:00:00.000Z"))
    put(root, "pr/s1/subagents/agent-a.jsonl", rec("a1", "ra", inp=999, cr=999, cw=999, ts="2026-11-09T12:00:00.000Z"))
    put(root, "pr/s2.jsonl", rec("o1", "ro", inp=5, cr=5, cw=5, ts="2026-11-09T13:00:00.000Z"))


def test_session_context_is_last_main_request_without_subagents(root, tdb):
    _ctx_session(root)
    T.scan(tdb, root)
    d = T.session_context(tdb, "s1")
    assert d["context"] == 3 + 200 + 7 and d["model"] == "claude-opus-5-1" and d["session"] == "s1"
    assert d["ts"] == 1794222000.0


def test_session_context_unknown_or_subagent_only_session_is_none(root, tdb):
    put(root, "pr/s3/subagents/agent-a.jsonl", rec("a1", "ra"))
    _ctx_session(root)
    T.scan(tdb, root)
    assert T.session_context(tdb, "nope") is None
    assert T.session_context(tdb, "s3") is None


def test_cli_context_text_json_and_errors(root, tdb):
    set_claude_dir(root)
    _ctx_session(root)
    res = runner.invoke(app, ["tokens", "--session", "s1", "--context"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == f"210 ({T.compact(210)})"
    res = runner.invoke(app, ["tokens", "--session", "s1", "--context", "--json", "--no-scan"])
    d = json.loads(res.output)
    assert d["context"] == 210 and d["session"] == "s1" and SENTINEL not in res.output
    res = runner.invoke(app, ["tokens", "--session", "missing", "--context", "--no-scan"])
    assert res.exit_code == 1 and "no usage found" in res.output
    res = runner.invoke(app, ["tokens", "--context"])
    assert res.exit_code == 2


def test_cli_context_json_without_data_is_valid_json(root, tdb):
    set_claude_dir(root)
    res = runner.invoke(app, ["tokens", "--session", "missing", "--context", "--json", "--no-scan"])
    assert res.exit_code == 1
    assert json.loads(res.output) == {"session": "missing", "context": None}


def test_cli_context_rejects_ignored_flags(root, tdb):
    set_claude_dir(root)
    for extra in (["--ticket", "E1-01"], ["--project", "."], ["--since", "7d"], ["--by", "model"], ["--summary"]):
        res = runner.invoke(app, ["tokens", "--session", "s1", "--context", "--no-scan", *extra])
        assert res.exit_code == 2, extra
