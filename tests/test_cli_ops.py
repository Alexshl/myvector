import json
import os
import subprocess
import sys

from typer.testing import CliRunner

from conftest import do_sync
from myvec import hooks, lock
from myvec.commands import app

runner = CliRunner()


def test_init_fg_creates_marker_and_syncs(tmp_path, monkeypatch):
    proj = tmp_path / "newproj"
    proj.mkdir()
    (proj / "a.py").write_text("def alpha_fn():\n    return 1\n")
    monkeypatch.chdir(proj)
    r = runner.invoke(app, ["init", "--name", "newproj", "--parent", "grp", "--exclude", "*.log", "--no-share-global", "--fg"])
    assert r.exit_code == 0, r.output
    t = (proj / ".myvector.toml").read_text()
    assert 'name = "newproj"' in t and 'parent = "grp"' in t and "share_global = false" in t and '"*.log"' in t
    assert "project 'grp/newproj' registered" in r.stdout
    out = json.loads(runner.invoke(app, ["search", "alpha_fn", "--json"]).stdout)
    assert out[0]["repo"] == "grp/newproj"


def test_sync_skips_when_lock_held(ws, monkeypatch):
    monkeypatch.chdir(ws)
    with lock.sync_lock() as got:
        assert got
        r = runner.invoke(app, ["sync"])
        assert r.exit_code == 0 and "sync already running" in r.stdout
        assert lock.is_locked()
    assert not lock.is_locked()
    r = runner.invoke(app, ["sync"])
    assert "synced acme" in r.stdout


def test_sync_outside_project_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["sync"]).exit_code != 0


def test_install_hooks_idempotent(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"model": "opus", "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    r = runner.invoke(app, ["install-hooks", "--settings", str(s)])
    assert r.exit_code == 0 and "installed" in r.stdout
    first = s.read_text()
    data = json.loads(first)
    assert data["model"] == "opus"
    post = data["hooks"]["PostToolUse"]
    assert len(post) == 4 and post[0]["hooks"][0]["command"] == "echo hi"
    ours = post[1]
    assert ours["matcher"] == "Edit|Write|MultiEdit" and ours["hooks"][0]["command"].endswith("hook post-edit")
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("hook session-start")
    backups = list(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text())["model"] == "opus"
    r = runner.invoke(app, ["install-hooks", "--settings", str(s)])
    assert "already installed" in r.stdout
    assert s.read_text() == first and len(list(tmp_path.glob("settings.json.bak-*"))) == 1


def test_install_hooks_creates_missing_file(tmp_path):
    s = tmp_path / "sub" / "settings.json"
    assert runner.invoke(app, ["install-hooks", "--settings", str(s)]).exit_code == 0
    assert "SessionStart" in json.loads(s.read_text())["hooks"]


def run_hook(which, stdin, cwd=None):
    return subprocess.run([sys.executable, "-m", "myvec", "hook", which], input=stdin, capture_output=True,
                          text=True, cwd=cwd, env=os.environ.copy(), timeout=20)


def test_hooks_exit_zero_on_garbage():
    for which in ("session-start", "post-edit", "bogus"):
        for stdin in ("", "not json", "[1,2]", '{"cwd": 5}', '{"tool_input": "x"}', '{"tool_input": {"file_path": 7}}'):
            r = run_hook(which, stdin)
            assert r.returncode == 0, (which, stdin, r.stderr)


def test_hook_session_start_prints_context_and_spawns_sync(ws, tmp_path):
    r = run_hook("session-start", json.dumps({"cwd": str(ws / "site")}))
    assert r.returncode == 0 and "project: acme" in r.stdout
    # not in a project: silent
    r = run_hook("session-start", json.dumps({"cwd": str(tmp_path)}))
    assert r.returncode == 0 and r.stdout == ""


def test_hook_post_edit_spawns_background_sync(ws, env):
    import time
    r = run_hook("post-edit", json.dumps({"tool_input": {"file_path": str(ws / "site/src/Invoice.php")}}))
    assert r.returncode == 0
    for _ in range(100):
        time.sleep(0.2)
        if (env / "index.db").exists() and "synced acme" in ((env / "logs/sync.log").read_text() if (env / "logs/sync.log").exists() else ""):
            break
    assert "synced acme" in (env / "logs/sync.log").read_text()
    # debounce: just synced -> no new sync
    n = (env / "logs/sync.log").read_text().count("synced acme")
    run_hook("post-edit", json.dumps({"tool_input": {"file_path": str(ws / "site/src/Invoice.php")}}))
    time.sleep(1.0)
    assert (env / "logs/sync.log").read_text().count("synced acme") == n
    # file outside any project: nothing
    assert run_hook("post-edit", json.dumps({"tool_input": {"file_path": "/etc/hosts"}})).returncode == 0


def test_hooks_main_never_raises(monkeypatch):
    monkeypatch.setattr(hooks, "session_start", lambda: 1 / 0)
    assert hooks.main(["session-start"]) == 0


def test_install_hooks_replaces_stale_entries(tmp_path):
    s = tmp_path / "settings.json"
    stale = "/Users/someone/.local/bin/myvec"
    s.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"{stale} hook session-start"}]}],
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": f"{stale} hook post-edit"}]},
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
                        {"matcher": "Edit", "hooks": [{"type": "command", "command": f"{stale} hook post-edit"}]}]}}))
    assert runner.invoke(app, ["install-hooks", "--settings", str(s)]).exit_code == 0
    first = s.read_text()
    data = json.loads(first)
    assert "someone" not in first
    cmds = [h["command"] for ev in data["hooks"].values() for g in ev for h in g["hooks"]]
    assert sum("hook session-start" in c for c in cmds) == 1 and sum("hook post-edit" in c for c in cmds) == 1
    assert "echo hi" in cmds and len(data["hooks"]["PostToolUse"]) == 4
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 1
    assert "already installed" in runner.invoke(app, ["install-hooks", "--settings", str(s)]).stdout
    assert s.read_text() == first


