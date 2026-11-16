import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from myvec import config, measure
from myvec import db as dbmod

BURN = "import time\nt=time.process_time()\nwhile time.process_time()-t<{s}: pass\n"   # CPU time, not wall: stable under load


def make_db() -> sqlite3.Connection:
    config.ensure_home()
    db = sqlite3.connect(str(config.db_path()), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
                     "CREATE TABLE IF NOT EXISTS projects(path TEXT PRIMARY KEY, root TEXT NOT NULL);")
    db.executescript(dbmod.TEST_RUNS_SCHEMA)
    return db


def runs(db):
    return [dict(r) for r in db.execute("SELECT * FROM test_runs ORDER BY id")]


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "t/E9-01-demo"], cwd=d, check=True)
    monkeypatch.chdir(d)
    return d


# ---------------------------------------------------------------- formatting

def test_formatting_uk():
    assert measure.fmt_duration(41.84) == "41,8 с"
    assert measure.fmt_duration(192) == "3 хв 12 с"
    assert measure.fmt_duration(3900) == "1 год 5 хв"
    assert measure.fmt_bytes(1.1 * 1024**3) == "1,1 ГБ"
    assert measure.fmt_bytes(85 * 1024**2) == "85 МБ"
    assert measure.fmt_bytes(None) == "–"
    line = measure.summary_line("uv run pytest -q", {"wall_s": 41.8, "cpu_s": 192, "avg_cores": 4.6, "peak_cores": 6.9,
                                                      "peak_rss_bytes": 1.1 * 1024**3, "exit_code": 0})
    assert line == "measure: uv run pytest -q · 41,8 с · CPU 3 хв 12 с · 4,6/6,9 ядра · 1,1 ГБ · exit 0"
    assert "4,6/– ядра" in measure.summary_line("x", {"wall_s": 1, "cpu_s": 1, "avg_cores": 4.6, "exit_code": 0})


def test_parse():
    opts, cmd = measure.parse(["--ticket", "E1-01", "--agent=qa", "--", "ls", "-l"])
    assert (opts["ticket"], opts["agent"], cmd) == ("E1-01", "qa", ["ls", "-l"])
    assert measure.parse(["ls"]) is None
    assert measure.parse(["--ticket", "E1", "--"]) is None
    assert measure.parse(["--bogus", "1", "--", "ls"]) is None


def test_main_usage(capsys):
    assert measure.main([]) == 2
    assert measure.main(["--help"]) == 0


# ---------------------------------------------------------------- real runs

def test_exit_passthrough_and_row(git_repo, capfd):
    db = make_db()
    code = measure.main(["--agent", "tester", "--label", "l1", "--"] + py("import sys; print('out'); sys.exit(3)"))
    cap = capfd.readouterr()
    assert code == 3
    assert "out" in cap.out and "measure:" not in cap.out    # stdout of the command is not mixed with the summary
    assert cap.err.strip().startswith("measure: ") and cap.err.strip().endswith("exit 3")
    [r] = runs(db)
    assert (r["state"], r["source"], r["exit_code"], r["agent"], r["label"]) == ("done", "wrapper", 3, "tester", "l1")
    assert r["branch"] == "t/E9-01-demo" and r["ticket_id"] == "E9-01" and r["ticket_source"] == "branch"
    assert r["wall_s"] > 0 and r["peak_rss_bytes"] > 0 and r["pid"]


def test_ticket_flag_wins(git_repo):
    db = make_db()
    measure.main(["--ticket", "E1-02", "--"] + py("pass"))
    assert runs(db)[0]["ticket_id"] == "E1-02" and runs(db)[0]["ticket_source"] == "flag"


