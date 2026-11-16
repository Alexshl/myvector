import json
import os
import sqlite3
from collections import namedtuple
from types import SimpleNamespace

import pytest

from myvec import config, nodeload
from myvec.nodeload import MinuteAggregator, ProcInfo, ProjectMap, Runner, Sampler, pick_temp

T = namedtuple("T", "label current")


class FakeSource:
    def __init__(self, ncores=4):
        self.cores = [0.0] * ncores
        self.procs_list: list[ProcInfo] = []
        self.cwds: dict[int, object] = {}
        self.cwd_calls: list[int] = []
        self.temp = 50.0

    def system(self):
        return {"cores": list(self.cores), "cpu_pct": sum(self.cores) / len(self.cores), "load": (1.0, 2.0, 3.0),
                "mem_used": 100, "mem_total": 1000, "swap_used": 5, "freq_mhz": 3000.0, "temp_c": self.temp}

    def procs(self):
        return list(self.procs_list)

    def cwd(self, pid):
        self.cwd_calls.append(pid)
        v = self.cwds.get(pid)
        if isinstance(v, Exception):
            raise v
        return v

    def cmdline(self, pid):
        return ["x", str(pid)]

    def host_info(self):
        return {"hostname": "h1", "cpu_model": "M", "cores": len(self.cores), "ram_total": 1000}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def mk(roots=(("/p/a", "/p/a"), ("/p/b", "/p/b")), **kw):
    src, clk = FakeSource(), Clock()
    s = Sampler(src, ProjectMap(roots), clock=clk, wall=clk, thread_cpu=lambda: 0.0)
    return src, clk, s


def tick(clk, s, dt=5.0):
    clk.t += dt
    return s.sample()


def test_delta_and_baseline():
    src, clk, s = mk()
    src.procs_list = [ProcInfo(10, 1.0, 1, "python", 100.0)]
    src.cwds[10] = "/p/a/sub"
    src.cores = [50.0] * 4
    first = s.sample()
    assert "/p/a" not in first.projects            # first sight is only a baseline
    src.procs_list = [ProcInfo(10, 1.0, 1, "python", 102.5)]
    snap = tick(clk, s)
    assert snap.projects["/p/a"]["cpu_pct"] == pytest.approx(50.0)   # 2.5 s / 5 s = 50% of one core
    assert snap.projects["/p/a"]["top_name"] == "python"
    # other = total - projects, never negative
    assert snap.projects[None]["cpu_pct"] == pytest.approx(200.0 - 50.0)
    src.cores = [1.0] * 4
    src.procs_list = [ProcInfo(10, 1.0, 1, "python", 105.0)]
    assert tick(clk, s).projects[None]["cpu_pct"] == 0.0


def test_access_denied_goes_to_other():
    src, clk, s = mk()
    src.procs_list = [ProcInfo(1, 1.0, 0, "root", None), ProcInfo(2, 1.0, 0, "p", 1.0)]
    src.cwds[2] = PermissionError("denied")
    s.sample()
    src.procs_list = [ProcInfo(1, 1.0, 0, "root", None), ProcInfo(2, 1.0, 0, "p", 3.0)]
    src.cores = [100.0] * 4
    snap = tick(clk, s)
    assert set(snap.projects) == {None}
    assert snap.projects[None]["cpu_pct"] == pytest.approx(400.0)


def test_cwd_resolved_once_and_only_with_cpu():
    src, clk, s = mk()
    src.procs_list = [ProcInfo(1, 1.0, 0, "idle", 5.0), ProcInfo(2, 1.0, 0, "busy", 5.0)]
    src.cwds = {1: "/p/a", 2: "/p/b"}
    s.sample()
    for i in range(1, 4):
        src.procs_list = [ProcInfo(1, 1.0, 0, "idle", 5.0), ProcInfo(2, 1.0, 0, "busy", 5.0 + i)]
        tick(clk, s)
    assert src.cwd_calls == [2]


