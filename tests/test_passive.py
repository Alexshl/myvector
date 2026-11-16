import json
import sqlite3

import pytest

from myvec import config, measure, nodeload, passive
from myvec import db as dbmod
from myvec.nodeload import ProcInfo, ProjectMap, Sampler
from myvec.passive import classify, is_measure

# ---------------------------------------------------------------- classify / is_measure

CASES = [
    # pytest
    (["pytest", "-q"], "pytest"),
    (["/home/u/p/.venv/bin/pytest", "tests/"], "pytest"),
    (["py.test"], "pytest"),
    (["python3.12", "-m", "pytest", "-x"], "pytest"),
    (["/usr/bin/python", "-X", "dev", "-m", "pytest"], "pytest"),
    (["uv", "run", "pytest", "-q"], "pytest"),
    (["uv", "run", "--python", "3.12", "python", "-m", "pytest"], "pytest"),
    (["uvx", "pytest"], "pytest"),
    (["poetry", "run", "pytest"], "pytest"),
    (["python", "/p/.venv/bin/pytest"], "pytest"),
    # php
    (["vendor/bin/phpunit", "--filter", "x"], "phpunit"),
    (["php", "vendor/bin/phpunit"], "phpunit"),
    (["php", "artisan", "test"], "artisan test"),
    (["php8.3", "-d", "memory_limit=-1", "artisan", "test", "--parallel"], "artisan test"),
    (["vendor/bin/paratest"], "paratest"),
    # js
    (["node", "/p/node_modules/.bin/vitest", "run"], "vitest"),
    (["node", "/p/node_modules/vitest/vitest.mjs", "run"], "vitest"),
    (["vitest", "--run"], "vitest"),
    (["npx", "vitest", "run"], "vitest"),
    (["pnpm", "exec", "vitest", "run"], "vitest"),
    (["node", "/p/node_modules/.bin/jest", "--ci"], "jest"),
    (["node", "/p/node_modules/jest/bin/jest.js"], "jest"),
    (["node", "/p/node_modules/jest-cli/bin/jest.js"], "jest"),
    (["npm", "test"], "npm test"),
    (["npm", "t"], "npm test"),
    (["npm", "run", "test"], "npm test"),
    (["pnpm", "test"], "pnpm test"),
    (["pnpm", "run", "test"], "pnpm test"),
    (["yarn", "test"], "yarn test"),
    (["bun", "test"], "bun test"),
    (["bun", "run", "test"], "bun test"),
    (["npm test", "", ""], "npm test"),             # npm retitles its process
    (["pnpm test"], "pnpm test"),
    (["uv", "run", "--", "pytest"], "pytest"),
    (["npx", "--", "vitest", "run"], "vitest"),
]

NEGATIVE = [
    ["vitest"], ["vitest", "watch"], ["vitest", "dev"], ["vitest", "--watch"], ["jest", "--watchAll"],
    ["npm", "test", "--", "--watch"], ["npm", "install"], ["npm", "run", "build"], ["pnpm", "dev"],
    ["python", "-m", "http.server"], ["python", "-c", "import pytest"], ["php", "artisan", "serve"],
    ["php", "artisan", "migrate"], ["myvec", "serve"], ["bash", "-c", "pytest"], ["node", "server.js"],
    ["pytest", "--collect-only"], ["python", "-m", "pytest", "--co", "-q"], ["pytest", "--version"], ["uv", "sync"], ["yarn", "build"], ["bun", "run", "dev"], [], ["pytest-watch"],
]


@pytest.mark.parametrize("argv,kind", CASES)
def test_classify_positive(argv, kind):
    assert classify(argv) == kind


@pytest.mark.parametrize("argv", NEGATIVE)
def test_classify_negative(argv):
    assert classify(argv) is None


def test_is_measure():
    assert is_measure(["/x/bin/myvec", "measure", "--", "pytest"])
    assert is_measure(["python3", "-m", "myvec", "measure", "--", "pytest"])
    assert is_measure(["python", "/x/bin/myvec", "measure", "--agent", "t", "--", "x"])
    assert is_measure(["uv", "run", "myvec", "measure", "--", "pytest"])
    assert not is_measure(["myvec", "serve"])
    assert not is_measure(["pytest", "measure"])
    assert not is_measure([])