def test_signal_death_exit_code(git_repo):
    db = make_db()
    code, row = measure.execute(py("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
    assert code == 128 + signal.SIGKILL and row["signal"] == signal.SIGKILL
    assert runs(db)[0]["signal"] == signal.SIGKILL


def test_not_found_and_not_executable(tmp_path, capsys):
    db = make_db()
    assert measure.main(["--", "definitely-not-a-command-xyz"]) == 127
    f = tmp_path / "noexec"
    f.write_text("x")
    assert measure.main(["--", str(f)]) == 126
    assert runs(db) == []


def test_cpu_counts_grandchildren(git_repo):
    db = make_db()
    burn = BURN.format(s=0.6)
    parent = textwrap.dedent(f"""
        import subprocess, sys
        ps = [subprocess.Popen([sys.executable, '-c', {burn!r}]) for _ in range(2)]
        for p in ps: p.wait()
    """)
    code, row = measure.execute(py(parent), interval=0.1)
    assert code == 0
    assert row["cpu_s"] >= 0.9 * 1.2          # two grandchildren of ~0.6 s each
    assert row["avg_cores"] > 1.0 and row["peak_cores"] >= row["avg_cores"]


def test_fast_grandchildren_counted(git_repo):
    make_db()
    parent = textwrap.dedent("""
        import subprocess, sys
        subprocess.run([sys.executable, '-c', 'x=sum(range(12000000))'])
        subprocess.run([sys.executable, '-c', 'x=sum(range(12000000))'])
    """)
    _, row = measure.execute(py(parent), interval=5.0)   # no sample can see them
    assert row["cpu_s"] > 0.1


def test_without_psutil_peaks_null(git_repo, monkeypatch):
    db = make_db()
    monkeypatch.setattr(measure, "_have_psutil", lambda: False)
    code, row = measure.execute(py("pass"))
    assert code == 0 and row["peak_cores"] is None and row["peak_rss_bytes"] is None
    line = measure.summary_line(row["command"], row)
    assert "/– ядра · – · exit 0" in line
    assert runs(db)[0]["peak_cores"] is None


def test_secret_in_command_masked(git_repo, capsys):
    db = make_db()
    measure.main(["--"] + py("pass") + ["API_KEY=hunter2hunter2hunter2"])
    err = capsys.readouterr().err
    assert "hunter2" not in err and "hunter2" not in runs(db)[0]["command"]
    tok = "ghp_" + "a1B2c3D4" * 5
    measure.main(["--"] + py("pass") + [tok])
    err = capsys.readouterr().err
    assert tok not in err and measure.HIDDEN in err
    assert runs(db)[1]["command"] == measure.HIDDEN


def test_running_row_during_run(git_repo):
    db = make_db()
    env = dict(os.environ)
    wrapper = subprocess.Popen([sys.executable, "-c", "import sys; from myvec import measure; sys.exit(measure.main(sys.argv[1:]))",
                                "--"] + py("import time; time.sleep(1.5)"), env=env, stderr=subprocess.DEVNULL)
    seen = None
    for _ in range(40):
        time.sleep(0.1)
        rs = runs(db)
        if rs:
            seen = rs[0]["state"]
            break
    wrapper.wait()
    assert seen == "running"
    assert runs(db)[0]["state"] == "done"


# ---------------------------------------------------------------- signals

def test_sigint_group_and_sigterm_forwarding(git_repo):
    db = make_db()
    boot = "import sys; from myvec import measure; sys.exit(measure.main(sys.argv[1:]))"
    sleeper = py("import time; time.sleep(30)")
    # Ctrl+C: delivered to the whole process group; the wrapper survives and reports 130
    w = subprocess.Popen([sys.executable, "-c", boot, "--"] + sleeper, start_new_session=True, stderr=subprocess.PIPE)
    time.sleep(1.0)
    os.killpg(w.pid, signal.SIGINT)
    _, err = w.communicate(timeout=10)
    assert w.returncode == -signal.SIGINT and b"exit 130" in err and "перервано".encode() in err   # wrapper re-raises
    assert runs(db)[-1]["signal"] == signal.SIGINT and runs(db)[-1]["interrupted"] == 1
    # SIGTERM to the wrapper only is forwarded to the command
    w = subprocess.Popen([sys.executable, "-c", boot, "--"] + sleeper, stderr=subprocess.PIPE)
    time.sleep(1.0)
    w.send_signal(signal.SIGTERM)
    w.communicate(timeout=10)
    assert w.returncode == -signal.SIGTERM
    assert runs(db)[-1]["signal"] == signal.SIGTERM and runs(db)[-1]["interrupted"] == 1


def test_interrupted_flag_when_child_catches_sigint(git_repo):
    db = make_db()
    boot = "import sys; from myvec import measure; sys.exit(measure.main(sys.argv[1:]))"
    child = py("import sys, time\ntry:\n    print('ready', flush=True); time.sleep(30)\nexcept KeyboardInterrupt:\n    sys.exit(2)")
    w = subprocess.Popen([sys.executable, "-c", boot, "--"] + child, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    w.stdout.readline()
    os.kill(w.pid, signal.SIGINT)
    w.communicate(timeout=10)
    r = runs(db)[-1]
    assert r["exit_code"] == 2 and r["signal"] is None and r["interrupted"] == 1


def test_not_interrupted_by_default(git_repo):
    db = make_db()
    measure.execute(py("pass"))
    assert runs(db)[0]["interrupted"] == 0


# ---------------------------------------------------------------- journal / fold

def test_locked_db_falls_back_to_journal_then_fold(git_repo):
    db = make_db()
    other = sqlite3.connect(str(config.db_path()), isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    t = time.monotonic()
    code, row = measure.execute(py("pass"))
    elapsed = time.monotonic() - t
    other.execute("ROLLBACK")
    other.close()
    assert code == 0 and elapsed < 4
    assert measure.pending() and runs(db) == []
    n = measure.fold(db)
    assert n == 2
    [r] = runs(db)
    assert r["state"] == "done" and r["run_uid"] == row["run_uid"]
    assert not measure.pending()
    assert measure.fold(db) == 0 and len(runs(db)) == 1       # no duplicates


def test_missing_db_goes_to_journal(git_repo):
    code, row = measure.execute(py("pass"))
    assert code == 0 and measure.pending()
    db = make_db()
    measure.fold(db)
    assert runs(db)[0]["run_uid"] == row["run_uid"]


def test_fold_does_not_revert_done(git_repo):
    db = make_db()
    code, row = measure.execute(py("pass"))
    start = dict(row, state="running", ended_at=None, exit_code=None)
    measure._journal(start)
    measure.fold(db)
    assert runs(db)[0]["state"] == "done"


def test_lost_for_dead_pid(git_repo):
    import socket

    db = make_db()
    p = subprocess.Popen(py("pass"))
    p.wait()
    now = time.time()
    measure._upsert(db, {"run_uid": "dead", "source": "wrapper", "state": "running", "host": socket.gethostname(),
                         "pid": p.pid, "started_at": now - 600})
    measure._upsert(db, {"run_uid": "alive", "source": "wrapper", "state": "running", "host": socket.gethostname(),
                         "pid": os.getpid(), "started_at": now - 600})
    measure._upsert(db, {"run_uid": "other", "source": "passive", "state": "running", "host": "elsewhere",
                         "pid": p.pid, "started_at": now - 600})
    measure.fold(db, now=now)
    st = {r["run_uid"]: r["state"] for r in runs(db)}
    assert st == {"dead": "lost", "alive": "running", "other": "running"}


def test_schema_v7_migrates_v6_and_accepts_passive():
    config.ensure_home()
    db = sqlite3.connect(str(config.db_path()), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('schema_version','6');")
    assert dbmod.SCHEMA_VERSION == 8
    db.executescript(dbmod.TEST_RUNS_SCHEMA)
    measure._upsert(db, {"run_uid": "p1", "source": "passive", "state": "done"})
    assert runs(db)[0]["source"] == "passive"


# ---------------------------------------------------------------- more coverage

def test_parse_equals_and_bad_interval():
    opts, cmd = measure.parse(["--ticket=E2-01", "--interval=0.5", "--", "x"])
    assert opts["ticket"] == "E2-01" and opts["interval"] == 0.5 and cmd == ["x"]
    assert measure.parse(["--interval", "abc", "--", "x"]) is None
    assert measure.parse(["--interval=zz", "--", "x"]) is None


def test_agent_label_masked(git_repo):
    db = make_db()
    measure.execute(py("pass"), agent="ghp_" + "a1B2c3D4" * 5, label="PASSWORD=hunter2hunter2")
    r = runs(db)[0]
    assert r["agent"] == measure.HIDDEN and "hunter2" not in r["label"]


def test_journal_line_is_bounded():
    line = measure._journal_line({"run_uid": "u", "command": "x" * 20000, "cwd": "/" + "d" * 9000})
    assert len(line) <= measure.JOURNAL_LINE_MAX and line.endswith(b"\n")
    assert b'"run_uid": "u"' in line


def test_stale_folding_file_is_adopted(git_repo):
    db = make_db()
    dead = subprocess.Popen(py("pass"))
    dead.wait()
    src = config.test_runs_path()
    src.parent.mkdir(parents=True, exist_ok=True)
    stale = src.with_name(f"{src.name}.folding.{dead.pid}.abcd1234")
    stale.write_text(__import__("json").dumps({"run_uid": "old1", "source": "wrapper", "state": "done", "cwd": "/x"}) + "\n")
    assert measure.pending()
    assert measure.fold(db) == 1
    assert [r["run_uid"] for r in runs(db)] == ["old1"] and not stale.exists()


def test_ticket_from_worktree_and_claim(tmp_path, monkeypatch):
    from myvec import ticketops

    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
                   cwd=main, check=True)
    wt = main / ".worktrees" / "E7-03"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "feature-x", str(wt)], cwd=main, check=True)
    db = make_db()
    monkeypatch.chdir(wt)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    a = measure.attribute(db, str(wt), None, None)
    assert (a["ticket_id"], a["ticket_source"]) == ("E7-03", "worktree")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    ticketops.record_claim("sess-1", "E9-99", main, cwd=wt)
    a = measure.attribute(db, str(wt), None, "sess-1")
    assert (a["ticket_id"], a["ticket_source"]) == ("E9-99", "claim")


def test_cli_fast_path_does_not_import_typer(tmp_path):
    code = ("import sys\nsys.argv=['myvec','measure','--',sys.executable,'-c','pass']\n"
            "from myvec import cli\n"
            "try:\n    cli.app()\nexcept SystemExit as e:\n    rc=e.code\n"
            "print('RC', rc, 'typer' in sys.modules, 'sqlite_vec' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=30,
                         env=dict(os.environ, MYVEC_HOME=str(tmp_path / "h")))
    assert "RC 0 False False" in out.stdout, out.stdout + out.stderr


def test_daemon_tick_test_runs(ws, monkeypatch):
    from conftest import do_sync
    from myvec import daemon

    do_sync(ws)
    st = daemon.DaemonState(7878, "t")
    try:
        st.refresh()
        measure._journal({"run_uid": "d1", "source": "wrapper", "state": "done", "cwd": "/x"})
        st._tick_test_runs()
        assert st.db().execute("SELECT state FROM test_runs WHERE run_uid='d1'").fetchone()[0] == "done"
        assert not measure.pending()
        logs = []
        monkeypatch.setattr(st, "_sync_log", logs.append)
        measure._journal({"run_uid": "d2", "source": "wrapper", "state": "done"})
        monkeypatch.setattr(measure, "fold", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        st._tick_test_runs()   # must not raise
        assert logs and "test_runs fold failed" in logs[0]
    finally:
        st.executor.shutdown(wait=True)
        st.close()


CHILD_COUNTER = ("import signal, sys, time\nn=[0]\n"
                 "def h(*a): n[0]+=1\n"
                 "signal.signal(signal.SIGINT, h)\nprint('ready', flush=True)\n"
                 "t=time.time()\nwhile time.time()-t<4 and not n[0]: time.sleep(0.02)\n"
                 "time.sleep(0.3)\nprint('SIGINTS', n[0])")   # exits after the first SIGINT, like a real tool
BOOT = "import sys; from myvec import measure; sys.exit(measure.main(sys.argv[1:]))"


def test_sigint_reaches_child_once_from_tty(git_repo):
    import pty

    make_db()
    pid, fd = pty.fork()
    if pid == 0:   # session leader with a controlling terminal, like a shell command
        os.execv(sys.executable, [sys.executable, "-c", BOOT, "--"] + py(CHILD_COUNTER))
    buf = b""
    while b"ready" not in buf:
        buf += os.read(fd, 1024)
    os.write(fd, b"\x03")   # the terminal turns it into SIGINT for the foreground group
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    os.waitpid(pid, 0)
    assert b"SIGINTS 1" in buf, buf


def test_sigint_to_wrapper_only_without_tty_is_forwarded(git_repo):
    make_db()
    w = subprocess.Popen([sys.executable, "-c", BOOT, "--"] + py(CHILD_COUNTER), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    w.stdout.readline()
    os.kill(w.pid, signal.SIGINT)
    out, _ = w.communicate(timeout=15)
    assert "SIGINTS 1" in out


def test_child_does_not_inherit_ignored_sigquit(git_repo):
    make_db()
    code, _ = measure.execute(py("import signal, sys; sys.exit(0 if signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL else 9)"))
    assert code == 0


def test_child_gets_measure_marker_env(git_repo, tmp_path):
    out = tmp_path / "env.txt"
    code = f"import os; open({str(out)!r}, 'w').write(os.environ.get('MYVEC_MEASURE_RUN', ''))"
    db = make_db()
    assert measure.main(["--"] + py(code)) == 0
    [r] = runs(db)
    assert out.read_text() == r["run_uid"]


def _passive_row(uid="passive-h-1-5000", state="running", **kw):
    row = {"run_uid": uid, "source": "passive", "state": state, "host": "h", "pid": 1, "project_path": "/p/own",
           "cwd": None, "label": "pytest", "command": "pytest", "started_at": 5.0, "sample_interval_s": 5.0}
    row.update(kw)
    return row


def test_fold_passive_attributes_once_and_keeps_project(git_repo):
    db = make_db()
    measure._journal(_passive_row(cwd=str(git_repo)))
    measure._journal(_passive_row(state="done", cwd=str(git_repo), ended_at=9.0, wall_s=4.0, cpu_s=6.0))
    assert measure.fold(db, now=10.0) == 2
    [r] = runs(db)
    assert (r["state"], r["source"], r["project_path"]) == ("done", "passive", "/p/own")   # the detector's project wins
    assert r["branch"] == "t/E9-01-demo" and r["ticket_id"] == "E9-01" and r["ticket_source"] == "branch"
    # a later event does not re-attribute: the ticket fixed at launch stays
    db.execute("UPDATE test_runs SET ticket_id='E1-01' WHERE run_uid=?", (r["run_uid"],))
    measure._journal(_passive_row(state="done", cwd=str(git_repo), ended_at=9.5, wall_s=4.5))
    measure.fold(db, now=11.0)
    [r] = runs(db)
    assert r["ticket_id"] == "E1-01" and r["wall_s"] == 4.5