def test_gone_pid_and_pid_reuse():
    src, clk, s = mk()
    src.cwds = {7: "/p/a"}
    src.procs_list = [ProcInfo(7, 1.0, 0, "old", 1.0)]
    s.sample()
    src.procs_list = [ProcInfo(7, 1.0, 0, "old", 2.0)]
    tick(clk, s)
    assert (7, 1.0) in s._proj
    src.cwds = {7: "/p/b"}
    src.procs_list = [ProcInfo(7, 9.0, 0, "new", 50.0)]   # same pid, other create_time: a new process, baseline
    snap = tick(clk, s)
    assert (7, 1.0) not in s._proj and "/p/b" not in snap.projects
    src.procs_list = [ProcInfo(7, 9.0, 0, "new", 51.0)]
    assert tick(clk, s).projects["/p/b"]["cpu_pct"] == pytest.approx(20.0)


def test_parent_fallback():
    src, clk, s = mk()
    src.cwds = {1: "/p/a", 2: "/nowhere"}
    src.procs_list = [ProcInfo(1, 1.0, 0, "parent", 1.0), ProcInfo(2, 1.0, 1, "child", 1.0)]
    s.sample()
    src.procs_list = [ProcInfo(1, 1.0, 0, "parent", 2.0), ProcInfo(2, 1.0, 1, "child", 2.0)]
    snap = tick(clk, s)
    assert snap.projects["/p/a"]["n"] == 2