# ---------------------------------------------------------------- fake system

class Tree:
    """A fake psutil: pid -> process; counts the lazy calls the detector makes."""

    def __init__(self):
        self.p: dict[int, dict] = {}
        self.calls = {"cmdline": [], "environ": [], "cwd": [], "details": []}

    def add(self, pid, ppid, name, argv, cwd="/p/a", ct=None, cpu=0.0, env=None):
        self.p[pid] = dict(ppid=ppid, name=name, argv=argv, cwd=cwd, ct=ct or 1000.0 + pid, cpu=cpu, env=env or {})

    def burn(self, pid, secs):
        self.p[pid]["cpu"] += secs

    def kill(self, *pids):
        for pid in pids:
            self.p.pop(pid, None)

    # -- source interface
    def system(self):
        return {"cores": [10.0] * 4, "cpu_pct": 10.0, "load": (1.0, 1.0, 1.0), "mem_used": 1, "mem_total": 2,
                "swap_used": 0, "freq_mhz": None, "temp_c": None}

    def procs(self):
        return [ProcInfo(pid, d["ct"], None, None, d["cpu"]) for pid, d in self.p.items()]

    def details(self, pid):
        self.calls["details"].append(pid)
        return self.p[pid]["ppid"], self.p[pid]["name"]

    def create_time(self, pid):
        return self.p[pid]["ct"]

    def cpu_time(self, pid):
        return self.p[pid]["cpu"]

    def cwd(self, pid):
        self.calls["cwd"].append(pid)
        return self.p[pid]["cwd"]

    def cmdline(self, pid):
        self.calls["cmdline"].append((pid, self.p[pid]["ct"]))
        return self.p[pid]["argv"]

    def environ_keys(self, pid, keys):
        self.calls["environ"].append(pid)
        return {k: v for k, v in self.p[pid]["env"].items() if k in keys}

    def host_info(self):
        return {"hostname": "h1", "cpu_model": "", "cores": 4, "ram_total": 1}


class Rig:
    def __init__(self, roots=(("/p/a", "/p/a"),), host="h1"):
        self.t = Tree()
        self.now = 1_000_000.0
        self.map = ProjectMap(roots)
        self.s = Sampler(self.t, self.map, clock=lambda: self.now, wall=lambda: self.now, thread_cpu=lambda: 0.0)
        self.events: list[dict] = []
        self.det = passive.Detector(self.t, self.map, lambda: host, 5.0)
        self.det.journal = self.events.append
        self.s.add_listener(self.det)

    def tick(self, dt=5.0):
        self.now += dt
        return self.s.sample()


def two_ticks(rig, pids, burn=2.0):
    rig.tick()
    for pid in pids:
        rig.t.burn(pid, burn)
    rig.tick()


def pytest_tree(rig, **kw):
    rig.t.add(10, 1, "bash", ["bash"], cwd="/p/a")
    rig.t.add(11, 10, "python", ["/p/.venv/bin/python", "-m", "pytest", "-q"], **kw)


# ---------------------------------------------------------------- detector

def test_basic_run_start_and_end():
    rig = Rig()
    pytest_tree(rig)
    rig.tick()                                   # baseline
    rig.t.burn(11, 3.0)
    rig.tick()
    [start] = rig.events
    assert (start["source"], start["state"], start["label"], start["project_path"]) == ("passive", "running", "pytest", "/p/a")
    assert start["run_uid"] == f"passive-h1-11-{int(1011.0 * 1000)}" and start["started_at"] == 1011.0
    assert start["pid"] == 11 and start["host"] == "h1" and start["command"] == "/p/.venv/bin/python -m pytest -q"
    rig.t.burn(11, 2.0)
    rig.tick()
    rig.t.kill(11)
    snap = rig.tick()
    [_, end] = rig.events
    assert end["state"] == "done" and end["run_uid"] == start["run_uid"] and end["ended_at"] == snap.ts
    assert end["wall_s"] == pytest.approx(snap.ts - 1011.0)
    assert end["cpu_s"] == pytest.approx(5.0)    # includes the CPU before the first delta
    assert end["peak_cores"] == pytest.approx(0.6)   # 3 s / 5 s
    assert end["avg_cores"] == pytest.approx(5.0 / end["wall_s"]) and end["n_samples"] == 2
    assert not {"exit_code", "signal", "peak_rss_bytes"} & {k for k, v in end.items() if v is not None}
    assert end["session_id"] is None and end["agent"] is None


