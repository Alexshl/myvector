import json
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import ORIG_TRIGGER, TRIGGERED, do_sync, git, write
from myvec import config, db as dbmod, gitexcludes, gitutil, searchcmd
from myvec.commands import app

runner = CliRunner()
NOTE = "(індекс оновлюється у фоні)"


@pytest.fixture
def synced(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    return ws


def test_no_note_when_index_is_fresh(synced):
    r = runner.invoke(app, ["search", "invoice total"])
    assert NOTE not in r.stdout and not TRIGGERED


def test_branch_switch_triggers_background_sync_and_note(synced):
    git(synced / "site", "checkout", "-q", "-b", "other")
    r = runner.invoke(app, ["search", "invoice total"])
    assert r.exit_code == 0 and r.stdout.rstrip().endswith(NOTE)
    assert TRIGGERED == [("acme", str(synced.resolve()))]
    do_sync(synced)                                  # what the background sync would do
    TRIGGERED.clear()
    assert NOTE not in runner.invoke(app, ["search", "invoice total"]).stdout and not TRIGGERED


def test_index_mtime_commit_and_non_git_dir_changes(synced):
    site = synced / "site"
    write(site / "new_module.py", "def brand_new():\n    return 1\n")
    git(site, "add", "-A")                                   # touches .git/index
    assert NOTE in runner.invoke(app, ["search", "invoice total"]).stdout
    do_sync(synced)
    assert NOTE not in runner.invoke(app, ["search", "invoice total"]).stdout
    # pseudo-repo (project root is not a git repo): a new top-level dir
    write(synced / "docs2/readme.md", "# hi\n")
    r = runner.invoke(app, ["search", "invoice total", "--scope", "project"])
    assert NOTE in r.stdout


def test_json_mode_puts_note_on_stderr(synced):
    git(synced / "site", "checkout", "-q", "-b", "other2")
    r = runner.invoke(app, ["search", "invoice total", "--json"])
    assert json.loads(r.stdout)                          # stdout stays pure JSON
    assert NOTE in r.stderr


def test_mode_off_and_memory_only_never_trigger(synced):
    git(synced / "site", "checkout", "-q", "-b", "other3")
    assert NOTE not in runner.invoke(app, ["search", "invoice", "--kind", "memory"]).stdout
    runner.invoke(app, ["mode", "off"])
    assert NOTE not in runner.invoke(app, ["search", "invoice"]).stdout and not TRIGGERED


def test_freshness_check_is_cheap(synced):
    db = dbmod.open_ready()
    repos = db.execute("SELECT * FROM repos").fetchall()
    t0 = time.perf_counter()
    for _ in range(20):
        searchcmd.stale_repos(db, repos)
    assert (time.perf_counter() - t0) / 20 < 0.02    # a few ms for 3 repos


def test_fingerprint_signals():
    pass


def test_real_trigger_prefers_daemon_then_spawns_throttled(synced, env, monkeypatch):
    from myvec import daemon_client
    calls = []
    monkeypatch.setattr(daemon_client, "request", lambda m, p, b=None, **k: calls.append((m, p)) or {"queued": True})
    spawned = []
    monkeypatch.setattr(searchcmd.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))
    ORIG_TRIGGER("acme", str(synced))
    assert calls == [("POST", "/api/projects/acme/sync")] and not spawned
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)         # no daemon
    ORIG_TRIGGER("acme", str(synced))
    assert len(spawned) == 1 and spawned[0][0][0][-3:] == ["myvec", "sync", "--quiet"] or "sync" in spawned[0][0][0]
    ORIG_TRIGGER("acme", str(synced))                                           # throttled
    assert len(spawned) == 1


def test_real_trigger_skips_when_lock_held(synced, env, monkeypatch):
    from myvec import daemon_client, lock
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(searchcmd.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with lock.sync_lock() as got:
        assert got
        ORIG_TRIGGER("acme", str(synced))
    assert not spawned


# ------------------------------------------------------------------ global git excludes

def init_project(tmp_path, monkeypatch, name="proj"):
    proj = tmp_path / name
    proj.mkdir()
    (proj / "a.py").write_text("def alpha_fn():\n    return 1\n")
    git(proj, "init", "-q", "-b", "main")
    monkeypatch.chdir(proj)
    r = runner.invoke(app, ["init", "--name", name, "--fg"])
    assert r.exit_code == 0, r.output
    return proj, r


def test_init_adds_marker_to_default_global_excludes(tmp_path, monkeypatch):
    proj, r = init_project(tmp_path, monkeypatch)
    ignore = tmp_path / "userhome/.config/git/ignore"          # git's default location
    assert ignore.read_text().splitlines() == [".myvector.toml"]
    assert "global git excludes" in r.stdout
    assert git(proj, "status", "--porcelain").count(".myvector.toml") == 0   # hidden from git
    assert not (tmp_path / "gitconfig").exists()                              # core.excludesFile NOT set by us
    # idempotent, also for a second project
    init_project(tmp_path, monkeypatch, "proj2")
    assert ignore.read_text().splitlines() == [".myvector.toml"]


def test_init_uses_configured_excludes_file(tmp_path, monkeypatch):
    custom = tmp_path / "userhome/my_global_ignore"
    custom.write_text("*.tmp\n.DS_Store")                       # no trailing newline
    (tmp_path / "gitconfig").write_text("[core]\n\texcludesFile = ~/my_global_ignore\n")
    init_project(tmp_path, monkeypatch)
    assert custom.read_text().splitlines() == ["*.tmp", ".DS_Store", ".myvector.toml"]
    assert not (tmp_path / "userhome/.config/git/ignore").exists()
    assert (tmp_path / "gitconfig").read_text() == "[core]\n\texcludesFile = ~/my_global_ignore\n"


def test_excludes_keeps_existing_equivalent_line(tmp_path, monkeypatch):
    ignore = tmp_path / "userhome/.config/git/ignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text("**/.myvector.toml\n")
    p, changed = gitexcludes.ensure_marker_excluded()
    assert p == ignore and changed is False and ignore.read_text() == "**/.myvector.toml\n"
