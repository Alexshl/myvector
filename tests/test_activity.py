import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from conftest import do_sync
from myvec import activity, config, db as dbmod
from myvec.commands import app

runner = CliRunner()


def mkrow(mode="auto", window=None, last=0.0):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE p(sync_mode TEXT, window_hours REAL, last_access REAL)")
    c.execute("INSERT INTO p VALUES(?,?,?)", (mode, window, last))
    return c.execute("SELECT * FROM p").fetchone()


def test_activity_window_logic():
    now = 1_000_000.0
    assert activity.is_active(mkrow("auto", None, now - 3600), 2.0, now)             # inside the default 2h window
    assert not activity.is_active(mkrow("auto", None, now - 3 * 3600), 2.0, now)     # expired
    assert activity.is_active(mkrow("auto", 5, now - 3 * 3600), 2.0, now)            # per-project window wins
    assert not activity.is_active(mkrow("auto", 0.5, now - 3600), 2.0, now)
    assert activity.is_active(mkrow("always", None, 0), 2.0, now)
    assert not activity.is_active(mkrow("off", None, now), 2.0, now)
    assert activity.active_until(mkrow("auto", 1, 100.0), 2.0) == 100.0 + 3600
    assert activity.active_until(mkrow("always", 1, 100.0), 2.0) is None
    assert activity.mode_of(mkrow(None)) == "auto"


def test_touch_extends_window_and_returns_mode(ws):
    _, db = do_sync(ws)
    row = db.execute("SELECT * FROM projects WHERE path='acme'").fetchone()
    assert activity.is_active(row, 2.0)                                    # registering counts as an access
    db.execute("UPDATE projects SET last_access=?, sync_mode='always' WHERE path='acme'", (time.time() - 10 * 3600,))
    assert activity.touch("acme") == "always"
    row = db.execute("SELECT * FROM projects WHERE path='acme'").fetchone()
    assert time.time() - row["last_access"] < 5
    assert activity.touch("no/such/project") is None


def test_touch_falls_back_to_activity_file_when_db_is_locked(ws):
    _, db = do_sync(ws)
    db.execute("UPDATE projects SET last_access=0 WHERE path='acme'")
    blocker = sqlite3.connect(str(config.db_path()), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")                                      # a long write (sync) is running
    t0 = time.time()
    assert activity.touch("acme") is None
    assert time.time() - t0 < 1.0                                           # short busy timeout, no hanging
    blocker.execute("ROLLBACK")
    blocker.close()
    assert config.activity_path().read_text().split("\t")[1].strip() == "acme"
    assert db.execute("SELECT last_access FROM projects WHERE path='acme'").fetchone()[0] == 0
    assert activity.fold_activity(db) == 1
    assert time.time() - db.execute("SELECT last_access FROM projects WHERE path='acme'").fetchone()[0] < 5
    assert not config.activity_path().exists()
    assert activity.fold_activity(db) == 0


def test_mode_command_and_status(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    r = runner.invoke(app, ["mode", "always", "--window-hours", "6"])
    assert r.exit_code == 0 and "mode=always" in r.stdout and "6h" in r.stdout
    st = runner.invoke(app, ["status"]).stdout
    assert "mode=always  window=6h  always on" in st
    runner.invoke(app, ["mode", "off", "--project", "acme"])
    assert "mode=off  window=6h  off (no automatic sync)" in runner.invoke(app, ["status"]).stdout
    runner.invoke(app, ["mode", "auto", "--project", str(ws)])              # filesystem path works too
    st = runner.invoke(app, ["status"]).stdout
    assert "mode=auto  window=6h  active until " in st
    db = dbmod.open_ready()
    db.execute("UPDATE projects SET last_access=0")
    assert "inactive (idle)" in runner.invoke(app, ["status"]).stdout
    assert runner.invoke(app, ["mode", "sometimes"]).exit_code != 0
    assert runner.invoke(app, ["mode", "auto", "--project", "nope"]).exit_code != 0
    assert runner.invoke(app, ["mode", "auto", "--window-hours", "0"]).exit_code != 0


def test_global_window_is_configurable(env):
    p = env / "config.toml"
    assert config.load_config().window_hours == 2.0
    p.write_text(p.read_text() + "")
    p.write_text(p.read_text().replace("window_hours = 2.0", "window_hours = 0.25"))
    assert config.load_config().window_hours == 0.25


def test_search_and_remember_extend_the_window(ws, monkeypatch):
    _, db = do_sync(ws)
    monkeypatch.chdir(ws / "site")
    for cmd in (["search", "invoice"], ["remember", "window extension check note"]):
        db.execute("UPDATE projects SET last_access=1 WHERE path='acme'")
        assert runner.invoke(app, cmd).exit_code == 0
        assert time.time() - db.execute("SELECT last_access FROM projects").fetchone()[0] < 10, cmd


def run_hook(which, stdin, cwd=None):
    return subprocess.run([sys.executable, "-m", "myvec", "hook", which], input=stdin, capture_output=True, text=True,
                          cwd=cwd, env=os.environ.copy(), timeout=20)


def test_hooks_touch_and_respect_off(ws, env):
    _, db = do_sync(ws)
    db.execute("UPDATE projects SET last_access=1")
    t0 = time.time()
    run_hook("session-start", json.dumps({"cwd": str(ws / "site")}))
    assert db.execute("SELECT last_access FROM projects").fetchone()[0] >= t0
    db.execute("UPDATE projects SET last_access=1")
    run_hook("post-edit", json.dumps({"tool_input": {"file_path": str(ws / "site/src/Invoice.php")}}))
    assert db.execute("SELECT last_access FROM projects").fetchone()[0] >= t0
    # mode off: still counted as access but never spawns a sync
    db.execute("UPDATE projects SET sync_mode='off', last_access=1")
    db.execute("UPDATE repos SET last_sync=1")
    log = env / "logs/hook-sync.log"
    if log.exists():
        log.unlink()
    run_hook("session-start", json.dumps({"cwd": str(ws / "site")}))
    run_hook("post-edit", json.dumps({"tool_input": {"file_path": str(ws / "site/src/Invoice.php")}}))
    time.sleep(0.5)
    assert not log.exists()
    assert db.execute("SELECT last_access FROM projects").fetchone()[0] >= t0


def test_hook_is_stdlib_only_and_fast(ws):
    do_sync(ws)
    code = ("import sys, json; sys.argv=['myvec','hook','post-edit']; "
            "from myvec import hooks; hooks.main(['post-edit']); "
            "bad=[m for m in ('typer','httpx','sqlite_vec','numpy','tree_sitter','starlette','uvicorn','watchdog') if m in sys.modules]; "
            "print(bad)")
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps({"tool_input": {"file_path": str(ws / "README.md")}}),
                       capture_output=True, text=True, env=os.environ.copy(), timeout=20)
    assert r.stdout.strip().endswith("[]"), r.stdout + r.stderr
    t0 = time.perf_counter()
    run_hook("session-start", json.dumps({"cwd": str(ws)}))
    assert time.perf_counter() - t0 < 1.0   # generous: interpreter start dominates; the real budget is checked in the smoke run
