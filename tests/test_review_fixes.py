"""Regression tests for the findings of the independent code review (one section per finding)."""
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import TRIGGERED, do_sync, git, write
from myvec import (activity, chunk, config, ctx as ctxmod, daemon, embed, filters, hooks, indexer, lock,
                   logutil, memory, projects, search as S, searchcmd, vaultgit)
from myvec import db as dbmod
from myvec.commands import app
from myvec.embed import get_embedder
from test_daemon import H, PORT, client, state  # noqa: F401 - fixtures

runner = CliRunner()


def cli(*a, ok=True):
    r = runner.invoke(app, list(a))
    if ok:
        assert r.exit_code == 0, r.output
    return r


@pytest.fixture
def synced(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    return ws


def commit_file(site, name, body="x = 1\n", msg="work"):
    write(site / name, body)
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", msg)


def mem_row(text):
    db = dbmod.open_ready()
    return db, db.execute("SELECT * FROM memories WHERE text LIKE ?", (f"%{text}%",)).fetchone()


# ================================================================== 1. blob content race

def test_content_edited_mid_sync_is_not_stored_under_the_old_sha(ws, monkeypatch):
    site = ws / "site"
    write(site / "race.py", "def original_alpha():\n    return 1\n")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "r")
    head_blob = git(site, "rev-parse", "HEAD:race.py").strip()
    orig = indexer.list_git_files

    def wrapped(*a, **kw):
        files = orig(*a, **kw)
        if a[2].dir == site:
            write(site / "race.py", "def edited_beta():\n    return 2\n")   # the user edits during the sync
        return files

    monkeypatch.setattr(indexer, "list_git_files", wrapped)
    st, db = do_sync(ws)
    # what was read is stored under its own key, the manifest points at it, the HEAD blob is not claimed
    real = indexer.git_blob_sha(b"def edited_beta():\n    return 2\n")
    assert db.execute("SELECT count(*) FROM chunks WHERE blob_sha=?", (head_blob,)).fetchone()[0] == 0
    assert db.execute("SELECT blob_sha FROM branch_files WHERE repo_id=(SELECT id FROM repos WHERE path='acme/site') "
                      "AND path='race.py'").fetchone()[0] == real
    assert any("edited_beta" in r[0] for r in db.execute("SELECT text FROM chunks WHERE blob_sha=?", (real,)))
    fp = db.execute("SELECT fingerprint FROM repos WHERE path='acme/site'").fetchone()[0]
    assert fp.endswith("|raced")                              # the next freshness check sees the repo as stale
    monkeypatch.setattr(indexer, "list_git_files", orig)
    git(site, "checkout", "--", "race.py")                    # revert: worktree == HEAD blob again
    st, db = do_sync(ws)
    texts = [r[0] for r in db.execute("SELECT text FROM chunks WHERE blob_sha=?", (head_blob,))]
    assert texts and all("original_alpha" in t and "edited_beta" not in t for t in texts)


def test_unreadable_is_never_persisted(ws, monkeypatch):
    site = ws / "site"
    write(site / "ghost.py", "def ghost_fn():\n    return 1\n")       # untracked
    orig = indexer._prepare

    def vanishing(base, rel, sha):
        if rel == "ghost.py":
            (base / rel).unlink()                                    # deleted between listing and reading
        return orig(base, rel, sha)

    monkeypatch.setattr(indexer, "_prepare", vanishing)
    st, db = do_sync(ws)
    assert st.errors == []
    assert db.execute("SELECT count(*) FROM blobs WHERE skip_reason='unreadable'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM branch_files WHERE path='ghost.py'").fetchone()[0] == 0


def test_eol_filters_do_not_look_like_a_race(ws):
    site = ws / "site"
    write(site / ".gitattributes", "*.crlf.txt text eol=crlf\n")
    write(site / "w.crlf.txt", "line one\nline two\n")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "crlf")
    (site / "w.crlf.txt").unlink()
    git(site, "checkout", "--", "w.crlf.txt")                         # worktree bytes are CRLF, the blob is LF
    assert b"\r\n" in (site / "w.crlf.txt").read_bytes()
    st, db = do_sync(ws)
    assert not db.execute("SELECT fingerprint FROM repos WHERE path='acme/site'").fetchone()[0].endswith("|raced")
    blob = git(site, "rev-parse", "HEAD:w.crlf.txt").strip()
    assert db.execute("SELECT count(*) FROM chunks WHERE blob_sha=?", (blob,)).fetchone()[0] >= 1


# ================================================================== 2. project path collisions