def test_hierarchy_npm_sh_node_one_run():
    rig = Rig()
    rig.t.add(10, 1, "bash", ["bash"])
    rig.t.add(20, 10, "npm", ["npm", "test"], cpu=0.5)
    rig.t.add(21, 20, "sh", ["sh", "-c", "vitest run"])
    rig.t.add(22, 21, "node", ["node", "/p/a/node_modules/.bin/vitest", "run"])
    rig.t.add(23, 22, "node", ["node", "--worker"])      # a vitest worker: child of a member, no cmdline read
    rig.tick()
    rig.t.burn(22, 2.0)
    rig.t.burn(23, 4.0)
    rig.tick()
    [start] = rig.events
    assert start["label"] == "npm test" and start["command"] == "npm test" and start["pid"] == 20
    rig.t.burn(22, 1.0)
    rig.t.burn(23, 1.0)
    rig.tick()
    rig.t.kill(20, 21, 22, 23)
    rig.tick()
    [_, end] = rig.events
    assert end["cpu_s"] == pytest.approx(0.5 + 3.0 + 5.0)    # npm + vitest + worker
    assert end["peak_cores"] == pytest.approx((2.0 + 4.0) / 5.0)
    assert (23, rig.t.p.get(23, {}).get("ct", 1023.0)) not in rig.t.calls["cmdline"]
    assert 10 not in {c[0] for c in rig.t.calls["cmdline"]}   # the shell above the root is not even read


def test_npm_retitled_process_is_the_root():
    rig = Rig()
    rig.t.add(15, 1, "npm test", ["npm test", "", ""], cpu=0.2)     # what psutil reports for a real npm
    rig.t.add(16, 15, "sh", ["sh", "-c", "vitest run"])
    rig.t.add(17, 16, "node", ["node", "/p/a/node_modules/.bin/vitest", "run"])
    rig.tick()
    rig.t.burn(17, 2.0)
    rig.tick()
    [start] = rig.events
    assert start["pid"] == 15 and start["label"] == "npm test" and start["command"].startswith("npm test")


def test_uv_run_python_root_is_uv_and_xdist_workers_join():
    rig = Rig()
    rig.t.add(30, 1, "uv", ["uv", "run", "pytest", "-n", "2"])
    rig.t.add(31, 30, "python", ["/p/.venv/bin/python", "/p/.venv/bin/pytest", "-n", "2"])
    rig.t.add(32, 31, "python", ["python", "-c", "import execnet"])
    rig.tick()
    for pid in (31, 32):
        rig.t.burn(pid, 2.0)
    rig.tick()
    [start] = rig.events
    assert start["pid"] == 30 and start["label"] == "pytest" and start["command"].startswith("uv run pytest")
    assert len(rig.det.runs) == 1


def test_dedup_with_measure_ancestor():
    rig = Rig()
    rig.t.add(40, 1, "myvec", ["/x/bin/myvec", "measure", "--", "uv", "run", "pytest"])
    rig.t.add(41, 40, "uv", ["uv", "run", "pytest"])
    rig.t.add(42, 41, "python", ["python", "-m", "pytest"])
    rig.t.add(43, 42, "python", ["python", "-c", "worker"])
    rig.tick()
    for pid in (42, 43):
        rig.t.burn(pid, 2.0)
    rig.tick()
    rig.t.kill(40, 41, 42, 43)
    rig.tick()
    assert rig.events == [] and rig.det.owner == {}


def test_dedup_with_env_marker_without_measure_ancestor():
    rig = Rig()
    rig.t.add(50, 1, "python", ["python", "-m", "pytest"], env={"MYVEC_MEASURE_RUN": "abc", "SECRET_TOKEN": "x"})
    rig.tick()
    rig.t.burn(50, 2.0)
    rig.tick()
    assert rig.events == []


