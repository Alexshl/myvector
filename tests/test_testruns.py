import socket
import sqlite3

import pytest

from myvec import config, db as dbmod, testruns


@pytest.fixture
def db():
    config.ensure_home()
    d = sqlite3.connect(str(config.db_path()), isolation_level=None)
    d.row_factory = sqlite3.Row
    d.executescript(dbmod.TEST_RUNS_SCHEMA)
    return d


def add(db, uid, started, project="acme", state="done", exit_code=0, **kw):
    row = dict(run_uid=uid, source="wrapper", state=state, host=socket.gethostname(), pid=1, project_path=project, cwd="/secret/cwd",
               session_id="sess-1", command="pytest -q", started_at=started, ended_at=started + 10, wall_s=10.0,
               cpu_s=30.0, exit_code=exit_code, interrupted=0)
    row.update(kw)
    db.execute(f"INSERT INTO test_runs({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


def test_list_sorted_limited_and_whitelisted(db):
    for i in range(5):
        add(db, f"r{i}", 1000 + i)
    add(db, "other", 5000, project="beta")
    rs = testruns.list_runs(db, "acme", limit=3)
    assert [r["run_uid"] for r in rs] == ["r4", "r3", "r2"]
    assert not {"cwd", "session_id", "pid"} & set(rs[0])
    assert rs[0]["regression"] is None and rs[0]["outcome"] == "passed"
    assert [r["run_uid"] for r in testruns.list_runs(db, "acme", before=1002)] == ["r1", "r0"]
    assert testruns.list_runs(db)[0]["run_uid"] == "other"


def test_outcome(db):
    add(db, "ok", 1)
    add(db, "bad", 2, exit_code=1)
    add(db, "sig", 3, exit_code=None, signal=9)
    add(db, "int", 4, exit_code=130, interrupted=1)
    add(db, "lost", 5, state="lost", exit_code=None)
    add(db, "run", 6, state="running", exit_code=None, ended_at=None, wall_s=None)
    got = {r["run_uid"]: r["outcome"] for r in testruns.list_runs(db)}
    assert got == {"ok": "passed", "bad": "failed", "sig": "failed", "int": "interrupted", "lost": "lost", "run": "running"}
    assert [r["run_uid"] for r in testruns.running(db)] == ["run"]


def test_passive_done_without_code_is_ended(db):
    add(db, "p1", 1, source="passive", exit_code=None)
    add(db, "p2", 2, source="passive", exit_code=None, signal=15)
    add(db, "w1", 3, exit_code=None)    # a wrapper row without a code stays as before
    got = {r["run_uid"]: r["outcome"] for r in testruns.list_runs(db)}
    assert got == {"p1": "ended", "p2": "failed", "w1": "passed"}


def test_summary_medians_ignore_passive_ended(db):
    add(db, "w", 1, wall_s=10.0, cpu_s=30.0)
    add(db, "p", 2, source="passive", exit_code=None, wall_s=1.0, cpu_s=1.0)
    s = testruns.summary(db, "acme")
    assert s["median_wall_s"] == 10.0 and s["median_cpu_s"] == 30.0


def test_summary_medians_and_counts(db):
    for i, w in enumerate([10, 20, 30]):
        add(db, f"p{i}", 100 + i, wall_s=w, cpu_s=w * 2)
    add(db, "bad", 200, exit_code=1, wall_s=999, cpu_s=999)
    add(db, "pas", 201, source="passive")
    s = testruns.summary(db, "acme")
    assert s["n"] == 5 and s["n_wrapper"] == 4 and s["n_passive"] == 1
    assert s["first_at"] == 100 and s["last_at"] == 201
    assert s["median_wall_s"] == 15.0 and s["median_cpu_s"] == 35.0      # passed only: wall 10,10,20,30; cpu 20,30,40,60
    assert testruns.summary(db, "none")["n"] == 0


def test_no_table_is_empty():
    config.ensure_home()
    d = sqlite3.connect(":memory:")
    d.row_factory = sqlite3.Row
    assert testruns.list_runs(d) == [] and testruns.running(d) == []
    assert testruns.summary(d, "x")["n"] == 0


def test_other_host_running_is_lost_and_not_listed(db):
    import socket
    add(db, "mine", 10, state="running", exit_code=None, ended_at=None, wall_s=None, host=socket.gethostname())
    add(db, "alien", 11, state="running", exit_code=None, ended_at=None, wall_s=None, host="other-host")
    assert [r["run_uid"] for r in testruns.running(db)] == ["mine"]
    got = {r["run_uid"]: r["outcome"] for r in testruns.list_runs(db)}
    assert got == {"mine": "running", "alien": "lost"}


def test_command_is_remasked_on_output(db):
    add(db, "a", 1, command="curl -H 'api_key=\"supersecretvalue1\"' x")
    add(db, "b", 2, command="deploy ghp_" + "a" * 36, label="ghp_" + "b" * 36)
    rs = {r["run_uid"]: r for r in testruns.list_runs(db)}
    assert "supersecretvalue1" not in rs["a"]["command"] and "•••" in rs["a"]["command"]
    assert rs["b"]["command"] == testruns.HIDDEN and rs["b"]["label"] == testruns.HIDDEN


# ------------------------------------------------------------------ regressions (E6-06, ADR-018)

def seed(db, n, label="full", wall=100.0, cpu=200.0, start=1000, **kw):
    for i in range(n):
        add(db, f"b{kw.get('source', 'wrapper')}{kw.get('project', 'acme')}{label}{i}", start + i * 100, label=label, wall_s=wall, cpu_s=cpu, **kw)


def reg_of(db, uid, **kw):
    return {r["run_uid"]: r["regression"] for r in testruns.list_runs(db, **kw)}[uid]


def test_regression_needs_min_runs_of_same_key(db):
    seed(db, 2)
    add(db, "cur", 5000, label="full", wall_s=300.0, cpu_s=200.0)
    assert reg_of(db, "cur") is None                      # 2 < 3 previous runs
    add(db, "b3", 2500, label="full", wall_s=100.0, cpu_s=200.0)
    r = reg_of(db, "cur")
    assert r["wall_pct"] == 200 and r["cpu_pct"] is None and r["n"] == 3 and r["median_wall_s"] == 100.0


def test_regression_threshold_is_25_percent(db):
    seed(db, 3)
    add(db, "edge", 5000, label="full", wall_s=125.0, cpu_s=200.0)
    add(db, "under", 6000, label="full", wall_s=124.0, cpu_s=200.0)
    got = {r["run_uid"]: r["regression"] for r in testruns.list_runs(db)}
    assert got["edge"]["wall_pct"] == 25 and got["under"] is None


def test_regression_cpu_separately_from_wall(db):
    seed(db, 3)
    add(db, "cpu", 5000, label="full", wall_s=100.0, cpu_s=400.0)
    r = reg_of(db, "cpu")
    assert r["cpu_pct"] == 100 and r["wall_pct"] is None and r["median_cpu_s"] == 200.0


def test_short_run_needs_min_absolute_increase(db):
    seed(db, 3, wall=2.0, cpu=2.0)
    add(db, "short", 5000, label="full", wall_s=3.0, cpu_s=3.0)     # +50 % but only +1 s
    add(db, "long", 6000, label="full", wall_s=4.0, cpu_s=2.0)      # +100 % and +2 s
    got = {r["run_uid"]: r["regression"] for r in testruns.list_runs(db)}
    assert got["short"] is None
    assert got["long"]["wall_pct"] == 100 and got["long"]["cpu_pct"] is None


def test_min_absolute_increase_comes_from_rule(db):
    seed(db, 3, wall=2.0, cpu=2.0)
    add(db, "short", 5000, label="full", wall_s=3.0, cpu_s=2.0)
    rule = {"pct": 25.0, "runs": 10, "min_runs": 3, "min_abs_s": 0.5}
    assert reg_of(db, "short", rule=rule)["wall_pct"] == 50


def test_regression_window_is_last_n_runs(db):
    seed(db, 5, wall=1000.0, start=1000)                  # old slow runs fall out of the window
    for i in range(10):
        add(db, f"new{i}", 10_000 + i * 10, label="full", wall_s=100.0, cpu_s=200.0)
    add(db, "cur", 20_000, label="full", wall_s=140.0, cpu_s=200.0)
    r = reg_of(db, "cur")
    assert r["median_wall_s"] == 100.0 and r["n"] == 10 and r["wall_pct"] == 40
    rule = {"pct": 25.0, "runs": 3, "min_runs": 3, "min_abs_s": 2.0}
    assert reg_of(db, "cur", rule=rule)["n"] == 3


def test_baseline_only_earlier_runs(db):
    seed(db, 3)
    add(db, "cur", 1150, label="full", wall_s=300.0, cpu_s=200.0)   # only b0, b1 are earlier
    assert reg_of(db, "cur") is None


def test_wrapper_and_passive_do_not_mix(db):
    seed(db, 3, wall=100.0)
    for i in range(3):
        add(db, f"p{i}", 2000 + i, source="passive", exit_code=None, label="pytest", command="/x/python -m pytest",
            wall_s=300.0, cpu_s=200.0)
    add(db, "pcur", 5000, source="passive", exit_code=None, label="pytest", command="/x/python -m pytest",
        wall_s=320.0, cpu_s=200.0)
    add(db, "wcur", 5001, label="full", wall_s=320.0, cpu_s=200.0)
    got = {r["run_uid"]: r["regression"] for r in testruns.list_runs(db)}
    assert got["pcur"] is None                            # passive base median 300, not 100
    assert got["wcur"]["median_wall_s"] == 100.0 and got["wcur"]["wall_pct"] == 220


def test_passive_base_is_ended_by_command(db):
    for i in range(3):
        add(db, f"p{i}", 2000 + i * 10, source="passive", exit_code=None, label="pytest", command="/x/py -m pytest a",
            wall_s=50.0, cpu_s=50.0)
        add(db, f"q{i}", 2005 + i * 10, source="passive", exit_code=None, label="pytest", command="/x/py -m pytest b",
            wall_s=500.0, cpu_s=50.0)
    add(db, "cur", 5000, source="passive", exit_code=None, label="pytest", command="/x/py -m pytest a", wall_s=80.0, cpu_s=50.0)
    add(db, "other", 5001, source="passive", exit_code=None, label="pytest", command="/x/py -m pytest c", wall_s=80.0, cpu_s=50.0)
    got = {r["run_uid"]: r["regression"] for r in testruns.list_runs(db)}
    assert got["cur"]["wall_pct"] == 60 and got["cur"]["median_wall_s"] == 50.0
    assert got["other"] is None


def test_different_labels_projects_do_not_mix(db):
    seed(db, 3, label="full", wall=100.0)
    seed(db, 3, label="node", wall=10.0)
    seed(db, 3, label="full", wall=10.0, project="beta", start=3000)
    add(db, "cur", 5000, label="full", wall_s=120.0, cpu_s=200.0)
    assert reg_of(db, "cur") is None                      # 120 vs 100 is +20 %
    add(db, "cur2", 5100, label="node", wall_s=30.0, cpu_s=200.0)
    assert reg_of(db, "cur2")["wall_pct"] == 200


def test_wrapper_without_label_keys_by_command(db):
    for i in range(3):
        add(db, f"a{i}", 1000 + i * 10, wall_s=100.0, cpu_s=200.0, command="pytest -q")
        add(db, f"b{i}", 1005 + i * 10, wall_s=500.0, cpu_s=200.0, command="npm test")
    add(db, "cur", 5000, wall_s=200.0, cpu_s=200.0, command="pytest -q")
    assert reg_of(db, "cur")["wall_pct"] == 100


def test_failed_not_in_baseline_and_not_marked(db):
    seed(db, 3, wall=100.0)
    for i in range(5):
        add(db, f"f{i}", 2000 + i, label="full", exit_code=1, wall_s=1000.0, cpu_s=2000.0)
    add(db, "int", 3000, label="full", exit_code=130, interrupted=1, wall_s=1000.0)
    add(db, "lost", 3100, label="full", state="lost", exit_code=None, wall_s=1000.0)
    add(db, "ok", 5000, label="full", wall_s=110.0, cpu_s=200.0)
    add(db, "bad", 5100, label="full", exit_code=1, wall_s=900.0, cpu_s=2000.0)
    got = {r["run_uid"]: r["regression"] for r in testruns.list_runs(db)}
    assert got["ok"] is None and got["bad"] is None and got["int"] is None and got["lost"] is None
    assert all(got[f"f{i}"] is None for i in range(5))


def test_running_marked_live_by_wall(db, monkeypatch):
    seed(db, 3, wall=100.0)
    add(db, "run", 10_000, label="full", state="running", exit_code=None, ended_at=None, wall_s=None, cpu_s=None)
    monkeypatch.setattr(testruns.time, "time", lambda: 10_060.0)
    assert testruns.running(db)[0]["regression"] is None          # 60 s < 125 s
    monkeypatch.setattr(testruns.time, "time", lambda: 10_200.0)
    r = testruns.running(db)[0]["regression"]
    assert r["wall_pct"] == 100 and r["median_wall_s"] == 100.0 and r["n"] == 3 and r["cpu_pct"] is None


def test_running_without_enough_base_is_not_marked(db, monkeypatch):
    seed(db, 2)
    add(db, "run", 10_000, label="full", state="running", exit_code=None, ended_at=None, wall_s=None, cpu_s=None)
    monkeypatch.setattr(testruns.time, "time", lambda: 99_999.0)
    assert testruns.running(db)[0]["regression"] is None


def test_recent_finished_marks_regression(db):
    seed(db, 3, start=1_000_000)
    add(db, "cur", 1_001_000, label="full", wall_s=300.0, cpu_s=200.0, ended_at=1_001_300)
    r = testruns.recent_finished(db, 1_001_200, 5)
    assert r[0]["run_uid"] == "cur" and r[0]["regression"]["wall_pct"] == 200


def test_rows_without_project_or_key_not_marked(db):
    for i in range(3):
        add(db, f"n{i}", 1000 + i, project=None, label="full", wall_s=10.0)
        add(db, f"e{i}", 1100 + i, command="", label="", wall_s=10.0)
    add(db, "nop", 5000, project=None, label="full", wall_s=100.0)
    add(db, "nok", 5001, command="", label="", wall_s=100.0)
    got = {r["run_uid"]: r["regression"] for r in testruns.list_runs(db)}
    assert got["nop"] is None and got["nok"] is None


def test_null_metrics_ignored(db):
    seed(db, 3, wall=100.0, cpu=None)
    add(db, "cur", 5000, label="full", wall_s=100.0, cpu_s=900.0)
    assert reg_of(db, "cur") is None                      # no CPU base: no verdict


def test_regression_object_shape(db):
    seed(db, 3)
    add(db, "cur", 5000, label="full", wall_s=300.0, cpu_s=200.0)
    r = reg_of(db, "cur")
    assert set(r) == {"wall_pct", "cpu_pct", "median_wall_s", "median_cpu_s", "n"}


def test_regression_n_counts_only_runs_with_wall(db):
    seed(db, 3)
    for i in range(2):
        add(db, f"nw{i}", 1350 + i * 10, label="full", wall_s=None, cpu_s=200.0)
    add(db, "cur", 5000, label="full", wall_s=300.0, cpu_s=200.0)
    r = reg_of(db, "cur")
    assert r["n"] == 3 and r["median_wall_s"] == 100.0