def test_worktree_outside_tree(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..")
    wt = tmp_path / "elsewhere" / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    pm = ProjectMap([("proj", str(main))])
    assert pm.project_for(str(wt / "src")) == "proj"
    assert pm.project_for(str(main / "x")) == "proj"
    assert pm.project_for(str(tmp_path / "other")) is None


def test_deepest_root_wins():
    pm = ProjectMap([("/p", "/p"), ("/p/inner", "/p/inner")])
    assert pm.project_for("/p/inner/x") == "/p/inner"
    assert pm.project_for("/p/other") == "/p"


def test_pick_temp():
    assert pick_temp({"k10temp": [T("Tccd1", 40.0), T("Tctl", 55.0)], "coretemp": [T("Package id 0", 60.0)]}) == 55.0
    assert pick_temp({"coretemp": [T("Core 0", 41.0), T("Package id 0", 60.0)]}) == 60.0
    assert pick_temp({"cpu_thermal": [T("", 48.0)]}) == 48.0
    assert pick_temp({}) is None and pick_temp(None) is None
    assert pick_temp({"nvme": [T("Composite", 30.0)]}) is None
    assert pick_temp({"k10temp": [T("Tctl", 55.0)], "custom": [T("x", 33.0)]}, "custom") == 33.0


def test_listener_lazy_and_failure_isolated():
    src, clk, s = mk()
    got = []
    s.add_listener(lambda snap, views, gone: (_ for _ in ()).throw(RuntimeError("boom")))
    s.add_listener(lambda snap, views, gone: got.append((views, gone)))
    src.cwds = {1: "/p/a"}
    src.procs_list = [ProcInfo(1, 1.0, 0, "t", 1.0)]
    s.sample()
    src.procs_list = [ProcInfo(1, 1.0, 0, "t", 2.0)]
    tick(clk, s)
    views, gone = got[-1]
    assert views[0].project == "/p/a" and views[0].cmdline() == ["x", "1"] and views[0].cwd() == "/p/a"
    src.procs_list = []
    tick(clk, s)
    assert got[-1][1] == {(1, 1.0)}


def snapshots(n, start=60_000.0, step=5.0, cpu=40.0, proj_cpu=80.0):
    out = []
    for i in range(n):
        out.append(nodeload.Snapshot(
            start + i * step, cpu, [cpu] * 2, (1.0, 1.0, 1.0), 100, 1000, 1, 50.0 + i, 3000.0,
            {"/p/a": {"cpu_pct": proj_cpu, "n": 2, "top_name": "py", "top_pid": 9},
             "/p/low": {"cpu_pct": 0.1, "n": 1, "top_name": "x", "top_pid": 1},
             None: {"cpu_pct": 5.0, "n": 0, "top_name": None, "top_pid": None}}))
    return out


def test_aggregator_minute_and_flush(tmp_path):
    db = nodeload.connect(tmp_path / "load.db")
    agg = MinuteAggregator("h1")
    for s in snapshots(13, start=60_000.0):   # 60000 is a minute boundary: 12 in minute A, 1 in minute B
        agg.add(s)
    assert len(agg.ready) == 1
    assert agg.flush(db) == 1 and not agg.ready
    m = db.execute("SELECT * FROM node_minutes").fetchone()
    assert m["n"] == 12 and m["cpu_avg"] == pytest.approx(40.0) and m["host"] == "h1"
    assert json.loads(m["cores_avg"]) == [40.0, 40.0] and m["temp_max"] == pytest.approx(61.0)
    assert db.execute("SELECT count(*) FROM node_samples").fetchone()[0] == 12
    raw = {r[0] for r in db.execute("SELECT project_path FROM node_proc_samples")}
    assert "/p/low" not in raw and "/p/a" in raw and "" in raw
    pm = {r["project_path"]: r["cpu_avg"] for r in db.execute("SELECT * FROM node_proc_minutes")}
    assert pm["/p/a"] == pytest.approx(80.0) and pm[""] == pytest.approx(5.0) and "/p/low" not in pm


def test_flush_buffer_when_db_busy(tmp_path):
    path = tmp_path / "load.db"
    src, clk = FakeSource(), Clock()
    r = Runner(src, ProjectMap(), db_path=path, clock=clk, wall=clk)
    r._db = nodeload.connect(path)
    r._db.execute("PRAGMA busy_timeout=50")
    blocker = sqlite3.connect(str(path), timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    for _ in range(14):   # 70 s: crosses a minute boundary
        clk.t += 5
        r.step()
    assert len(r.agg.ready) >= 1                     # sampling went on, the batch is buffered
    blocker.execute("ROLLBACK")
    clk.t += 5
    r.step()
    assert not r.agg.ready
    assert r._db.execute("SELECT count(*) FROM node_minutes").fetchone()[0] >= 1


def test_retention(tmp_path):
    db = nodeload.connect(tmp_path / "load.db")
    now = 10_000_000.0
    db.executemany("INSERT INTO node_samples(ts, cpu_pct) VALUES(?,?)", [(now - 25 * 3600, 1), (now - 3600, 2)])
    db.executemany("INSERT INTO node_proc_samples(ts, project_path, cpu_pct) VALUES(?,?,?)",
                   [(now - 25 * 3600, "a", 1), (now - 10, "a", 1)])
    db.executemany("INSERT INTO node_minutes(minute, n) VALUES(?,?)",
                   [(int((now - 31 * 86400) // 60), 1), (int((now - 86400) // 60), 1)])
    db.executemany("INSERT INTO node_proc_minutes(minute, project_path) VALUES(?,?)",
                   [(int((now - 31 * 86400) // 60), "a"), (int((now - 60) // 60), "a")])
    nodeload.prune(db, now, 24.0, 30.0)
    for t in ("node_samples", "node_proc_samples", "node_minutes", "node_proc_minutes"):
        assert db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == (1 if t != "x" else 0), t


def test_series_periods(tmp_path):
    db = nodeload.connect(tmp_path / "load.db")
    agg = MinuteAggregator("h")
    snaps = snapshots(12 * 70, start=60_000.0)   # 70 minutes
    for s in snaps:
        agg.add(s)
        if agg.ready:
            agg.flush(db)
    now = snaps[-1].ts + 5
    one = nodeload.series(db, "1h", now)
    assert one["step_s"] == 5 and 0 < len(one["points"]) <= 720
    day = nodeload.series(db, "24h", now)
    assert len(day["points"]) == 69 and day["points"][0]["cpu_pct"] == pytest.approx(40.0)
    mon = nodeload.series(db, "30d", now)
    assert 2 <= len(mon["points"]) <= 4 and mon["step_s"] == 1800
    with pytest.raises(ValueError):
        nodeload.series(db, "7d", now)


def test_top_projects_window():
    snaps = snapshots(12)
    out = nodeload.top_projects(snaps, snaps[0].ts)
    assert out[0]["project"] == "/p/a" and out[0]["cpu_pct"] == pytest.approx(80.0) and out[0]["top_name"] == "py"
    assert out[-1]["project"] is None
    assert nodeload.top_projects(snaps, snaps[-1].ts + 100) == []


def test_runner_api_and_roots(tmp_path):
    src, clk = FakeSource(), Clock()
    r = Runner(src, ProjectMap(), db_path=tmp_path / "load.db", clock=clk, wall=clk)
    r.set_roots([("/p/a", "/p/a")])
    src.cwds = {1: "/p/a"}
    src.procs_list = [ProcInfo(1, 1.0, 0, "py", 0.0)]
    r.step()
    for i in range(1, 15):
        src.procs_list = [ProcInfo(1, 1.0, 0, "py", float(i))]
        clk.t += 5
        src.cores = [50.0] * 4
        r.step()
    cur = r.current()
    assert cur["host"]["hostname"] == "h1" and cur["snapshot"]["cpu_pct"] == 50.0
    assert cur["projects"][0]["project"] == "/p/a" and cur["temp_max_24h"] == 50.0
    assert r.series("24h")["period"] == "24h"
    st = r.status()
    assert st["enabled"] and st["last_ts"] == r.last.ts and 0.0 <= st["sampler_cpu_pct"] < 1.0


def test_migration_idempotent(tmp_path):
    p = tmp_path / "load.db"
    nodeload.connect(p).close()
    db = nodeload.connect(p)
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    assert config.load_db_path().name == "load.db"


def test_config_keys(tmp_path):
    cfg = config.load_config()
    assert cfg.load_enabled and cfg.load_sample_s == 5.0 and cfg.load_raw_hours == 24.0
    (config.home() / "config.toml").write_text("load_enabled = false\nload_sample_s = 2\nload_temp_sensor = \"x\"\n")
    cfg = config.load_config()
    assert not cfg.load_enabled and cfg.load_sample_s == 2.0 and cfg.load_temp_sensor == "x"


def test_buffer_is_capped():
    agg = MinuteAggregator("h")
    for s in snapshots(12 * (nodeload.BUFFER_MINUTES + 10), start=60_000.0):
        agg.add(s)
    assert len(agg.ready) == nodeload.BUFFER_MINUTES


def test_interval_clamp():
    r = Runner(FakeSource(), ProjectMap(), interval=0.0)
    assert r.interval == nodeload.MIN_INTERVAL
    (config.home() / "config.toml").write_text("load_sample_s = 0.1\n")
    assert config.load_config().load_sample_s == 1.0


def test_series_and_current_on_missing_db(tmp_path):
    path = tmp_path / "none" / "load.db"
    r = Runner(FakeSource(), ProjectMap(), db_path=path)
    assert r.series("24h")["points"] == []
    assert r.current()["temp_max_24h"] is None
    assert not path.exists() and not path.parent.exists()
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    r2 = Runner(FakeSource(), ProjectMap(), db_path=empty)
    assert r2.series("1h")["points"] == []


def test_current_temp_max_24h_from_db(tmp_path):
    src, clk = FakeSource(), Clock()
    path = tmp_path / "load.db"
    db = nodeload.connect(path)
    db.execute("INSERT INTO node_minutes(minute, n, temp_max) VALUES(?,?,?)", (int((clk.t - 3600) // 60), 1, 77.0))
    db.close()
    r = Runner(src, ProjectMap(), db_path=path, clock=clk, wall=clk)
    clk.t += 5
    r.step()
    assert r.current()["temp_max_24h"] == 77.0


def test_loop_survives_step_exception_and_flushes_on_stop(tmp_path):
    import time as _t

    class Boom(FakeSource):
        n = 0

        def system(self):
            Boom.n += 1
            if Boom.n == 1:
                raise RuntimeError("boom")
            return super().system()

    path = tmp_path / "load.db"
    r = Runner(Boom(), ProjectMap(), interval=1.0, db_path=path)
    r.start()
    t0 = _t.time()
    while r.last is None and _t.time() - t0 < 10:
        _t.sleep(0.05)
    assert r.last is not None                      # the first step raised, the loop went on
    r.stop()
    assert not r._thread.is_alive()
    db = nodeload.connect(path, readonly=True)
    assert db.execute("SELECT count(*) FROM node_samples").fetchone()[0] >= 1   # stop() flushed the open minute


def test_concurrent_set_roots(tmp_path):
    import threading

    src, clk = FakeSource(), Clock()
    r = Runner(src, ProjectMap(), db_path=tmp_path / "load.db", clock=clk, wall=clk)
    src.cwds = {i: "/p/a" for i in range(1, 30)}
    stop = threading.Event()
    errs = []

    def churn():
        i = 0
        while not stop.is_set():
            r.set_roots([(f"/p/{i % 3}", f"/p/{i % 3}"), ("/p/a", "/p/a")])
            i += 1

    t = threading.Thread(target=churn)
    t.start()
    try:
        for k in range(1, 60):
            src.procs_list = [ProcInfo(i, 1.0, 0, "x", float(k)) for i in range(1, 30)]
            clk.t += 5
            try:
                r.step()
            except Exception as e:  # noqa: BLE001
                errs.append(e)
    finally:
        stop.set()
        t.join()
    assert not errs


def test_runner_host_info_failure_tolerated(tmp_path):
    class Bad(FakeSource):
        def host_info(self):
            raise OSError("no sysctl")

    r = Runner(Bad(), ProjectMap(), db_path=tmp_path / "load.db")
    r.step()
    assert r.current()["host"]["hostname"]


# ------------------------------------------------------------------ CPU alert (E6-06, ADR-018)

def series(cpus, start=60_000.0, step=5.0, projects=None):
    """Snapshots with the given cpu_pct per tick; `projects` maps tick index -> projects dict."""
    out = []
    for i, c in enumerate(cpus):
        pj = (projects or {}).get(i, {"/p/a": {"cpu_pct": c * 2, "n": 1, "top_name": "php", "top_pid": 7}})
        out.append(nodeload.Snapshot(start + i * step, c, [c] * 2, (1.0, 1.0, 1.0), 1, 2, 0, None, None, pj))
    return out


def alert(snaps, pct=85.0, minutes=5.0, interval=5.0, now=None):
    return nodeload.cpu_alert(snaps, pct, minutes, interval, snaps[-1].ts if now is None else now)


def test_alert_none_for_empty_or_cool():
    assert nodeload.cpu_alert([], 85, 5, 5.0, 0.0) is None
    assert alert(series([10.0] * 100)) is None


def test_alert_not_before_duration_and_active_at_exactly_5_min():
    assert alert(series([90.0] * 60)) is None              # 295 s
    a = alert(series([90.0] * 61))                         # exactly 300 s
    assert a is not None and a["minutes"] == pytest.approx(5.0) and a["since"] == 60_000.0 and a["pct"] == 85.0


def test_alert_threshold_is_inclusive():
    assert alert(series([85.0] * 61)) is not None
    assert alert(series([84.9] * 61)) is None


def test_alert_duration_comes_from_argument():
    assert alert(series([90.0] * 13), minutes=1.0) is not None
    assert alert(series([90.0] * 13), minutes=2.0) is None


def test_alert_short_dip_keeps_series():
    cpus = [90.0] * 30 + [10.0] * 2 + [90.0] * 29           # 10 s dip
    a = alert(series(cpus))
    assert a is not None and a["since"] == 60_000.0


def test_alert_long_dip_resets_series():
    cpus = [90.0] * 40 + [10.0] * 5 + [90.0] * 30           # 25 s dip, only 150 s after it
    assert alert(series(cpus)) is None
    a = alert(series([90.0] * 40 + [10.0] * 5 + [90.0] * 61))
    assert a is not None and a["since"] == 60_000.0 + 45 * 5.0


def test_alert_drop_below_threshold_at_the_end_resets():
    assert alert(series([90.0] * 80 + [10.0] * 5)) is None  # newest is cool for 20 s


def test_alert_time_gap_resets_series():
    snaps = series([90.0] * 40) + series([90.0] * 30, start=60_000.0 + 40 * 5.0 + 120.0)
    assert alert(snaps) is None                              # a sleep gap splits it


def test_alert_stale_newest_snapshot_is_not_active():
    snaps = series([90.0] * 80)
    assert alert(snaps, now=snaps[-1].ts + 10.0) is not None
    assert alert(snaps, now=snaps[-1].ts + 16.0) is None     # > 3 intervals


def test_alert_culprit_is_heaviest_project_and_process():
    pj = {"/p/a": {"cpu_pct": 100.0, "n": 1, "top_name": "node", "top_pid": 1},
          "/p/b": {"cpu_pct": 610.0, "n": 3, "top_name": "php", "top_pid": 42},
          None: {"cpu_pct": 900.0, "n": 0, "top_name": None, "top_pid": None}}
    a = alert(series([90.0] * 61, projects={i: pj for i in range(61)}))
    assert a["project"] == "/p/b" and a["top_name"] == "php" and a["top_pid"] == 42 and a["cores"] == pytest.approx(6.1)


def test_alert_only_unmapped_load_has_no_project():
    pj = {None: {"cpu_pct": 400.0, "n": 0, "top_name": None, "top_pid": None}}
    a = alert(series([90.0] * 61, projects={i: pj for i in range(61)}))
    assert a is not None and a["project"] is None


def test_runner_status_and_current_carry_alert(tmp_path):
    src, clk = FakeSource(), Clock()
    r = Runner(src, ProjectMap(), db_path=tmp_path / "load.db", clock=clk, wall=clk, passive_tests=False,
               alert_pct=50.0, alert_minutes=1.0)
    r.set_roots([("/p/a", "/p/a")])
    src.cwds = {1: "/p/a"}
    src.cores = [90.0] * 4
    src.procs_list = [ProcInfo(1, 1.0, 0, "py", 0.0)]
    r.step()
    assert r.status()["alert"] is None and r.status()["alert_rule"] == {"pct": 50.0, "minutes": 1.0}
    for i in range(1, 16):
        src.procs_list = [ProcInfo(1, 1.0, 0, "py", float(i) * 4)]
        clk.t += 5
        r.step()
    for d in (r.status(), r.current()):
        assert d["alert"]["project"] == "/p/a" and d["alert"]["top_name"] == "py"
        assert d["alert_rule"] == {"pct": 50.0, "minutes": 1.0}
    clk.t += 60                                              # sampler stopped: stale
    assert r.status()["alert"] is None


def test_alert_path_never_signals_processes():
    import inspect
    for fn in (nodeload.cpu_alert, Runner.alert_info):
        src = inspect.getsource(fn)
        assert "kill" not in src and "terminate" not in src and "send_signal" not in src


# ------------------------------------------------------------------ config (E6-06)

def write_cfg(text):
    config.ensure_home()
    (config.home() / "config.toml").write_text(text)
    return config.load_config()


def test_alert_config_defaults_and_read():
    cfg = config.load_config()
    assert (cfg.load_alert_pct, cfg.load_alert_minutes) == (85.0, 5.0)
    assert (cfg.test_regression_pct, cfg.test_regression_runs, cfg.test_regression_min_runs) == (25.0, 10, 3)
    assert cfg.test_regression_min_abs_s == 2.0
    cfg = write_cfg("load_alert_pct = 70\nload_alert_minutes = 2.5\ntest_regression_pct = 40\n"
                    "test_regression_runs = 20\ntest_regression_min_runs = 5\ntest_regression_min_abs_s = 1\n")
    assert (cfg.load_alert_pct, cfg.load_alert_minutes) == (70.0, 2.5)
    assert (cfg.test_regression_pct, cfg.test_regression_runs, cfg.test_regression_min_runs) == (40.0, 20, 5)
    assert cfg.test_regression_min_abs_s == 1.0


def test_alert_config_is_clamped():
    cfg = write_cfg("load_alert_pct = 500\nload_alert_minutes = 600\ntest_regression_runs = 999\ntest_regression_min_runs = 999\n")
    assert cfg.load_alert_pct == 100.0 and cfg.load_alert_minutes == 55.0
    assert cfg.test_regression_runs == 50 and cfg.test_regression_min_runs == 50
    cfg = write_cfg("load_alert_pct = 0\nload_alert_minutes = 0\ntest_regression_pct = -5\ntest_regression_runs = 1\n"
                    "test_regression_min_runs = 0\ntest_regression_min_abs_s = -3\n")
    assert cfg.load_alert_pct == 1.0 and cfg.load_alert_minutes == 1.0
    assert cfg.test_regression_pct == 25.0 and cfg.test_regression_runs == 3 and cfg.test_regression_min_runs == 1
    assert cfg.test_regression_min_abs_s == 0.0


def test_min_runs_never_exceeds_runs():
    cfg = write_cfg("test_regression_runs = 4\ntest_regression_min_runs = 9\n")
    assert cfg.test_regression_min_runs == 4


def test_default_config_text_roundtrips_alert_keys():
    text = config.default_config_text()
    for k in ("load_alert_pct = 85", "load_alert_minutes = 5", "test_regression_pct = 25", "test_regression_runs = 10",
              "test_regression_min_runs = 3", "test_regression_min_abs_s = 2"):
        assert k in text
    cfg = write_cfg(text)
    assert cfg.load_alert_pct == 85.0 and cfg.test_regression_runs == 10


def test_alert_minutes_fit_the_snapshot_ring():
    cfg = write_cfg("load_sample_s = 1\nload_alert_minutes = 40\n")
    assert cfg.load_alert_minutes == nodeload.RING / 60 - 1   # 720 one-second snapshots = 12 min, minus a minute of slack
    cfg = write_cfg("load_sample_s = 5\nload_alert_minutes = 40\n")
    assert cfg.load_alert_minutes == 40.0


def test_alert_dip_of_one_slow_interval_keeps_the_series():
    snaps = series([90.0] * 10 + [10.0] + [90.0] * 10, step=20.0)    # the dip lasts one interval (20 s > grace 15 s)
    a = alert(snaps, minutes=5.0, interval=20.0)
    assert a is not None and a["since"] == snaps[0].ts


# ------------------------------------------------------------------ E6-10 idle CPU

def test_system_source_proc_matches_psutil():
    psutil = pytest.importorskip("psutil")
    src = nodeload.SystemSource()
    if not src._fast_ok:
        pytest.skip("no /proc")
    mine = {p.pid: p for p in src._procs_proc()}[os.getpid()]
    ref = psutil.Process()
    t = ref.cpu_times()
    assert abs(mine.create_time - ref.create_time()) < 0.02
    assert abs(mine.cpu_time - (t.user + t.system)) < 0.1
    again = {p.pid: p for p in src.procs()}[os.getpid()]
    assert again.create_time == mine.create_time   # a stable key
    assert mine.name is None and mine.ppid is None


def test_system_source_falls_back_to_psutil(monkeypatch):
    pytest.importorskip("psutil")
    src = nodeload.SystemSource()
    monkeypatch.setattr(src, "_procs_proc", lambda: (_ for _ in ()).throw(PermissionError("x")))
    src._fast_ok = True
    pids = {p.pid for p in src.procs()}
    assert os.getpid() in pids and src._fast_ok is False


def test_system_reads_freq_and_temp_every_sixth_call(monkeypatch):
    pytest.importorskip("psutil")
    src = nodeload.SystemSource()
    calls = {"slow": 0}
    orig = src._read_slow

    def counted():
        calls["slow"] += 1
        return orig()

    monkeypatch.setattr(src, "_read_slow", counted)
    for _ in range(12):
        src.system()
    assert calls["slow"] == 2


def test_create_time_matches_procs_and_survives_boot_time_jump(monkeypatch):
    psutil = pytest.importorskip("psutil")
    src = nodeload.SystemSource()
    if not src._fast_ok:
        pytest.skip("no /proc")
    me = os.getpid()
    assert src.create_time(me) == {p.pid: p for p in src.procs()}[me].create_time
    monkeypatch.setattr(psutil, "boot_time", lambda: src._boot + 3.0)   # clock step after the source was made
    assert src.create_time(me) == {p.pid: p for p in src.procs()}[me].create_time