def _two_api_dirs(tmp_path):
    dirs = []
    for parent, fn in (("one", "alpha_only"), ("two", "beta_only")):
        d = tmp_path / parent / "api"
        d.mkdir(parents=True)
        git(d, "init", "-q", "-b", "main")
        write(d / "m.py", f"def {fn}():\n    return 1\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "i")
        write(d / ".myvector.toml", 'name = "api"\n')
        dirs.append(d)
    return dirs


def test_same_named_projects_do_not_hijack_each_other(tmp_path):
    a, b = _two_api_dirs(tmp_path)
    do_sync(a)
    with pytest.raises(indexer.ProjectConflict, match="--name"):
        do_sync(b)
    db = dbmod.open_ready()
    assert [tuple(r) for r in db.execute("SELECT path, dir FROM repos")] == [("api", str(a.resolve()))]
    assert [tuple(r) for r in db.execute("SELECT path, root FROM projects")] == [("api", str(a.resolve()))]
    n0 = embed.STATS["texts"]
    st, _ = do_sync(a)                                          # a is untouched: nothing re-embedded, nothing dropped
    assert embed.STATS["texts"] == n0 and st.errors == []


def test_init_refuses_taken_path_and_suggests_a_name(tmp_path, monkeypatch):
    a, b = _two_api_dirs(tmp_path)
    (a / ".myvector.toml").unlink()
    (b / ".myvector.toml").unlink()
    monkeypatch.chdir(a)
    assert cli("init", "--fg").exit_code == 0
    monkeypatch.chdir(b)
    r = cli("init", "--fg", ok=False)
    assert r.exit_code != 0 and "already used by" in r.output and "--name two-api" in r.output
    assert not (b / ".myvector.toml").exists()                  # nothing written for the refused project
    assert cli("init", "--name", "two-api", "--fg").exit_code == 0
    assert (b / ".myvector.toml").exists()
    db = dbmod.open_ready()
    assert {r[0] for r in db.execute("SELECT path FROM projects")} == {"api", "two-api"}


def test_moved_project_directory_is_taken_over(tmp_path):
    a, _ = _two_api_dirs(tmp_path)
    do_sync(a)
    moved = tmp_path / "one" / "api-renamed"
    a.rename(moved)                                            # the old directory no longer exists
    n0 = embed.STATS["texts"]
    st, db = do_sync(moved)
    assert st.errors == [] and embed.STATS["texts"] == n0
    assert [tuple(r) for r in db.execute("SELECT path, dir FROM repos")] == [("api", str(moved.resolve()))]
    assert db.execute("SELECT root FROM projects WHERE path='api'").fetchone()[0] == str(moved.resolve())


# ================================================================== 3. transactions

def test_listing_holds_no_transaction_and_survives_concurrent_commit(ws, monkeypatch):
    st, db = do_sync(ws)
    db.close()
    write(ws / "zz_new.txt", "brand new content " * 10)
    orig = indexer._hash_cached
    calls = {"n": 0, "in_txn": []}

    def wrapped(db, *a, **kw):
        calls["n"] += 1
        calls["in_txn"].append(db.in_transaction)
        if calls["n"] == 1:
            r = orig(db, *a, **kw)
            o = sqlite3.connect(str(config.db_path()), isolation_level=None)
            o.execute("UPDATE projects SET last_access=last_access+1")   # e.g. activity.touch from a hook
            o.close()
            return r
        return orig(db, *a, **kw)

    monkeypatch.setattr(indexer, "_hash_cached", wrapped)
    st, db = do_sync(ws)
    assert st.errors == [] and calls["n"] >= 1
    assert not any(calls["in_txn"])                              # walking/hashing never holds a transaction
    assert db.execute("SELECT count(*) FROM branch_files WHERE path='zz_new.txt'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM file_state WHERE path='zz_new.txt'").fetchone()[0] == 1


def test_remember_rolls_back_the_vault_file_when_the_db_write_fails(synced, env, monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    real_index = memory._index
    monkeypatch.setattr(memory, "_index", boom)
    r = cli("remember", "a note whose database write fails", ok=False)
    assert r.exit_code != 0 or isinstance(r.exception, sqlite3.OperationalError)
    assert not list((env / "vault").rglob("*.md"))                # no orphan file
    monkeypatch.setattr(memory, "_index", real_index)
    cli("remember", "kept note that will be updated later on")
    f = next((env / "vault/projects/acme").glob("*.md"))
    before = f.read_text()
    monkeypatch.setattr(memory, "_index", boom)
    cli("remember", "kept note that will be updated later on", ok=False)   # dedup -> update path fails
    assert f.read_text() == before                                # old content restored


# ================================================================== 4. FTS path index

def test_fts_join_uses_the_blob_sha_index(synced):
    db = dbmod.open_ready()
    db.execute("CREATE TEMP TABLE IF NOT EXISTS scope_repos(repo_id INTEGER PRIMARY KEY, branch TEXT, weight REAL, "
               "rpath TEXT)")
    plan = " | ".join(r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN " + S.CODE_FTS_SQL.format(table="chunks_fts"), ("renderPdf", 150)))
    assert "branch_files_sha" in plan, plan
    assert "SCAN bf" not in plan and "SCAN branch_files" not in plan, plan
    # the sweeps of gc use the same index instead of scanning per chunk
    plan = " | ".join(r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM chunks WHERE blob_sha NOT IN (SELECT blob_sha FROM branch_files)"))
    assert "branch_files_sha" in plan or "COVERING INDEX" in plan, plan


def test_v3_database_gets_the_index_and_new_columns(synced):
    db = dbmod.open_ready()
    db.execute("DROP INDEX branch_files_sha")
    db.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    db.close()
    db = dbmod.open_ready()
    assert dbmod.get_meta(db, "schema_version") == str(dbmod.SCHEMA_VERSION) == "8"
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='branch_files_sha'").fetchone()
    cols = {r[1] for r in db.execute("PRAGMA table_info(memories)")}
    assert {"base_commit", "tip_seen"} <= cols


# ================================================================== 5. dedup across branch scope

def test_dedup_never_turns_a_project_note_into_a_branch_note(synced):
    site = synced / "site"
    text = "deploy goes through pipeline alpha with manual approval gate"
    cli("remember", text)
    git(site, "checkout", "-q", "-b", "feat")
    commit_file(site, "f.py", "z = 1\n")
    r = cli("remember", text, "--branch")
    assert "updated existing" not in r.output
    db = dbmod.open_ready()
    rows = db.execute("SELECT id, branch, superseded_by FROM memories ORDER BY n").fetchall()
    assert [(r["branch"], r["superseded_by"]) for r in rows] == [(None, None), ("feat", None)]
    # the same text on the same branch is a duplicate of the branch note, not of the project one
    r = cli("remember", text, "--branch")
    assert "updated existing" in r.output
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 2
    git(site, "checkout", "-q", "main")
    git(site, "branch", "-D", "feat")
    _, db = do_sync(synced)
    states = {(r["branch"], r["superseded_by"]) for r in db.execute("SELECT * FROM memories")}
    assert (None, None) in states and (None, "archived") not in states    # the project note survives
    assert ("feat", None) not in states                                     # the branch note was archived


# ================================================================== 6. merged detection

def test_note_before_own_commits_on_a_deleted_unmerged_branch_is_archived(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "abandoned")
    cli("remember", "experimental idea about otter queue sharding", "--branch")
    commit_file(site, "o.py", "def o():\n    return 1\n")
    git(site, "checkout", "-q", "main")
    git(site, "branch", "-D", "abandoned")
    _, db = do_sync(synced)
    r = db.execute("SELECT branch, superseded_by FROM memories WHERE text LIKE '%otter%'").fetchone()
    assert r["superseded_by"] == "archived"


def test_fast_forwarded_fresh_branch_is_not_merged(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "fresh")
    cli("remember", "note about walrus refactor planning", "--branch")
    git(site, "checkout", "-q", "main")
    commit_file(site, "m1.py", "y = 2\n", "main moves on")
    git(site, "checkout", "-q", "fresh")
    git(site, "merge", "-q", "--ff-only", "main")           # update the branch from main: no work of its own
    _, db = do_sync(synced)
    r = db.execute("SELECT branch, superseded_by FROM memories WHERE text LIKE '%walrus%'").fetchone()
    assert (r["branch"], r["superseded_by"]) == ("fresh", None)


def test_base_commit_is_recorded_in_db_and_frontmatter(synced, env):
    site = synced / "site"
    main_tip = git(site, "rev-parse", "HEAD").strip()
    git(site, "checkout", "-q", "-b", "based")
    commit_file(site, "b.py", "b = 1\n")
    cli("remember", "branch note remembering its base", "--branch")
    _, r = mem_row("remembering its base")
    assert r["base_commit"] == main_tip and r["commit"] and r["branch"] == "based"
    assert f"base_commit: {main_tip}" in next((env / "vault/projects/acme").glob("*.md")).read_text()


def test_fresh_branch_note_is_merged_by_a_merge_commit(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "later-work")
    cli("remember", "note written before the branch had commits", "--branch")
    commit_file(site, "l.py", "l = 1\n")
    git(site, "checkout", "-q", "main")
    commit_file(site, "m.py", "m = 1\n", "main moves on")
    git(site, "merge", "-q", "--no-ff", "-m", "merge later-work", "later-work")
    _, db = do_sync(synced)
    r = db.execute("SELECT branch, superseded_by FROM memories WHERE text LIKE '%before the branch had%'").fetchone()
    assert (r["branch"], r["superseded_by"]) == (None, None)


def test_observed_branch_work_proves_a_later_fast_forward_merge(synced):
    site = synced / "site"
    git(site, "checkout", "-q", "-b", "observed")
    cli("remember", "note on a fresh branch that later got merged", "--branch")
    commit_file(site, "obs.py", "o = 1\n")
    _, db = do_sync(synced)                                  # lifecycle sees the branch ahead of main
    r = db.execute("SELECT branch, tip_seen FROM memories WHERE text LIKE '%later got merged%'").fetchone()
    assert r["branch"] == "observed" and r["tip_seen"] == git(site, "rev-parse", "HEAD").strip()
    git(site, "checkout", "-q", "main")
    git(site, "merge", "-q", "--ff-only", "observed")
    git(site, "branch", "-d", "observed")
    _, db = do_sync(synced)
    r = db.execute("SELECT branch, superseded_by FROM memories WHERE text LIKE '%later got merged%'").fetchone()
    assert (r["branch"], r["superseded_by"]) == (None, None)


# ================================================================== 7. secret masking

@pytest.mark.parametrize("src,kept,masked", [
    ("SECRET_KEY = 'django-insecure-abcdef123456'", "SECRET_KEY = '•••'", 1),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "AWS_SECRET_ACCESS_KEY=•••", 1),
    ("$db_pass = 'hunter2hunter2';", "$db_pass = '•••';", 1),
    ("define('DB_PASSWORD', 'hunter2hunter2');", "define('DB_PASSWORD', '•••');", 1),
    ('define("STRIPE_KEY", "abcd12345678");', 'define("STRIPE_KEY", "•••");', 1),
    ("dbPassword: 'hunter2hunter2'", "dbPassword: '•••'", 1),
    ("const sentryDsn = 'https://abc@o1.ingest.sentry.io/1';", "const sentryDsn = '•••';", 1),
    ("client_secret: abcdefghij123", "client_secret: •••", 1),
    ("private_key = \"abcdefghijkl\"", "private_key = \"•••\"", 1),
    # not masked: variables, calls, env lookups, identifiers, templates
    ("$passwordConfirmation = $request->input('password_confirmation');",) * 2 + (0,),
    ("define('APP_NAME', 'My Application');",) * 2 + (0,),
    ("tokenizer = Tokenizer.from_pretrained('bert')",) * 2 + (0,),
    ("SECRET_KEY = os.environ['SECRET_KEY']",) * 2 + (0,),
    ("secret_key_base: <%= ENV['SECRET'] %>",) * 2 + (0,),
])
def test_masking_gaps(src, kept, masked):
    out, n = filters.mask_secrets(src)
    assert (out, n) == (kept, masked)


def test_markdown_heading_symbols_are_masked_everywhere(ws):
    write(ws / "site/docs/ops.md", "# Ops\n\n## prod password: hunter2xyz99\n\nrun the deploy script with the "
                                   "usual flags, then verify the health endpoint answers within a minute.\n")
    git(ws / "site", "add", "-A")
    git(ws / "site", "commit", "-q", "-m", "ops")
    st, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM chunks WHERE symbol LIKE '%hunter2xyz99%' OR text LIKE '%hunter2xyz99%'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chunks WHERE symbol LIKE '%password: •••%'").fetchone()[0] >= 1
    assert db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'hunter2xyz99'").fetchone()[0] == 0


def test_import_head_is_masked(ws, env):
    write(ws / "CLAUDE.md", "# Notes\n\n## prod password: hunter2xyz99\n\nshort text that stays under the merge "
                            "threshold of tiny markdown sections, so it gets its own piece to import.\n")
    do_sync(ws)
    db = dbmod.open_ready()
    res = memory.import_all(db, get_embedder(config.load_config()))
    assert res["memories"] >= 1
    rows = [r[0] for r in db.execute("SELECT text FROM memories WHERE source='import'")]
    assert rows and not any("hunter2xyz99" in t for t in rows)
    assert any("password: •••" in t for t in rows)


def test_long_private_key_is_dropped_in_every_chunk(ws):
    body = "\n".join("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" + f"{i:04d}" for i in range(150))
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    write(ws / "site/src/keys.py", f"def key_holder():\n    return '''\n{pem}\n'''\n\n\ndef after_key():\n    return 1\n")
    bare_end = "\n".join("c2VjcmV0LWtleS1tYXRlcmlhbC1saW5l" + f"{i:04d}" for i in range(150))
    write(ws / "site/src/tail.txt", f"intro line\n{bare_end}\n-----END PRIVATE KEY-----\noutro line\n")
    git(ws / "site", "add", "-A")
    git(ws / "site", "commit", "-q", "-m", "pem")
    st, db = do_sync(ws)
    stored = " ".join(r[0] for r in db.execute("SELECT text FROM chunks"))
    assert "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" not in stored and "c2VjcmV0LWtleS1tYXRlcmlhbC1saW5l" not in stored
    assert "PRIVATE KEY" not in stored
    assert st.secrets_skipped >= 3
    assert filters.has_hard_secret("-----END EC PRIVATE KEY-----")           # a bare END is hard too
    assert filters.private_key_ranges("a\n-----BEGIN PRIVATE KEY-----\nabcdefgh1234\n-----END PRIVATE KEY-----\n") == [(2, 4)]


# ================================================================== 8. path traversal / reserved names

@pytest.mark.parametrize("name", ["../../../../escape", "..", "a/../b", "/abs", "global", "_archive", "_topics",
                                  "global/x", "a b", "a//b", ".", "x/./y"])
def test_invalid_project_names_are_rejected(tmp_path, name):
    root = tmp_path / "evil"
    root.mkdir()
    write(root / ".myvector.toml", f'name = "{name}"\n')
    with pytest.raises(projects.InvalidMarker, match="invalid project"):
        projects.read_marker(root)
    assert projects.try_read_marker(root) is None
    assert ctxmod.current(None, root) is None


def test_valid_names_still_work(tmp_path):
    for name in ("acme", "grp/new.proj_1-x", "a.b"):
        root = tmp_path / name.replace("/", "_")
        root.mkdir()
        write(root / ".myvector.toml", f'name = "{name}"\n')
        assert projects.read_marker(root).path == name


def test_invalid_marker_message_in_cli_and_silent_hooks(tmp_path, monkeypatch, capsys):
    root = tmp_path / "evil"
    root.mkdir()
    write(root / ".myvector.toml", 'name = "../escape"\n')
    monkeypatch.chdir(root)
    r = cli("sync", ok=False)
    assert r.exit_code != 0 and "invalid project" in r.output and "'..'" in r.output
    r = cli("init", "--name", "../escape", ok=False)
    assert r.exit_code != 0 and "invalid project name" in r.output
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(root)})))
    assert hooks.main(["session-start"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(root / "a.py")}})))
    assert hooks.main(["post-edit"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_vault_paths_stay_inside_the_vault(synced, env):
    for bad in ("..", "../x", "a/../../b", "/etc", ""):
        with pytest.raises(ValueError):
            memory.scope_dir(bad)
    assert memory.scope_dir("acme/site") == env / "vault/projects/acme/site"
    assert memory.scope_dir("global") == env / "vault/global"
    db = dbmod.open_ready()
    with pytest.raises(ValueError):
        memory.remember(db, get_embedder(config.load_config()), "evil note", "../../escape")
    assert not (env.parent / "escape").exists() and not list(env.rglob("evil*"))
    # topics: an unsafe scope in the database never writes outside vault/_topics
    for i in range(2):
        m = memory.Memory(id=f"t{i}", scope="../../outside", body=f"topic note number {i}", created="2026-01-01",
                          updated="2026-01-01")
        memory._index(db, m, get_embedder(config.load_config()).embed_docs([m.body])[0])
    memory.topics(db)
    assert not (env / "outside.md").exists() and not (env.parent / "outside.md").exists()
    assert not any(p for p in env.rglob("outside*.md"))
    # archive: a vault_file path that escapes the vault is not moved
    victim = env.parent / "victim.md"
    victim.write_text("x")
    row = db.execute("SELECT * FROM memories WHERE id='t0'").fetchone()
    db.execute("UPDATE memories SET vault_file=? WHERE n=?", (str(victim), row["n"]))
    db.execute("BEGIN IMMEDIATE")
    memory._archive_row(db, db.execute("SELECT * FROM memories WHERE id='t0'").fetchone(), superseded_by="archived")
    db.execute("COMMIT")
    assert victim.exists()


# ================================================================== 9. child project inside a git root

def test_child_marker_inside_git_root_is_indexed_once(tmp_path):
    root = tmp_path / "mono"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / ".myvector.toml", 'name = "mono"\n')
    write(root / "pkg/.myvector.toml", 'name = "pkg"\n')
    write(root / "pkg/lib.py", "def unique_pkg_fn():\n    return 42\n")
    write(root / "top.py", "def unique_top_fn():\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "i")
    st, db = do_sync(root)
    rows = [tuple(r) for r in db.execute(
        "SELECT r.path, bf.path FROM branch_files bf JOIN repos r ON r.id=bf.repo_id WHERE bf.path LIKE '%lib.py'")]
    assert rows == [("mono/pkg", "lib.py")]
    assert db.execute("SELECT count(*) FROM branch_files bf JOIN repos r ON r.id=bf.repo_id "
                      "WHERE r.path='mono' AND bf.path='top.py'").fetchone()[0] == 1


# ================================================================== 10. reembed

def test_reembed_is_atomic(synced, monkeypatch):
    db = dbmod.open_ready()
    cfg = config.load_config()
    before = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("chunks_vec", "chunk_repo", "memories_vec")}
    assert before["chunks_vec"] > 3
    cli("remember", "a memory that also gets re-embedded")
    before["memories_vec"] = db.execute("SELECT count(*) FROM memories_vec").fetchone()[0]
    orig = indexer._insert_vec
    n = {"i": 0}

    def flaky(*a, **kw):
        n["i"] += 1
        if n["i"] == 3:
            raise RuntimeError("boom half-way")
        return orig(*a, **kw)

    monkeypatch.setattr(indexer, "_insert_vec", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        indexer.reembed(db, cfg, get_embedder(cfg))
    after = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in ("chunks_vec", "chunk_repo", "memories_vec")}
    assert after == before                                       # the old index is intact
    assert not db.in_transaction
    monkeypatch.setattr(indexer, "_insert_vec", orig)
    res = indexer.reembed(db, cfg, get_embedder(cfg))
    assert res["chunks"] > 0 and res["memories"] == 1
    assert db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == before["chunks_vec"]
    assert db.execute("SELECT scope FROM memories_vec").fetchone()[0] == "acme"


# ================================================================== 11. memory KNN inside the scope partition

def test_memory_search_is_not_crowded_out_by_other_scopes(synced):
    db = dbmod.open_ready()
    cfg = config.load_config()
    emb = get_embedder(cfg)
    vec_other = emb.embed_docs(["alpha beta gamma"])[0]
    for i in range(260):                                          # 260 closer notes of an unrelated project
        memory._index(db, memory.Memory(id=f"o{i}", scope="other", body="alpha beta gamma",
                                        created="2026-01-01", updated="2026-01-01"), vec_other)
    target = memory.Memory(id="mine", scope="acme", body="alpha beta gamma delta epsilon zeta",
                           created="2026-01-01", updated="2026-01-01")
    memory._index(db, target, emb.embed_docs([target.body])[0])
    c = ctxmod.current(db, synced / "site")
    hits = S.search(db, cfg, emb, "alpha beta gamma", c, kind="memory", scope="project", mode="vector")
    assert [h.data["id"] for h in hits] == ["mine"]
    # nearest_in_scope (dedup) is partitioned the same way
    near = memory.nearest_in_scope(db, emb.embed_docs([target.body])[0], "acme")
    assert near and near[0][0]["id"] == "mine"


def test_memories_vec_migrates_to_the_scope_partition(synced):
    cli("remember", "migrated vector memory about pelicans")
    cli("remember", "global flavoured memory about herons", "--global")
    db = dbmod.open_ready()
    vecs = db.execute("SELECT rowid, embedding FROM memories_vec").fetchall()
    assert len(vecs) == 2
    dims = int(dbmod.get_meta(db, "dims"))
    db.execute("DROP TABLE memories_vec")                         # the v3 layout: no partition column
    db.execute(f"CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[{dims}] distance_metric=cosine)")
    for rid, emb in vecs:
        db.execute("INSERT INTO memories_vec(rowid, embedding) VALUES(?,?)", (rid, emb))
    db.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    db.close()
    db = dbmod.open_ready()
    assert "partition key" in db.execute("SELECT sql FROM sqlite_master WHERE name='memories_vec'").fetchone()[0]
    assert db.execute("SELECT count(*) FROM memories_vec").fetchone()[0] == 2
    qv = dbmod.pack(get_embedder(config.load_config()).embed_query("pelicans"))
    got = db.execute("SELECT rowid FROM memories_vec WHERE embedding MATCH ? AND k=5 AND scope=?", (qv, "acme")).fetchall()
    assert len(got) == 1 and db.execute("SELECT scope FROM memories WHERE n=?", (got[0][0],)).fetchone()[0] == "acme"
    dbmod.open_ready()                                            # idempotent


def test_promote_moves_the_vector_to_the_global_partition(synced):
    cli("remember", "promotable memory about kingfishers")
    _, r = mem_row("kingfishers")
    cli("promote", r["id"])
    db = dbmod.open_ready()
    qv = dbmod.pack(get_embedder(config.load_config()).embed_query("kingfishers"))
    assert db.execute("SELECT count(*) FROM memories_vec WHERE embedding MATCH ? AND k=5 AND scope='global'",
                      (qv,)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM memories_vec WHERE embedding MATCH ? AND k=5 AND scope='acme'",
                      (qv,)).fetchone()[0] == 0


# ================================================================== 12. gc of vanished repos

def test_gc_removes_repos_whose_directory_vanished(ws, env, monkeypatch):
    monkeypatch.chdir(ws / "site")
    st, db = do_sync(ws)
    libs_id = db.execute("SELECT id FROM repos WHERE path='acme/libs'").fetchone()[0]
    assert db.execute("SELECT count(*) FROM chunks WHERE text LIKE '%libs_only_helper%'").fetchone()[0] == 1
    shutil.rmtree(ws / "libs")
    r = cli("gc")
    assert "1 vanished repos" in r.stdout and "acme/libs" in r.stdout
    assert db.execute("SELECT count(*) FROM repos WHERE path='acme/libs'").fetchone()[0] == 0
    for t in ("branch_files", "chunk_repo", "file_state"):
        assert db.execute(f"SELECT count(*) FROM {t} WHERE repo_id=?", (libs_id,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chunks WHERE text LIKE '%libs_only_helper%'").fetchone()[0] == 0   # orphan
    assert "acme/libs" in (env / "logs/sync.log").read_text()
    assert db.execute("SELECT count(*) FROM repos WHERE path='acme/site'").fetchone()[0] == 1


# ================================================================== 13. log rotation

def test_logutil_rotates(tmp_path):
    p = tmp_path / "x.log"
    for i in range(200):
        logutil.append(p, "line %d %s" % (i, "y" * 50), max_bytes=1000)
    assert p.stat().st_size <= 1100 and (tmp_path / "x.log.1").exists()
    assert (tmp_path / "x.log.1").stat().st_size <= 1100
    assert not (tmp_path / "x.log.2").exists()


def test_every_log_is_rotated(synced, env, monkeypatch):
    logs = env / "logs"
    big = b"x" * (logutil.MAX_BYTES + 10) + b"\n"
    names = ["queries.jsonl", "sync.log", "hook.log", "hook-sync.log", "vault-git.log"]
    for n in names:
        (logs / n).write_bytes(big)
    searchcmd.log_query({"q": "x"})
    from myvec import commands
    commands._sync_log("hello")
    hooks._log("hello")
    vaultgit._log("hello")
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    hooks.spawn_sync(str(synced))
    for n in names:
        assert (logs / (n + ".1")).stat().st_size == len(big), n
        assert (logs / n).stat().st_size < 1000, n


# ================================================================== 14. GET /api/search has no side effects

def test_get_search_is_read_only_and_post_may_sync(client, state, ws):
    git(ws / "site", "checkout", "-q", "-b", "other")             # makes the index stale
    db = dbmod.open_ready()
    db.execute("UPDATE projects SET last_access=0")
    before_events = len(state.bus.recent(500))
    j = client.get("/api/search", params={"q": "renderPdf", "cwd": str(ws / "site")}).json()
    assert j["stale"] is True and j["index_refreshing"] is False
    time.sleep(0.3)
    assert not TRIGGERED and not state._queued
    assert not [e for e in state.bus.recent(500)[before_events:] if e["type"].startswith("sync_")]
    assert db.execute("SELECT max(last_access) FROM projects").fetchone()[0] == 0     # no activity extension
    # POST (CSRF-protected) is allowed to: it flags the refresh, starts the sync and extends the window
    assert client.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}).status_code == 403
    j = client.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H).json()
    assert j["index_refreshing"] is True and j["stale"] is True and j["results"]
    assert db.execute("SELECT max(last_access) FROM projects").fetchone()[0] > 0
    state.executor.shutdown(wait=True)