def test_env_only_whitelisted_keys_read_and_session_id_recorded():
    rig = Rig()
    rig.t.add(60, 1, "pytest", ["pytest"], env={"CLAUDE_CODE_SESSION_ID": "sess-9", "AWS_SECRET": "zzz"})
    asked = []
    orig = rig.t.environ_keys
    rig.t.environ_keys = lambda pid, keys: (asked.append(tuple(keys)), orig(pid, keys))[1]
    two_ticks(rig, [60])
    [start] = rig.events
    assert start["session_id"] == "sess-9" and "zzz" not in json.dumps(start)
    assert set(asked[0]) == {"MYVEC_MEASURE_RUN", "CLAUDE_CODE_SESSION_ID"}


def test_cwd_outside_projects_not_recorded_and_worktree_maps_to_project(tmp_path):
    rig = Rig()
    rig.t.add(70, 1, "pytest", ["pytest"], cwd="/tmp/elsewhere")
    two_ticks(rig, [70])
    assert rig.events == []
    # a linked worktree outside the project tree belongs to the main checkout
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    rig2 = Rig(roots=((str(main), str(main)),))
    rig2.t.add(71, 1, "pytest", ["pytest"], cwd=str(wt))
    two_ticks(rig2, [71])
    assert [e["project_path"] for e in rig2.events] == [str(main)]


def test_secrets_masked_in_command():
    rig = Rig()
    tok = "ghp_" + "a1B2c3D4" * 5
    rig.t.add(80, 1, "pytest", ["pytest", "API_KEY=hunter2hunter2hunter2"])
    rig.t.add(82, 1, "pytest", ["pytest", "-k", tok])
    pem = "-----BEGIN RSA PRIVATE KEY-----"
    rig.t.add(83, 1, "pytest", ["pytest", "--id", "AKI" + "A" + "IOSFODNN7EXAMPLE"])
    rig.t.add(81, 1, "pytest", ["pytest", "--key", pem])
    two_ticks(rig, [80, 81, 82, 83])
    cmds = {e["pid"]: e["command"] for e in rig.events}
    assert "hunter2" not in cmds[80] and "•••" in cmds[80]
    assert cmds[81] == measure.HIDDEN and cmds[82] == measure.HIDDEN
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(rig.events)


def test_cmdline_budget_once_per_key_and_only_busy_candidates():
    rig = Rig()
    rig.t.add(90, 1, "firefox", ["firefox"])                 # busy, but not a candidate name
    rig.t.add(91, 1, "python", ["python", "-m", "http.server"])    # busy candidate, not a test: negative cache
    rig.t.add(92, 1, "node", ["node", "x.js"])               # idle candidate (never burns)
    rig.t.add(93, 1, "pytest", ["pytest"])
    rig.tick()
    for _ in range(4):
        for pid in (90, 91, 93):
            rig.t.burn(pid, 2.0)
        rig.tick()
    asked = [c[0] for c in rig.t.calls["cmdline"]]
    assert sorted(asked) == [91, 93]                         # one call each, none for 90 / 92
    assert len(set(rig.t.calls["cmdline"])) == len(rig.t.calls["cmdline"])
    # a reused pid (new create_time) is read again
    rig.t.kill(91)
    rig.tick()
    rig.t.add(91, 1, "python", ["python", "-m", "http.server"], ct=5000.0)
    rig.tick()
    rig.t.burn(91, 2.0)
    rig.tick()
    assert (91, 5000.0) in rig.t.calls["cmdline"]


