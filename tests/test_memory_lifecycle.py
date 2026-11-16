import json
import os
import sqlite3
import subprocess

import pytest
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import config, db as dbmod, memory, vaultgit
from myvec.commands import app

runner = CliRunner()


@pytest.fixture
def synced(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    return ws


def cli(*args):
    r = runner.invoke(app, list(args))
    assert r.exit_code == 0, r.output
    return r


def search_mem(q, **kw):
    r = runner.invoke(app, ["search", q, "--kind", "memory", "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def row(mid_or_text):
    db = dbmod.open_ready()
    r = db.execute("SELECT * FROM memories WHERE id=? OR text LIKE ?", (mid_or_text, f"%{mid_or_text}%")).fetchone()
    return db, r


def commit_file(site, name, body="x = 1\n", msg="work"):
    write(site / name, body)
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", msg)


# ------------------------------------------------------------------ origin metadata vs branch filter

def test_remember_defaults_to_project_with_origin_metadata(synced, env):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "feature")
    cli("remember", "billing uses the quarterly rounding rule")
    db, r = row("quarterly rounding")
    assert r["scope"] == "acme" and r["branch"] is None
    assert r["origin_branch"] == "feature" and r["origin_repo"] == "acme/site" and r["commit"]
    text = next((env / "vault/projects/acme").glob("*.md")).read_text()
    assert "origin_branch: feature" in text and "origin_repo: acme/site" in text and "commit: " in text
    assert not any(ln.startswith("branch:") for ln in text.splitlines())
    git(site, "checkout", "-q", "main")
    assert search_mem("quarterly rounding")  # visible from every branch


def test_remember_branch_flag_filters_by_branch(synced, env):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "feature")
    cli("remember", "experimental flux capacitor wiring notes", "--branch")
    db, r = row("flux capacitor")
    assert r["branch"] == "feature" and r["origin_branch"] == "feature"
    assert "branch: feature" in next((env / "vault/projects/acme").glob("*.md")).read_text()
    assert search_mem("flux capacitor")
    ctx = runner.invoke(app, ["context"]).stdout
    assert "flux capacitor" in ctx
    git(site, "checkout", "-q", "main")
    assert not search_mem("flux capacitor")
    assert "flux capacitor" not in runner.invoke(app, ["context"]).stdout


# ------------------------------------------------------------------ lifecycle

def test_lifecycle_merged_branch_becomes_project_wide(synced, env):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "feature")
    commit_file(site, "f1.py", "def f1():\n    return 1\n")
    cli("remember", "feature branch decision about zebra caching", "--branch")
    commit_file(site, "f2.py", "def f2():\n    return 2\n")
    do_sync(synced)
    assert row("zebra caching")[1]["branch"] == "feature"      # still alive: not merged
    git(site, "checkout", "-q", "main")
    git(site, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    st, db = do_sync(synced)                                   # lifecycle runs inside sync
    r = db.execute("SELECT * FROM memories WHERE text LIKE '%zebra caching%'").fetchone()
    assert r["branch"] is None and r["origin_branch"] == "feature" and r["superseded_by"] is None
    text = next((env / "vault/projects/acme").glob("*.md")).read_text()
    assert "origin_branch: feature" in text and not any(ln.startswith("branch:") for ln in text.splitlines())
    assert search_mem("zebra caching")                         # visible on main now
    log = subprocess.run(["git", "-C", str(env / "vault"), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert "lifecycle" in log and "-> project" in log


def test_lifecycle_merge_commit_at_branch_tip_without_new_work(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "feature")
    commit_file(site, "f1.py")
    cli("remember", "remembered at the very tip of the branch", "--branch")
    git(site, "checkout", "-q", "main")
    commit_file(site, "m1.py", "y = 2\n", "main moves on")
    git(site, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    _, db = do_sync(synced)
    assert db.execute("SELECT branch FROM memories WHERE text LIKE '%very tip%'").fetchone()[0] is None


def test_fresh_branch_without_work_is_not_merged(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "fresh")
    cli("remember", "note on a brand new branch with no commits", "--branch")
    git(site, "checkout", "-q", "main")
    commit_file(site, "m1.py", "y = 2\n", "main moves on")
    _, db = do_sync(synced)
    r = db.execute("SELECT * FROM memories WHERE text LIKE '%brand new branch%'").fetchone()
    assert r["branch"] == "fresh" and r["superseded_by"] is None


def test_lifecycle_deleted_unmerged_branch_is_archived(synced, env):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "wip")
    commit_file(site, "w1.py", "def w1():\n    return 1\n")
    cli("remember", "wip idea about pelican routing", "--branch")
    git(site, "checkout", "-q", "main")
    git(site, "branch", "-D", "wip")
    _, db = do_sync(synced)
    r = db.execute("SELECT * FROM memories WHERE text LIKE '%pelican routing%'").fetchone()
    assert r["superseded_by"] == "archived"
    assert not list((env / "vault/projects/acme").glob("*.md"))
    assert list((env / "vault/_archive").rglob("*.md"))
    assert not search_mem("pelican routing")
    assert db.execute("SELECT count(*) FROM memories_vec WHERE rowid=?", (r["n"],)).fetchone()[0] == 0


def test_lifecycle_deleted_but_merged_branch_becomes_project_wide(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "done")
    commit_file(site, "d1.py", "def d1():\n    return 1\n")
    cli("remember", "done branch note about heron indexing", "--branch")
    git(site, "checkout", "-q", "main")
    git(site, "merge", "-q", "--ff-only", "done")
    git(site, "branch", "-d", "done")
    _, db = do_sync(synced)
    r = db.execute("SELECT * FROM memories WHERE text LIKE '%heron indexing%'").fetchone()
    assert r["branch"] is None and r["superseded_by"] is None


def test_lifecycle_also_runs_in_gc(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "wip2")
    commit_file(site, "w2.py", "def w2():\n    return 1\n")
    cli("remember", "gc driven lifecycle check about marmot", "--branch")
    git(site, "checkout", "-q", "main")
    git(site, "branch", "-D", "wip2")
    r = cli("gc")
    assert "1 archived" in r.stdout
    assert row("marmot")[1]["superseded_by"] == "archived"


# ------------------------------------------------------------------ promote

def test_promote_moves_to_global(synced, env):
    cli("remember", "naming convention for invoices is INV-year-seq")
    db, r = row("naming convention")
    mid = r["id"]
    assert list((env / "vault/projects/acme").glob("*.md"))
    out = cli("promote", mid).stdout
    assert "promoted" in out
    assert not list((env / "vault/projects/acme").glob("*.md"))
    f = next((env / "vault/global").glob("*.md"))
    assert "scope: global" in f.read_text() and mid in f.name
    r = dbmod.open_ready().execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
    assert r["scope"] == "global" and r["vault_file"] == str(f) and r["branch"] is None
    assert search_mem("naming convention")[0]["scope"] == "global"
    assert runner.invoke(app, ["promote", mid]).exit_code != 0       # already global
    assert runner.invoke(app, ["promote", "nope"]).exit_code != 0


def test_promote_drops_branch_filter(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "feat3")
    cli("remember", "branchy fact about otters", "--branch")
    mid = row("otters")[1]["id"]
    cli("promote", mid)
    git(site, "checkout", "-q", "main")
    assert search_mem("otters")


# ------------------------------------------------------------------ vault is a git repo

def vault_log(env):
    return subprocess.run(["git", "-C", str(env / "vault"), "log", "--format=%s"], capture_output=True,
                          text=True).stdout.splitlines()


def test_vault_git_init_and_autocommit(synced, env):
    cli("remember", "autocommit target note about lemurs")
    v = env / "vault"
    assert (v / ".git").is_dir()
    assert (v / ".gitignore").read_text().split() == [".obsidian/workspace*.json", ".trash/"]
    mid = row("lemurs")[1]["id"]
    assert any(s.startswith(f"myvec: remember {mid}") for s in vault_log(env))
    cli("pin", mid)
    cli("pin", mid, "--unpin")
    cli("promote", mid)
    cli("forget", mid)
    log = "\n".join(vault_log(env))
    for needle in (f"myvec: pin {mid}", f"myvec: unpin {mid}", f"myvec: promote {mid}", f"myvec: forget {mid}"):
        assert needle in log
    st = subprocess.run(["git", "-C", str(v), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert st.strip() == ""                                     # everything committed
    # untracked obsidian workspace state is ignored
    write(v / ".obsidian/workspace.json", "{}")
    write(v / ".trash/x.md", "x")
    assert vaultgit.autocommit("myvec: noop") is False


def test_vault_git_failures_never_break_commands(synced, env, monkeypatch):
    def boom(*a, **k):
        raise OSError("git exploded")
    monkeypatch.setattr(vaultgit, "_git", boom)
    r = runner.invoke(app, ["remember", "survives a broken vault git setup"])
    assert r.exit_code == 0, r.output
    assert "git exploded" in (env / "logs/vault-git.log").read_text()


def test_vault_git_ignores_user_global_config(synced, env, tmp_path, monkeypatch):
    gc = tmp_path / "gitconfig"
    gc.write_text("[commit]\n\tgpgsign = true\n[core]\n\thooksPath = /nonexistent\n")
    cli("remember", "made despite hostile global git config")
    assert any("remember" in s for s in vault_log(env))


# ------------------------------------------------------------------ schema migration

def test_migration_from_v1_schema(tmp_path, env):
    # a v1 database: old columns only, no schema_version
    import sqlite3 as sq
    p = config.db_path()
    c = sq.connect(p)
    c.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE projects(path TEXT PRIMARY KEY, root TEXT NOT NULL, parent TEXT, share_global INTEGER DEFAULT 1, registered_at TEXT);
    CREATE TABLE repos(id INTEGER PRIMARY KEY, project_path TEXT NOT NULL, path TEXT NOT NULL UNIQUE, dir TEXT NOT NULL UNIQUE, current_branch TEXT, last_sync REAL);
    CREATE TABLE blobs(sha TEXT PRIMARY KEY, lang TEXT, indexed_at REAL, skip_reason TEXT, secret_skipped INTEGER DEFAULT 0);
    CREATE TABLE memories(n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, scope TEXT NOT NULL, branch TEXT, "commit" TEXT,
      vault_file TEXT, text TEXT NOT NULL, created TEXT, updated TEXT, pinned INTEGER DEFAULT 0,
      superseded_by TEXT, source TEXT DEFAULT 'vault', tags TEXT DEFAULT '');
    INSERT INTO projects VALUES('acme','/x',NULL,1,'2024-01-01');
    INSERT INTO memories(id, scope, branch, "commit", text) VALUES('m1','acme/site','feat','abc','old branch memory');
    INSERT INTO meta VALUES('model','embeddinggemma'); INSERT INTO meta VALUES('dims','64');
    """)
    c.commit()
    c.close()
    db = dbmod.open_ready()
    assert dbmod.get_meta(db, "schema_version") == str(dbmod.SCHEMA_VERSION)
    cols = {r[1] for r in db.execute("PRAGMA table_info(memories)")}
    assert {"origin_branch", "origin_repo"} <= cols
    assert {"sync_mode", "window_hours", "last_access"} <= {r[1] for r in db.execute("PRAGMA table_info(projects)")}
    assert "fingerprint" in {r[1] for r in db.execute("PRAGMA table_info(repos)")}
    assert "secret_masked" in {r[1] for r in db.execute("PRAGMA table_info(blobs)")}
    m = db.execute("SELECT * FROM memories").fetchone()
    assert m["branch"] == "feat" and m["origin_branch"] == "feat" and m["origin_repo"] == "acme/site"
    assert db.execute("SELECT sync_mode FROM projects").fetchone()[0] == "auto"
    dbmod.open_ready()   # idempotent


def test_gc_vacuum_compacts_the_index(synced, env):
    r = cli("gc", "--vacuum")
    assert "gc:" in r.stdout and "vacuum: index.db" in r.stdout
    assert search_mem("anything") is not None                         # the index still opens and answers


def test_migrate_skips_tables_missing_in_old_db(tmp_path):
    """`myvec status` on a DB from an older version calls migrate() before init_schema(): a table that
    does not exist yet (e.g. `sessions`) must be skipped, not ALTERed (OperationalError: no such table)."""
    import sqlite3

    from myvec import db as dbmod

    db = sqlite3.connect(str(tmp_path / "old.db"), isolation_level=None)
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES('schema_version', '1')")
    db.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, root TEXT)")
    db.execute("CREATE TABLE memories(n INTEGER PRIMARY KEY, scope TEXT, branch TEXT, origin_branch TEXT, origin_repo TEXT)")
    dbmod.migrate(db)
    have = {r[1] for r in db.execute("PRAGMA table_info(projects)")}
    assert "sync_mode" in have
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone() is None