# ================================================================== 15. lock

def test_is_locked_does_not_take_the_lock(monkeypatch):
    with lock.sync_lock() as got:
        assert got
    assert lock.config.lock_path().exists()
    calls = []
    real = lock.fcntl.flock
    monkeypatch.setattr(lock.fcntl, "flock", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    assert lock.is_locked() is False
    assert calls == []                                             # no holder -> not even a probe
    monkeypatch.setattr(lock.fcntl, "flock", real)                 # not undo(): that would also drop MYVEC_HOME
    with lock.sync_lock() as got:
        assert got and lock.is_locked() is True                    # pid is alive and the probe is refused
    assert lock.is_locked() is False


def test_stale_holder_pid_is_not_a_lock(tmp_path):
    config.ensure_home()
    config.lock_path().write_text("999999999")                     # a pid that does not exist
    assert lock.is_locked() is False
    config.lock_path().write_text(str(os.getpid()))                # alive pid but nobody holds the flock
    assert lock.is_locked() is False


def test_sync_lock_can_wait_for_a_short_holder():
    release = threading.Event()
    held = threading.Event()

    def holder():
        with lock.sync_lock() as got:
            assert got
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    threading.Timer(0.3, release.set).start()
    t0 = time.monotonic()
    with lock.sync_lock(wait=3.0) as got:
        assert got and 0.2 < time.monotonic() - t0 < 2.5
    t.join()
    with lock.sync_lock() as g1:
        with lock.sync_lock(wait=0.2) as g2:                       # nobody releases: gives up
            assert g1 and not g2


def test_cli_sync_that_meets_a_just_released_lock_still_runs(ws, monkeypatch):
    monkeypatch.chdir(ws)
    release = threading.Event()
    held = threading.Event()

    def holder():
        with lock.sync_lock():
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    threading.Timer(0.4, release.set).start()
    r = runner.invoke(app, ["sync"])
    t.join()
    assert "sync already running" not in r.stdout and "synced acme" in r.stdout


# ================================================================== 16. deleted index.db

def test_daemon_reopens_a_replaced_index_db(state, ws):
    old = state.db()
    old_ino = state._db_ino
    for f in config.home().glob("index.db*"):
        f.unlink()
    state.refresh()                                                # must not blow up on the vanished file
    new = state.db()
    assert new is not old and state._db_ino != old_ino or state._db_ino is not None
    assert new.execute("SELECT count(*) FROM projects").fetchone()[0] == 0     # a fresh, empty database
    other = dbmod.connect()                                        # a rebuild writes the new file elsewhere...
    dbmod.init_schema(other, config.load_config())
    other.execute("INSERT INTO projects(path, root) VALUES('reborn', '/x')")
    other.close()
    assert [r["path"] for r in state.project_rows()] == ["reborn"]


# ================================================================== 17. fold_activity

def test_fold_activity_survives_a_competing_folder(ws, monkeypatch):
    st, db = do_sync(ws)
    p = config.activity_path()
    p.write_text("1000.000\tacme\n")
    real = os.replace

    def stolen(src, dst, *a, **kw):
        real(src, dst, *a, **kw)
        if str(src) == str(p):
            Path(dst).unlink()                                     # another folder finished first and deleted it
        return None

    monkeypatch.setattr(activity.os, "replace", stolen)
    assert activity.fold_activity(db) == 0                          # no FileNotFoundError
    monkeypatch.setattr(activity.os, "replace", real)              # not undo(): that would also drop MYVEC_HOME
    assert not [x for x in p.parent.glob("activity*") if x.name != "activity.lock"]  # the lock file stays by design


def test_fold_activity_concurrent_callers_lose_nothing(ws):
    st, db0 = do_sync(ws)
    db0.execute("UPDATE projects SET last_access=0")
    p = config.activity_path()
    errors, applied = [], []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            activity._append_queue(p, f"{1000 + i}.000\tacme\n")
            time.sleep(0.001)
        applied.append(("last", i))

    def folder():
        db = dbmod.connect(threads=True)
        try:
            for _ in range(60):
                try:
                    activity.fold_activity(db)
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))
        finally:
            db.close()

    w = threading.Thread(target=writer)
    fs = [threading.Thread(target=folder) for _ in range(4)]
    w.start()
    [f.start() for f in fs]
    [f.join() for f in fs]
    stop.set()
    w.join()
    activity.fold_activity(db0)
    assert errors == []
    last = applied[0][1]
    assert db0.execute("SELECT last_access FROM projects WHERE path='acme'").fetchone()[0] == 1000 + last


