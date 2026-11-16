"""Aggregate myvec versus self-search per project (ADR-029) and the `myvec searches` CLI. Fixed time, tmp home."""
import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from myvec import config, searchlog, searchstats, tokens as T
from myvec import db as dbmod
from myvec.commands import app
from test_tool_calls import bash, put, tool

runner = CliRunner()
BASE_TS = datetime(2026, 11, 9, 10, 0, 0, tzinfo=timezone.utc).timestamp()
NOW = BASE_TS + 3600
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def iso(off):
    return datetime.fromtimestamp(BASE_TS + off, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "claude_projects"
    r.mkdir()
    return r


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "acme"
    b.mkdir()
    return str(b)


@pytest.fixture
def idx(base):
    db = dbmod.open_ready(config.load_config())
    db.execute("INSERT INTO projects(path, root) VALUES(?, ?)", (base, base))
    db.commit()
    yield db
    db.close()


@pytest.fixture
def tdb():
    db = T.open_db()
    yield db
    db.close()


def scan(tdb, root):
    T.scan(tdb, root, now=NOW)


def row(res, project):
    return next(d for d in res["projects"] if d["project"] == project)


def log(ts, **kw):
    searchlog.record(searchlog.shape("some query", ts=ts, **kw))


def test_empty_data_gives_empty_aggregate(idx, tdb):
    res = searchstats.usage(idx, tdb, 14, NOW)
    assert res["projects"] == [] and res["total"]["share"] is None and res["total"]["myvec"] == 0
    assert res["collector"]["backfill"] is False and res["collector"]["last_scan"] is None
    res = searchstats.usage(None, None, 14, NOW)
    assert res["projects"] == [] and res["total"]["p50_ms"] is None


def test_counts_share_sessions_and_subagents(root, tdb, idx, base):
    put(root, "pr/s1.jsonl",
        bash("a1", "myvec search foo", cwd=base, sid="s1", ts=iso(10)),
        tool("a2", "Grep", {"pattern": "x"}, cwd=base, sid="s1", ts=iso(20)),
        bash("a3", "myvec recall bar", cwd=base, sid="s1", ts=iso(30)),
        bash("a4", "rg y", cwd=base, sid="s1", ts=iso(40)))
    put(root, "pr/s2.jsonl", bash("b1", "grep z", cwd=base, sid="s2", ts=iso(50)))
    put(root, "pr/s1/subagents/agent-x.jsonl",
        bash("c1", "grep q", cwd=base, sid="s1", ts=iso(60)), bash("c2", "myvec search w", cwd=base, ts=iso(70)))
    scan(tdb, root)
    res = searchstats.usage(idx, tdb, 14, NOW)
    d = row(res, base)
    assert (d["myvec"], d["recall"], d["self"]) == (2, 1, 4)
    assert d["self_sub"] == 1 and d["myvec_sub"] == 1
    assert d["share"] == pytest.approx(2 / 6)
    assert d["sessions"] == 2 and d["sessions_myvec"] == 1
    assert res["total"]["myvec"] == 2 and res["total"]["share"] == pytest.approx(2 / 6)
    assert res["days"] == 14


def test_miss_is_self_within_window_after_myvec(root, tdb, idx, base):
    lines = [bash("m1", "myvec search a", cwd=base, ts=iso(1)), tool("g1", "Grep", {}, cwd=base, ts=iso(2))]   # miss
    lines += [bash("m2", "myvec search b", cwd=base, ts=iso(3))]
    lines += [bash(f"n{i}", "ls", cwd=base, ts=iso(4 + i)) for i in range(searchstats.MISS_WINDOW + 1)]
    lines += [tool("g2", "Grep", {}, cwd=base, ts=iso(30))]                                                # too far
    lines += [bash("m3", "myvec search c", cwd=base, ts=iso(40))]                                           # no self after
    put(root, "pr/s1.jsonl", *lines)
    scan(tdb, root)
    d = row(searchstats.usage(idx, tdb, 14, NOW), base)
    assert d["myvec"] == 3 and d["miss"] == 1


def test_miss_boundary_exactly_at_window(root, tdb, idx, base):
    lines = [bash("m1", "myvec search a", cwd=base, ts=iso(1))]
    lines += [bash(f"n{i}", "ls", cwd=base, ts=iso(2 + i)) for i in range(searchstats.MISS_WINDOW - 1)]
    lines += [tool("g1", "Grep", {}, cwd=base, ts=iso(30))]       # seq = 1 + (W-1) + 1 = m1.seq + W
    put(root, "pr/s1.jsonl", *lines)
    scan(tdb, root)
    assert row(searchstats.usage(idx, tdb, 14, NOW), base)["miss"] == 1


def test_project_attribution_worktree_and_outside(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("a", "rg x", cwd=f"{base}/.worktrees/E1-01/src", sid="s1", ts=iso(1)),
        bash("b", "rg x", cwd="/elsewhere/proj", sid="s1", ts=iso(2)),
        bash("c", "rg x", cwd=None, sid="s1", ts=iso(3)))
    scan(tdb, root)
    res = searchstats.usage(idx, tdb, 14, NOW)
    assert row(res, base)["self"] == 1 and row(res, T.NO_PROJECT)["self"] == 2


def test_window_excludes_old_calls(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("old", "rg x", cwd=base, ts=iso(-30 * 86400)), bash("new", "rg x", cwd=base, ts=iso(5)))
    scan(tdb, root)
    assert row(searchstats.usage(idx, tdb, 14, NOW), base)["self"] == 1
    assert row(searchstats.usage(idx, tdb, 60, NOW), base)["self"] == 2


def test_log_metrics_logged_zero_results_p50_via(idx, tdb, base):
    for ts, res, ms, via in [(NOW - 60, 3, 10, "daemon"), (NOW - 50, 0, 20, "direct"), (NOW - 40, 5, 30, "ui")]:
        log(ts, project=base, results=res, ms=ms, via=via)
    log(NOW - 30, project=None, results=1, ms=100, via="direct")
    log(NOW - 99999999, project=base, results=1, ms=1, via="direct")          # outside the window
    d = row(searchstats.usage(idx, tdb, 14, NOW), base)
    assert d["logged"] == 3 and d["zero_results"] == 1 and d["p50_ms"] == 20
    assert d["via"] == {"daemon": 1, "direct": 1, "ui": 1} and d["share"] is None
    res = searchstats.usage(idx, tdb, 14, NOW)
    assert row(res, T.NO_PROJECT)["logged"] == 1 and res["total"]["logged"] == 4
    assert res["total"]["p50_ms"] == 25 and res["total"]["via"]["direct"] == 2


def test_agent_attribution_by_nearest_myvec_call(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("a1", "myvec search a", cwd=base, sid=SID, ts=iso(100)))
    put(root, f"pr/{SID}/subagents/agent-x.jsonl", bash("c1", "myvec search b", cwd=base, ts=iso(200)))
    (root / f"pr/{SID}/subagents/agent-x.meta.json").write_text(json.dumps({"agentType": "coder"}))
    put(root, f"pr/{SID}.jsonl", bash("a1", "myvec search a", cwd=base, sid=SID, ts=iso(100)))
    scan(tdb, root)
    log(BASE_TS + 102, project=base, session=SID, via="direct")           # near the main call
    log(BASE_TS + 201, project=base, session=SID, via="direct")           # near the sub-agent call
    log(BASE_TS + 500, project=base, session=SID, via="direct")           # nothing within 5 s
    log(BASE_TS + 100, project=base, session=None, via="ui")              # no session
    d = row(searchstats.usage(idx, tdb, 14, NOW), base)
    assert d["agents"]["coder"] == 1 and d["agents"]["unknown"] == 1
    assert sum(d["agents"].values()) == 3 and d["logged"] == 4
    assert d["agents"].get("main", 0) == 1


def test_no_tokens_db_still_reports_log(idx, base):
    log(NOW - 5, project=base, results=2, ms=7, via="direct")
    res = searchstats.usage(idx, None, 14, NOW)
    assert row(res, base)["logged"] == 1 and row(res, base)["self"] == 0


def test_collector_reports_backfill_and_disabled(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("a", "rg x", cwd=base, ts=iso(1)))
    scan(tdb, root)
    assert searchstats.collector(tdb)["backfill"] is False and searchstats.collector(tdb)["last_scan"] == NOW
    tdb.execute('UPDATE token_files SET "offset"=0')
    c = searchstats.collector(tdb)
    assert c["backfill"] is True and c["pending_bytes"] > 0 and c["enabled"] is True


# ------------------------------------------------------------------ CLI

def seed_cli(root, tdb, idx, base):
    import time
    now = time.time()
    ts = datetime.fromtimestamp(now - 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    put(root, "pr/s1.jsonl", bash("a1", "myvec search a", cwd=base, sid="s1", ts=ts),
        tool("a2", "Grep", {}, cwd=base, sid="s1", ts=ts), bash("a3", "rg q", cwd=base, sid="s1", ts=ts),
        bash("a4", "rg q", cwd=base, sid="s1", ts=ts))
    T.scan(tdb, root)
    log(now - 100, project=base, results=0, ms=1234, via="daemon")


def test_cli_json_has_project_fields(root, tdb, idx, base):
    seed_cli(root, tdb, idx, base)
    r = runner.invoke(app, ["searches", "--days", "14", "--json", "--no-scan"])
    assert r.exit_code == 0, r.output
    j = json.loads(r.stdout)
    d = next(x for x in j["projects"] if x["project"] == base)
    for k in ("sessions", "sessions_myvec", "myvec", "recall", "self", "share", "miss", "self_sub",
              "logged", "zero_results", "p50_ms"):
        assert k in d
    assert (d["myvec"], d["self"], d["miss"], d["logged"], d["zero_results"]) == (1, 3, 1, 1, 1)
    assert d["share"] == pytest.approx(0.25) and j["days"] == 14 and "collector" in j


def test_cli_table_ukrainian_numbers_and_percent(root, tdb, idx, base):
    seed_cli(root, tdb, idx, base)
    r = runner.invoke(app, ["searches", "--no-scan"])
    assert r.exit_code == 0, r.output
    assert "проєкт" in r.stdout and "частка" in r.stdout and "усього" in r.stdout
    assert "25%" in r.stdout and "1\u00a0234" in r.stdout


def test_cli_project_filter_and_unknown_project(root, tdb, idx, base):
    seed_cli(root, tdb, idx, base)
    r = runner.invoke(app, ["searches", "--no-scan", "--json", "--project", base])
    assert [d["project"] for d in json.loads(r.stdout)["projects"]] == [base]
    r = runner.invoke(app, ["searches", "--no-scan", "--project", "/nope"])
    assert r.exit_code == 1


def test_cli_runs_scan_unless_no_scan(root, tdb, idx, base, monkeypatch):
    seed_cli(root, tdb, idx, base)
    set_dir = config.home() / "config.toml"
    set_dir.write_text(set_dir.read_text().replace('claude_projects_dir = ""', f'claude_projects_dir = "{root}"'))
    put(root, "pr/s9.jsonl", bash("z1", "rg late", cwd=base, sid="s9",
                                  ts=datetime.fromtimestamp(__import__("time").time() - 60, timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%S.000Z")))
    r = runner.invoke(app, ["searches", "--no-scan", "--json"])
    assert next(x for x in json.loads(r.stdout)["projects"] if x["project"] == base)["self"] == 3
    r = runner.invoke(app, ["searches", "--json"])
    assert next(x for x in json.loads(r.stdout)["projects"] if x["project"] == base)["self"] == 4


def test_cli_without_any_data(env):
    r = runner.invoke(app, ["searches", "--no-scan"])
    assert r.exit_code == 0 and "усього" in r.stdout


def test_routes_and_fts_fallback_counted_old_records_tolerated(idx, tdb, base):
    log(NOW - 60, project=base, results=1, ms=1, via="daemon", route="vector")
    log(NOW - 50, project=base, results=0, ms=1, via="daemon", route="fts-fallback")
    log(NOW - 40, project=base, results=1, ms=1, via="daemon")                     # old record, no route
    log(NOW - 30, project=base, results=1, ms=1, via="daemon", route="from-the-future")
    res = searchstats.usage(idx, tdb, 14, NOW)
    d = row(res, base)
    assert d["fts_fallback"] == 1 and d["logged"] == 4
    assert d["routes"] == {"vector": 1, "fts-fallback": 1, "?": 1, "from-the-future": 1}
    assert res["total"]["fts_fallback"] == 1 and res["total"]["routes"]["fts-fallback"] == 1


def test_table_has_fts_fallback_column(idx, tdb, base):
    log(NOW - 60, project=base, results=1, ms=1, via="daemon", route="fts-fallback")
    from myvec import searchstatscli
    assert "FTS-fallback" in searchstatscli.render(searchstats.usage(idx, tdb, 14, NOW))


def test_non_string_route_values_do_not_crash(idx, tdb, base):
    for i, bad in enumerate([None, "", 5, ["fts-fallback"], {"a": 1}]):
        log(NOW - 60 + i, project=base, results=1, ms=1, via="daemon", route=bad)
    res = searchstats.usage(idx, tdb, 14, NOW)
    d = row(res, base)
    assert d["logged"] == 5 and d["fts_fallback"] == 0 and sum(d["routes"].values()) == 5
    assert all(isinstance(k, str) for k in d["routes"])
    json.dumps(res)


def test_fts_fallback_record_still_counted_as_myvec_search(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("a1", "myvec search foo", cwd=base, sid="s1", ts=iso(10)),
        bash("a2", "rg y", cwd=base, sid="s1", ts=iso(40)))
    scan(tdb, root)
    before = row(searchstats.usage(idx, tdb, 14, NOW), base)
    log(BASE_TS + 10, project=base, results=1, ms=1, via="daemon", route="fts-fallback", session="s1")
    d = row(searchstats.usage(idx, tdb, 14, NOW), base)
    assert d["fts_fallback"] == 1
    assert (d["myvec"], d["self"], d["share"]) == (before["myvec"], before["self"], before["share"]) == (1, 1, 0.5)


# --- E8-10: foreign OS, onset, explore sessions, p50 24h, --project resolver ---

def _put_calls(root, base, cwd_other=None):
    put(root, "pr/s1.jsonl", bash("a1", "rg old", cwd=base, sid="s1", ts=iso(0)),
        bash("a2", "myvec search a", cwd=base, sid="s1", ts=iso(100)), bash("a3", "rg q", cwd=base, sid="s1", ts=iso(200)),
        bash("f1", "grep x", cwd=f"{searchstats.FOREIGN_PREFIX}mac/proj", sid="s1", ts=iso(300)))
    T.scan(T.open_db(), root, now=NOW)


def test_foreign_os_rows_excluded_and_counted(root, tdb, idx, base):
    _put_calls(root, base)
    res = searchstats.usage(idx, tdb, 14, now=NOW)
    assert res["total"]["foreign"] == 1
    assert res["total"]["self"] == 1 and "(поза проєктами)" not in {d["project"] for d in res["projects"]}


def test_pre_onset_uses_first_myvec_when_no_registered_at(root, tdb, idx, base):
    _put_calls(root, base)
    d = row(searchstats.usage(idx, tdb, 14, now=NOW), base)
    assert (d["pre_onset"], d["self"], d["myvec"]) == (1, 1, 1)
    assert d["onset"] == BASE_TS + 100 and d["share"] == pytest.approx(0.5)


def test_onset_is_min_of_registered_at_and_first_myvec(root, tdb, idx, base):
    _put_calls(root, base)
    early = datetime.fromtimestamp(BASE_TS - 50).strftime("%Y-%m-%dT%H:%M:%S")
    idx.execute("UPDATE projects SET registered_at=?", (early,)); idx.commit()
    d = row(searchstats.usage(idx, tdb, 14, now=NOW), base)
    assert d["pre_onset"] == 0 and d["self"] == 2 and d["onset"] == pytest.approx(BASE_TS - 50, abs=1)
    late = datetime.fromtimestamp(BASE_TS + 3000).strftime("%Y-%m-%dT%H:%M:%S")   # after rebuild
    idx.execute("UPDATE projects SET registered_at=?", (late,)); idx.commit()
    d = row(searchstats.usage(idx, tdb, 14, now=NOW), base)
    assert d["onset"] == BASE_TS + 100 and d["pre_onset"] == 1


def test_explore_sessions_and_p50_24h(root, tdb, idx, base):
    _put_calls(root, base)
    log(NOW - 100, project=base, ms=100, via="daemon")
    log(NOW - 3 * 86400, project=base, ms=900, via="daemon")
    res = searchstats.usage(idx, tdb, 14, now=NOW)
    d = row(res, base)
    assert (d["sessions_explore"], d["sessions_myvec_explore"]) == (1, 1)
    assert d["p50_ms_24h"] == 100 and d["p50_ms"] == 500
    assert res["total"]["p50_ms_24h"] == 100 and res["total"]["sessions_explore"] == 1


def test_payload_has_no_paths_or_commands(root, tdb, idx, base):
    _put_calls(root, base)
    res = searchstats.usage(idx, tdb, 14, now=NOW)
    txt = json.dumps(res["total"])
    assert searchstats.FOREIGN_PREFIX not in txt and "rg " not in txt and "grep" not in txt


def test_cli_project_dot_worktree_name_and_registered_empty(root, tdb, idx, base, monkeypatch):
    import os
    seed_cli(root, tdb, idx, base)
    wt = os.path.join(base, ".worktrees", "X1")
    os.makedirs(wt)
    for arg, cwd in ((".", base), (".", wt), (base, base), ("/nope", None)):
        if cwd:
            monkeypatch.chdir(cwd)
        r = runner.invoke(app, ["searches", "--no-scan", "--json", "--project", arg])
        if cwd:
            assert r.exit_code == 0, r.output
            assert [d["project"] for d in json.loads(r.stdout)["projects"]] == [base]
        else:
            assert r.exit_code == 1
    idx.execute("INSERT INTO projects(path, root) VALUES('empty', ?)", (os.path.join(base, "e"),)); idx.commit()
    r = runner.invoke(app, ["searches", "--no-scan", "--json", "--project", "empty"])
    assert r.exit_code == 0 and json.loads(r.stdout)["projects"][0]["self"] == 0


def test_cli_project_typo_inside_project_fails(root, tdb, idx, base, monkeypatch):
    seed_cli(root, tdb, idx, base)
    monkeypatch.chdir(base)
    r = runner.invoke(app, ["searches", "--no-scan", "--json", "--project", "typo-name"])
    assert r.exit_code == 1 and "neither" in r.output


def test_existing_payload_keys_unchanged(root, tdb, idx, base):
    _put_calls(root, base)
    d = row(searchstats.usage(idx, tdb, 14, now=NOW), base)
    old = {"project", "sessions", "sessions_myvec", "myvec", "recall", "self", "self_sub", "myvec_sub", "share", "miss",
           "logged", "zero_results", "fts_fallback", "routes", "p50_ms", "via", "agents"}
    new = {"pre_onset", "onset", "sessions_explore", "sessions_myvec_explore", "p50_ms_24h"}
    e816 = {"self_by_kind", "classified", "share_explore", "doc_reads", "self_doc", "myvec_docs"}
    assert set(d) == old | new | e816
    assert set(searchstats.usage(idx, tdb, 14, now=NOW)["total"]) == old | new | e816 | {"foreign"}


def test_foreign_rows_not_in_total_or_share(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("a1", "myvec search a", cwd=base, sid="s1", ts=iso(10)),
        bash("f1", "grep x", cwd=f"{searchstats.FOREIGN_PREFIX}mac/proj", sid="s1", ts=iso(20)),
        bash("f2", "myvec search b", cwd=f"{searchstats.FOREIGN_PREFIX}mac/proj", sid="s1", ts=iso(30)))
    T.scan(T.open_db(), root, now=NOW)
    t = searchstats.usage(idx, tdb, 14, now=NOW)["total"]
    assert (t["foreign"], t["self"], t["myvec"], t["share"]) == (2, 0, 1, 1.0)


def test_cli_project_by_name_and_absolute_worktree_path(root, tdb, idx, base):
    import os
    seed_cli(root, tdb, idx, base)
    wt = os.path.join(base, ".worktrees", "E8-10")
    os.makedirs(wt)
    for arg in (base, wt):
        r = runner.invoke(app, ["searches", "--no-scan", "--json", "--project", arg])
        assert r.exit_code == 0, r.output
        assert [d["project"] for d in json.loads(r.stdout)["projects"]] == [base]


def test_cli_unregistered_path_exit_1_with_message(root, tdb, idx, tmp_path):
    r = runner.invoke(app, ["searches", "--no-scan", "--project", str(tmp_path / "other")])
    assert r.exit_code == 1
    assert "registered" in (r.output + (r.stderr if r.stderr_bytes else ""))


def test_cli_text_and_json_have_no_command_or_cwd_text(root, tdb, idx, base):
    seed_cli(root, tdb, idx, base)
    put(root, "pr/s9.jsonl", bash("z1", "grep secretpattern", cwd=f"{searchstats.FOREIGN_PREFIX}mac/proj", sid="s9", ts=iso(5)))
    T.scan(tdb, root, now=NOW)
    for args in ([], ["--json"]):
        r = runner.invoke(app, ["searches", "--no-scan", *args])
        assert r.exit_code == 0, r.output
        assert "secretpattern" not in r.output and searchstats.FOREIGN_PREFIX not in r.output and "rg q" not in r.output
        if "--json" in args:
            assert base not in json.dumps(json.loads(r.stdout)["total"])
    r = runner.invoke(app, ["searches", "--no-scan"])
    assert "не враховано:" in r.output and "p50 доба" in r.output


def test_kind_aggregates_share_explore_and_docs(root, tdb, idx, base):
    put(root, "pr/s1.jsonl",
        bash("k1", "myvec search foo", cwd=base, sid="s1", ts=iso(10)),
        bash("k2", "rg 'a|b'", cwd=base, sid="s1", ts=iso(20)),          # explore
        bash("k3", "rg foo", cwd=base, sid="s1", ts=iso(30)),            # exact
        bash("k4", "rg -c foo", cwd=base, sid="s1", ts=iso(40)),         # count (not in the denominator)
        bash("k5", "grep foo src/a.py", cwd=base, sid="s1", ts=iso(50)),   # file
        bash("k6", "rg --files", cwd=base, sid="s1", ts=iso(60)),        # unclassified
        bash("k7", "find . -name '*.md'", cwd=base, sid="s1", ts=iso(70)),   # name + doc
        tool("k8", "Read", {"file_path": "/p/docs/a.md"}, cwd=base, sid="s1", ts=iso(80)),
        tool("k9", "Read", {"file_path": "/p/docs/a.md"}, cwd=searchstats.FOREIGN_PREFIX + "x",
             sid="s1", ts=iso(85)))
    scan(tdb, root)
    log(BASE_TS + 15, via="cli", project=base, kind="docs", route="hybrid", results=3, ms=10)
    log(BASE_TS + 16, via="cli", project=base, kind="code", route="hybrid", results=3, ms=10)
    res = searchstats.usage(idx, tdb, 14, NOW)
    for d in (row(res, base), res["total"]):
        assert d["self_by_kind"] == {"logs": 0, "count": 1, "file": 1, "explore": 1, "exact": 1, "name": 1,
                                     "unclassified": 1}
        assert d["self"] == sum(d["self_by_kind"].values()) == 6
        assert d["classified"] == pytest.approx(5 / 6)
        assert d["share_explore"] == pytest.approx(1 / 4)                # 1 / (1 + 1 + 1 + 1 unclassified)
        assert (d["doc_reads"], d["self_doc"], d["myvec_docs"]) == (1, 1, 1)
    assert res["total"]["foreign"] == 1                                   # the foreign Read is not counted


def test_share_explore_none_without_denominator(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("k1", "rg -c foo", cwd=base, ts=iso(10)))
    scan(tdb, root)
    d = row(searchstats.usage(idx, tdb, 14, NOW), base)
    assert d["share_explore"] is None and d["classified"] == 1.0 and d["share"] == 0.0
    assert searchstats.blank_row("x")["classified"] is None


def test_cli_kind_columns_and_footer(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("k1", "myvec search foo", cwd=base, ts=iso(10)),
        bash("k2", "rg 'a|b'", cwd=base, ts=iso(20)), bash("k3", "rg --files", cwd=base, ts=iso(30)))
    scan(tdb, root)
    out = runner.invoke(app, ["searches", "--no-scan"]).output
    assert "розвідка" in out and "частка (розв.)" in out and "класиф." in out
    assert "некласифіковано: 1" in out and "читання доків: Read 0" in out
    data = json.loads(runner.invoke(app, ["searches", "--no-scan", "--json"]).output)
    assert data["total"]["self_by_kind"]["unclassified"] == 1


def test_cli_footer_unclassified_row_always_shown(root, tdb, idx, base):
    put(root, "pr/s1.jsonl", bash("k1", "rg 'a|b'", cwd=base, ts=iso(10)))
    scan(tdb, root)
    out = runner.invoke(app, ["searches", "--no-scan"]).output
    assert "некласифіковано: 0" in out and "читання доків:" in out


def test_readonly_v2_tokens_db_keeps_counts(root, tdb, idx, base):
    put(root, "pr/s1.jsonl",
        bash("a1", "myvec search foo", cwd=base, sid="s1", ts=iso(10)),
        bash("a2", "rg y", cwd=base, sid="s1", ts=iso(40)))
    scan(tdb, root)
    tdb.execute("ALTER TABLE tool_calls DROP COLUMN kind")
    tdb.execute("ALTER TABLE tool_calls DROP COLUMN doc")
    tdb.close()
    ro = T.open_db(readonly=True)
    try:
        d = row(searchstats.usage(idx, ro, 14, NOW), base)
    finally:
        ro.close()
    assert (d["myvec"], d["self"]) == (1, 1)
    assert d["self_by_kind"]["unclassified"] == 1


def test_resolve_project_symlink_spellings(tmp_path, monkeypatch):
    import os
    from myvec import searchstatscli
    real = tmp_path / "real" / "proj"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    db = dbmod.open_ready(config.load_config())
    try:
        # root recorded through the symlink (as after `relocate`)
        db.execute("INSERT INTO projects(path, root) VALUES('proj', ?)", (str(link / "proj"),))
        db.commit()
        for arg in (str(link / "proj"), str(link / "proj" / "sub"), str(real), str(real / "sub")):
            assert searchstatscli.resolve_project(db, arg) == "proj", arg
        monkeypatch.chdir(link / "proj" / "sub")
        monkeypatch.setenv("PWD", str(link / "proj" / "sub"))
        assert searchstatscli.resolve_project(db, ".") == "proj"
        # real root + a symlink to it
        db.execute("UPDATE projects SET root=?", (str(real),))
        db.commit()
        for arg in (str(link / "proj"), str(real / "sub")):
            assert searchstatscli.resolve_project(db, arg) == "proj", arg
        monkeypatch.setenv("PWD", "/nonexistent")
        assert searchstatscli.resolve_project(db, ".") == "proj"
        # a non-existent path never falls back to the cwd project
        assert searchstatscli.resolve_project(db, str(link / "typo")) is None
        assert searchstatscli.resolve_project(db, "typo-name") is None
    finally:
        db.close()


def test_resolve_project_nonexistent_path_none_even_with_real_root_fallback(tmp_path):
    from myvec import searchstatscli
    real = tmp_path / "real" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    db = dbmod.open_ready(config.load_config())
    try:
        db.execute("INSERT INTO projects(path, root) VALUES('proj', ?)", (str(link / "proj"),))
        db.commit()
        assert searchstatscli.resolve_project(db, str(real / "missing")) is None
        assert searchstatscli.resolve_project(db, str(link / "proj" / "missing")) is None
    finally:
        db.close()


def test_resolve_project_pwd_not_samefile_is_ignored(tmp_path, monkeypatch):
    from myvec import searchstatscli
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    db = dbmod.open_ready(config.load_config())
    try:
        db.execute("INSERT INTO projects(path, root) VALUES('pa', ?)", (str(a),))
        db.execute("INSERT INTO projects(path, root) VALUES('pb', ?)", (str(b),))
        db.commit()
        monkeypatch.chdir(a)
        monkeypatch.setenv("PWD", str(b))   # stale PWD pointing at another project
        assert searchstatscli.resolve_project(db, ".") == "pa"
    finally:
        db.close()


def test_resolve_project_real_root_fallback_picks_longest_nested_root(tmp_path):
    from myvec import searchstatscli
    outer = tmp_path / "real" / "outer"
    inner = outer / "inner"
    (inner / "src").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    db = dbmod.open_ready(config.load_config())
    try:
        db.execute("INSERT INTO projects(path, root) VALUES('outer', ?)", (str(link / "outer"),))
        db.execute("INSERT INTO projects(path, root) VALUES('inner', ?)", (str(link / "outer" / "inner"),))
        db.commit()
        assert searchstatscli.resolve_project(db, str(inner / "src")) == "inner"
        assert searchstatscli.resolve_project(db, str(outer)) == "outer"
    finally:
        db.close()