def test_relocate_rewrites_paths_only_on_segment_boundary(ws, tmp_path):
    from myvec import config, db as dbmod, relocate
    _, db = do_sync(ws)
    root = str(ws)
    other = root + "-other/x"                       # shares the string prefix, not the path segment
    db.execute("INSERT INTO memories(id, scope, vault_file, text, source) VALUES ('m1','global',?, 'a', 'vault')", (root + "/vault/a.md",))
    db.execute("INSERT INTO memories(id, scope, vault_file, text, source) VALUES ('m2','global',?, 'b', 'import')", (other,))
    db.execute("INSERT INTO memories(id, scope, vault_file, text, source) VALUES ('m3','global',NULL, 'c', 'vault')")
    config.save_registry([root, other])
    chunks = db.execute("SELECT count(*), sum(id) FROM chunks").fetchone()[:]
    db.close()

    r = runner.invoke(app, ["relocate", "--from", root, "--to", "/home/new", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would rewrite" in r.stdout and "projects: 1" in r.stdout and "memories: 1" in r.stdout and "registry: 1" in r.stdout
    assert config.load_registry() == sorted([root, other])
    db = dbmod.connect(vec=False)
    assert db.execute("SELECT root FROM projects").fetchone()[0] == root
    db.close()

    r = runner.invoke(app, ["relocate", "--from", root + "/", "--to", "/home/new/"])
    assert r.exit_code == 0 and "rewrote" in r.stdout, r.output
    db = dbmod.connect(vec=False)
    assert db.execute("SELECT root FROM projects").fetchone()[0] == "/home/new"
    dirs = {x[0] for x in db.execute("SELECT dir FROM repos")}
    assert {"/home/new/site", "/home/new/libs"} <= dirs and all(d.startswith("/home/new") for d in dirs)
    vf = dict(db.execute("SELECT id, vault_file FROM memories WHERE id IN ('m1','m2','m3')").fetchall())
    assert vf == {"m1": "/home/new/vault/a.md", "m2": other, "m3": None}
    assert config.load_registry() == sorted(["/home/new", other])
    assert tuple(db.execute("SELECT count(*), sum(id) FROM chunks").fetchone()) == tuple(chunks)
    assert runner.invoke(app, ["relocate", "--from", "rel", "--to", "/x"]).exit_code != 0
    assert relocate.rewrite("/a/bc", "/a/b", "/z") is None