def test_restart_same_run_uid_and_lost_without_daemon():
    rig = Rig()
    pytest_tree(rig)
    two_ticks(rig, [11])
    first = rig.events[0]["run_uid"]
    rig2 = Rig()
    rig2.t = rig.t
    rig2.s.source = rig.t
    rig2.det.source = rig.t
    two_ticks(rig2, [11])
    assert rig2.events[0]["run_uid"] == first and rig2.events[0]["started_at"] == rig.events[0]["started_at"]
    # the same uid twice in the journal folds into one row; a dead pid with no daemon around -> lost
    import socket
    config.ensure_home()
    for e in (rig.events[0], rig2.events[0]):
        measure._journal(dict(e, host=socket.gethostname(), pid=2 ** 22 + 7, started_at=1.0))
    db = sqlite3.connect(str(config.db_path()), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(dbmod.TEST_RUNS_SCHEMA)
    measure.fold(db, now=1000.0)
    rows = db.execute("SELECT state, source FROM test_runs").fetchall()
    assert [tuple(r) for r in rows] == [("lost", "passive")]


def test_detector_does_not_open_index_db(monkeypatch):
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (opened.append(a), real(*a, **k))[1])
    rig = Rig()
    rig.det.journal = measure._journal            # the real journal, but still no database
    pytest_tree(rig)
    two_ticks(rig, [11])
    rig.t.kill(11)
    rig.tick()
    assert opened == []
    lines = [json.loads(x) for x in config.test_runs_path().read_text().splitlines()]
    assert [x["state"] for x in lines] == ["running", "done"]


def test_fold_of_detector_rows_gives_done_row():
    rig = Rig()
    rig.det.journal = measure._journal
    pytest_tree(rig)
    two_ticks(rig, [11], burn=4.0)
    rig.t.kill(11)
    rig.tick()
    config.ensure_home()
    db = sqlite3.connect(str(config.db_path()), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(dbmod.TEST_RUNS_SCHEMA)
    assert measure.fold(db) == 2
    [r] = db.execute("SELECT * FROM test_runs").fetchall()
    assert (r["state"], r["source"], r["label"], r["exit_code"]) == ("done", "passive", "pytest", None)
    assert r["wall_s"] > 0 and r["cpu_s"] == pytest.approx(4.0) and r["peak_cores"] > 0


def test_listener_failure_does_not_break_sampler():
    rig = Rig()
    pytest_tree(rig)
    rig.t.cmdline = lambda pid: (_ for _ in ()).throw(RuntimeError("boom"))
    two_ticks(rig, [11])     # no exception; AccessDenied-style failures read as an empty cmdline
    assert rig.events == []


# ---------------------------------------------------------------- wiring

def test_runner_attaches_detector_and_can_be_disabled(tmp_path):
    r = nodeload.Runner(Tree(), ProjectMap([]), db_path=tmp_path / "load.db")
    assert isinstance(r.passive, passive.Detector) and r.passive in r.sampler.listeners
    off = nodeload.Runner(Tree(), ProjectMap([]), db_path=tmp_path / "l2.db", passive_tests=False)
    assert off.passive is None and off.sampler.listeners == []
    r.step()
    r.step()


def test_lifespan_with_detector_starts_and_stops(ws, monkeypatch):
    from starlette.testclient import TestClient

    from myvec import daemon

    class Src(Tree):
        pass

    monkeypatch.setattr(nodeload, "SystemSource", lambda sensor="": Src())
    st = daemon.DaemonState(7899, "tok", background=True)
    try:
        with TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7899"):
            assert st.load is not None and st.load.passive is not None and st.load._thread.is_alive()
            thread = st.load._thread
        assert not thread.is_alive()
    finally:
        st.close()


def test_journal_concurrent_writers_and_folder_lose_nothing():
    import threading

    config.ensure_home()
    n_writers, per = 4, 150
    stop = threading.Event()
    got: list[str] = []

    def writer(w):
        for i in range(per):
            measure._journal({"run_uid": f"w{w}-{i}", "source": "passive", "state": "running"})

    def folder():
        while True:
            done = stop.is_set()
            files = measure._claim(config.test_runs_path())
            got.extend(r["run_uid"] for r in measure._read_rows(files))
            for f in files:
                f.unlink()
            if done:
                return

    ft = threading.Thread(target=folder)
    ws_ = [threading.Thread(target=writer, args=(w,)) for w in range(n_writers)]
    ft.start()
    for t in ws_:
        t.start()
    for t in ws_:
        t.join()
    stop.set()
    ft.join()
    assert len(got) == n_writers * per and len(set(got)) == n_writers * per