def test_queue_lock_failure_does_not_break_append(ws, monkeypatch):
    p = config.activity_path()
    monkeypatch.setattr(activity.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    activity._append_queue(p, "5.000\tacme\n")
    assert p.read_text() == "5.000\tacme\n"


def test_daemon_refresh_tolerates_fold_failures(state, monkeypatch):
    monkeypatch.setattr(activity, "fold_activity", lambda db: (_ for _ in ()).throw(FileNotFoundError("gone")))
    state.refresh()


# ================================================================== 18. daemon / hooks small things

class SpyEmbedder(embed.FakeEmbedder):
    owned: list = []

    def embed_query(self, text):
        SpyEmbedder.owned.append(SpyEmbedder.lock._is_owned())
        return super().embed_query(text)


def test_query_is_embedded_outside_the_db_lock(state, ws, monkeypatch):
    from starlette.testclient import TestClient

    SpyEmbedder.owned = []
    SpyEmbedder.lock = state.db_lock
    monkeypatch.setattr(embed, "get_embedder", lambda cfg: SpyEmbedder(cfg))
    c = TestClient(daemon.create_app(state), base_url=f"http://127.0.0.1:{PORT}")
    assert c.get("/api/search", params={"q": "renderPdf", "cwd": str(ws / "site")}).status_code == 200
    assert c.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H).status_code == 200
    assert c.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H).status_code == 200
    assert len(SpyEmbedder.owned) == 3 and not any(SpyEmbedder.owned)


def test_query_embedding_has_a_short_timeout(monkeypatch):
    seen = []

    class R:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def json(self):
            return {"embeddings": [[1.0] * 64] * self.n}

    import httpx

    def post(url, json=None, timeout=None):
        seen.append(timeout)
        return R(len(json["input"]))

    monkeypatch.setattr(httpx, "post", post)
    e = embed.OllamaEmbedder(config.load_config())
    e.embed_query("hello")
    e.embed_docs(["a", "b"])
    assert seen == [embed.QUERY_TIMEOUT, embed.BATCH_TIMEOUT] and seen[0] <= 10 and seen[1] >= 60


@pytest.mark.parametrize("k", ["abc", 0, -3, 1000, True, 1.5, [1]])
def test_cli_search_endpoint_rejects_bad_k_with_400(client, ws, k):
    r = client.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site"), "k": k}, headers=H)
    assert r.status_code == 400 and "k" in r.json()["error"]
    r = client.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site"), "k": 3}, headers=H)
    assert r.status_code == 200


def test_install_hooks_quotes_the_myvec_path(tmp_path):
    s = tmp_path / "settings.json"
    exe = "/Users/some one/.local/bin/myvec"
    assert hooks.install(s, myvec_cmd=exe)[0] is True
    data = json.loads(s.read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] + data["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert len(cmds) == 4
    for c, tail in zip(cmds, (["hook", "session-start"], ["hook", "post-edit"], ["hook", "session-event"],
                              ["hook", "session-pulse"])):
        assert shlex.split(c) == [exe, *tail]
    assert hooks.install(s, myvec_cmd=exe)[0] is False                # idempotent with the quoted form


def test_vault_commits_are_serialized(synced, env, monkeypatch):
    vaultgit.ensure_repo()
    active, peak = [0], [0]
    mu = threading.Lock()

    def fake_git(*args):
        with mu:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with mu:
            active[0] -= 1
        return subprocess.CompletedProcess(args, 1 if args[:1] == ("diff",) else 0, b"", b"")

    monkeypatch.setattr(vaultgit, "_git", fake_git)
    ts = [threading.Thread(target=vaultgit.autocommit, args=(f"msg {i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


def test_concurrent_real_commits_both_land(synced, env):
    vaultgit.ensure_repo()
    for i in range(2):
        write(env / "vault" / f"note{i}.md", f"note {i}")
    ts = [threading.Thread(target=vaultgit.autocommit, args=(f"myvec: concurrent {i}",)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    st = subprocess.run(["git", "-C", str(env / "vault"), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert st.strip() == ""
    assert "commit failed" not in ((env / "logs/vault-git.log").read_text() if (env / "logs/vault-git.log").exists() else "")


# ================================================================== 19. chunking quality

def csharp_sample(n=40):
    out = ["using System;", "using System.Linq;", "", "namespace App.Util", "{", "    /// <summary>",
           "    /// Counter helpers.", "    /// </summary>", "    [Serializable]", "    public class Counter", "    {",
           "        private int _n;", ""]
    for i in range(n):
        out += [f"        /// <summary>Doc for member {i}.</summary>",
                f"        private static int Count{i}(int a) => a + {i};", ""]
    out += ["    }", "}"]
    return "\n".join(out) + "\n"


def php_sample(n=45):
    out = ["<?php", "namespace App;", "", "/** Registry of tiny getters. */", "#[Entity]", "class Registry", "{"]
    for i in range(n):
        out += [f"    /** Getter {i}. */", f"    public function get{i}(): int {{ return {i}; }}", ""]
    out += ["}", ""]
    return "\n".join(out)


def ts_sample(n=45):
    out = ["// header comment", "import { Injectable } from '@angular/core';", "", "/** The service. */",
           "@Injectable({ providedIn: 'root' })", "export class Svc {"]
    for i in range(n):
        out += [f"  /** Doc {i}. */", f"  value{i}(): number {{ return {i}; }}", ""]
    out += ["}", ""]
    return "\n".join(out)


def check_sizes(chs, text):
    lines = text.count("\n") + 1
    for c in chs:
        n = c.end_line - c.start_line + 1
        assert n <= chunk.MAX_CHUNK_LINES and len(c.text) <= chunk.MAX_CHUNK_CHARS
        if lines >= chunk.MIN_CHUNK_LINES:
            assert n >= chunk.MIN_CHUNK_LINES or len(c.text) >= chunk.MIN_CHUNK_CHARS, (c.start_line, c.end_line, c.text)


@pytest.mark.parametrize("path,src,cls", [
    ("Counter.cs", csharp_sample(), "Counter"),
    ("Registry.php", php_sample(), "Registry"),
    ("svc.ts", ts_sample(), "Svc"),
])
def test_large_class_of_tiny_members_has_no_tiny_chunks(path, src, cls):
    lang, chs = chunk.chunk_file(path, src)
    assert len(src.split("\n")) > chunk.MAX_CHUNK_LINES or len(src) > chunk.MAX_CHUNK_CHARS   # the split path
    check_sizes(chs, src)
    assert len(chs) >= 2 and len(chs) < 40
    assert all(c.symbol == cls for c in chs)
    # nothing is lost: the chunks together cover every member
    joined = "\n".join(c.text for c in chs)
    for i in (0, 20, 39):
        assert f"{i}(" in joined
    # no chunk is just comments / attributes
    for c in chs:
        body = [ln.strip() for ln in c.text.splitlines() if ln.strip()]
        assert not all(ln.startswith(("///", "//", "/**", "*", "#[", "[", "@")) for ln in body)


def test_doc_comments_and_attributes_travel_with_the_declaration():
    src = csharp_sample()
    _, chs = chunk.chunk_file("Counter.cs", src)
    # the doc comment of each member is in the same chunk as the member
    for i in range(40):
        owner = [c for c in chs if f"Count{i}(" in c.text]
        assert owner and f"Doc for member {i}." in owner[0].text
    # the leading comment + attribute of the class are not split from the class header
    first = chs[0]
    assert "[Serializable]" in first.text and "Counter helpers" in first.text and "public class Counter" in first.text
    assert first.start_line == 1                                   # `using` header glue rides along

    from myvec import chunk as c2
    ts = ts_sample(3)
    _, chs = c2.chunk_file("a.ts", ts)
    assert any("/** The service. */" in c.text and "export class Svc" in c.text for c in chs)


def test_class_with_only_small_members_is_one_chunk_named_after_the_class():
    src = csharp_sample(8)
    lang, chs = chunk.chunk_file("Small.cs", src)
    assert len(src.split("\n")) <= chunk.MAX_CHUNK_LINES and len(src) <= chunk.MAX_CHUNK_CHARS
    assert len(chs) == 1 and chs[0].symbol == "Counter"
    _, chs = chunk.chunk_file("Small.php", php_sample(5))
    assert len(chs) == 1 and chs[0].symbol == "Registry"


def test_files_smaller_than_the_minimum_stay_single_chunks():
    _, chs = chunk.chunk_file("tiny.cs", "using System;\nclass A { int F() => 1; }\n")
    assert len(chs) == 1
    _, chs = chunk.chunk_file("tiny.ts", "export const a = () => 1;\n")
    assert len(chs) == 1


def test_big_methods_keep_their_own_symbol_and_the_limits():
    big = "\n".join(f"        var v{i} = {i};" for i in range(70))
    members = []
    for i in range(30):
        members += [f"        /// <summary>m{i}</summary>", f"        int M{i}() => {i};", ""]
    src = ("namespace N\n{\n    public class Mixed\n    {\n" + "\n".join(members) +
           f"\n        public void Huge()\n        {{\n{big}\n        }}\n" + "\n".join(members[:12]) + "    }\n}\n")
    _, chs = chunk.chunk_file("Mixed.cs", src)
    check_sizes(chs, src)
    assert any(c.symbol == "Mixed.Huge" and "void Huge" in c.text for c in chs)


def test_openai_embedder_uses_v1_embeddings_with_prefixes_in_index_order(monkeypatch):
    import httpx

    cfg = config.load_config()
    cfg.embed_api, cfg.ollama_url = "openai", "http://127.0.0.1:11435/"
    calls = []

    class R:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def json(self):  # reversed on purpose: the embedder must sort by `index`
            return {"data": [{"index": i, "embedding": [float(i + 1)] + [0.0] * (cfg.dims - 1)}
                             for i in reversed(range(self.n))]}

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        return R(len(json["input"]))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.delenv("MYVEC_EMBEDDER", raising=False)                   # conftest forces the fake embedder
    e = embed.get_embedder(cfg)
    assert isinstance(e, embed.OpenAIEmbedder)
    vecs = e.embed_docs(["a", "b"])
    assert calls[0][0] == "http://127.0.0.1:11435/v1/embeddings"
    assert calls[0][1]["input"] == [e.doc_prefix + "a", e.doc_prefix + "b"]
    assert len(vecs) == 2 and all(len(v) == cfg.dims for v in vecs)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: type("E", (), {"status_code": 500, "text": "boom"})())
    with pytest.raises(embed.EmbedError):
        e.embed_query("x")
