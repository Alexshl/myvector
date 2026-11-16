"""`myvec forget-project` (E8-15, ADR-044): dry-run by default, gone/unreachable/present, nothing automatic."""
import hashlib
import shutil

from typer.testing import CliRunner

from conftest import do_sync, write
from myvec import config, forget, indexer, lock, projects, searchstats
from myvec import db as dbmod
from myvec.commands import app

runner = CliRunner()
SHARED = "def shared_marker_fn():\n    return 'quasar-shared'\n"


def mk(root, name, extra=None):
    write(root / "a.py", SHARED)
    if extra:
        write(root / "b.py", extra)
    write(root / ".myvector.toml", f'name = "{name}"\nshare_global = true\ninclude = []\nexclude = []\n')
    st, db = do_sync(root)
    config.register_root(str(root))
    return db


def open_db():
    return dbmod.open_ready(config.load_config())


def counts(db):
    out = {}
    for t in ("projects", "repos", "chunks", "chunk_repo", "blobs", "tickets", "ticket_edges", "ticket_issues",
              "ticket_dirs", "memories", "branch_files", "file_state"):
        out[t] = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    return out


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def add_tickets(db, p):
    db.execute("INSERT INTO tickets(project_path, id, title) VALUES (?, 'T-1', 't')", (p,))
    db.execute("INSERT INTO ticket_edges VALUES (?, 'T-1', 'T-2', 'deps')", (p,))
    db.execute("INSERT INTO ticket_issues(project_path, code) VALUES (?, 'x')", (p,))
    db.execute("INSERT INTO ticket_dirs(project_path, dir) VALUES (?, 'd')", (p,))
    db.commit()


def world(tmp_path):
    """main project + worktree (shared blob) + a keeper; the worktree dir is then removed (gone)."""
    main = tmp_path / "main"
    keep = tmp_path / "keep"
    wt = main / ".worktrees" / "E1-01"
    mk(main, "main")
    mk(keep, "keep", extra="def keeper_only():\n    return 1\n")
    db = mk(wt, "main/E1-01", extra="def wt_only_fn():\n    return 2\n")
    add_tickets(db, "main/E1-01")
    add_tickets(db, "keep")
    shutil.rmtree(wt)
    return main, keep, wt, db


# ---------------------------------------------------------------- classify

def test_classify_present_gone_unreachable(tmp_path):
    main = tmp_path / "main"
    (main / ".worktrees").mkdir(parents=True)
    assert forget.classify(str(main)) == "present"
    assert forget.classify(str(main / ".worktrees" / "E1-01")) == "gone"      # main checkout in place
    assert forget.classify(str(tmp_path / "tmpproj")) == "gone"               # parent exists
    assert forget.classify(str(tmp_path / "nodisk" / "proj")) == "unreachable"  # parent missing
    assert forget.classify(str(tmp_path / "nomain" / ".worktrees" / "E1-01")) == "unreachable"
    assert forget.classify(searchstats.FOREIGN_PREFIX + "bob/proj") == "unreachable"


# ---------------------------------------------------------------- dry run

def test_dry_run_changes_nothing_and_says_memory_stays(tmp_path):
    main, keep, wt, db = world(tmp_path)
    before_c = counts(open_db())
    reg = config.registry_path()
    before_reg = sha(reg)
    r = runner.invoke(app, ["forget-project", "--missing"])
    assert r.exit_code == 0, r.output
    assert "main/E1-01" in r.stdout and "state=gone" in r.stdout
    assert "repos=1" in r.stdout and "files=" in r.stdout and "chunks=" in r.stdout and "tickets=1" in r.stdout
    assert "vault memory is NOT removed" in r.stdout
    assert "dry run: 1 projects / 1 registry roots would be forgotten; rerun with --yes" in r.stdout
    assert "the sweep would also drop" in r.stdout and "(orphans, as in gc)" in r.stdout
    assert counts(open_db()) == before_c and sha(reg) == before_reg
    r = runner.invoke(app, ["forget-project", "main/E1-01"])      # explicit target without --yes: also a dry run
    assert r.exit_code == 0 and "dry run" in r.stdout and "the sweep would also drop" in r.stdout
    assert counts(open_db()) == before_c and sha(reg) == before_reg


# ---------------------------------------------------------------- --yes

def test_yes_forgets_project_keeps_others_memory_and_shared_vectors(tmp_path):
    main, keep, wt, db = world(tmp_path)
    db.execute("INSERT INTO memories(id, scope, text) VALUES ('m1', 'project:main/E1-01', 'remember me')")
    db.commit()
    wt_repo = db.execute("SELECT id FROM repos WHERE project_path='main/E1-01'").fetchone()[0]
    wt_blobs = {r[0] for r in db.execute("SELECT blob_sha FROM branch_files WHERE repo_id=?", (wt_repo,))}
    before = counts(db)
    r = runner.invoke(app, ["forget-project", "--missing", "--yes"])
    assert r.exit_code == 0, r.output
    assert "forgot 1 projects, 1 repos, 1 registry roots" in r.stdout
    db = open_db()
    assert db.execute("SELECT count(*) FROM projects WHERE path='main/E1-01'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM repos WHERE project_path='main/E1-01'").fetchone()[0] == 0
    for t in ("tickets", "ticket_edges", "ticket_issues", "ticket_dirs"):
        assert db.execute(f"SELECT count(*) FROM {t} WHERE project_path='main/E1-01'").fetchone()[0] == 0
        assert db.execute(f"SELECT count(*) FROM {t} WHERE project_path='keep'").fetchone()[0] == 1
    assert str(wt) not in config.load_registry()
    assert {str(main), str(keep)} <= set(config.load_registry())
    # memory untouched (index and vault stay)
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == before["memories"]
    # orphans swept: the worktree-only blob is gone, the blob shared with main/keep stays with its vector
    after = counts(db)
    assert after["blobs"] < before["blobs"] and after["chunks"] < before["chunks"]
    left = {r[0] for r in db.execute("SELECT sha FROM blobs")}
    shared = {r[0] for r in db.execute("SELECT blob_sha FROM branch_files WHERE repo_id IN "
                                       "(SELECT id FROM repos WHERE project_path='main')")}
    assert shared and shared <= left
    assert db.execute("SELECT count(*) FROM chunks WHERE blob_sha IN (%s)" % ",".join("?" * len(shared)),
                      list(shared)).fetchone()[0] > 0
    assert (wt_blobs - shared).isdisjoint(left)
    assert after["chunk_repo"] < before["chunk_repo"]
    orphans = db.execute("SELECT count(*) FROM chunk_repo WHERE repo_id NOT IN (SELECT id FROM repos)").fetchone()[0]
    assert orphans == 0


def test_yes_keeps_vault_memory_files(tmp_path):
    main, keep, wt, db = world(tmp_path)
    mem = config.home() / "vault" / "projects" / "main" / "E1-01"
    write(mem / "note.md", "# note\nkeep me\n")
    assert runner.invoke(app, ["forget-project", "--missing", "--yes"]).exit_code == 0
    assert (mem / "note.md").read_text() == "# note\nkeep me\n"


def test_yes_does_not_touch_child_projects(tmp_path):
    main = tmp_path / "par"
    mk(main, "par")
    child = tmp_path / "par-child"
    mk(child, "par/sub")
    shutil.rmtree(main)
    r = runner.invoke(app, ["forget-project", "par", "--yes"])
    assert r.exit_code == 0, r.output
    db = open_db()
    assert db.execute("SELECT count(*) FROM projects WHERE path='par'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM projects WHERE path='par/sub'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM repos WHERE project_path='par/sub'").fetchone()[0] == 1


def test_target_by_root_directory_and_present_warning_keeps_marker(tmp_path):
    keep = tmp_path / "keep"
    mk(keep, "keep")
    r = runner.invoke(app, ["forget-project", str(keep)])
    assert r.exit_code == 0 and "state=present" in r.stdout and "warning" in r.stdout
    assert runner.invoke(app, ["forget-project", str(keep), "--yes"]).exit_code == 0
    assert (keep / ".myvector.toml").exists() and (keep / "a.py").exists()
    assert open_db().execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    assert str(keep) not in config.load_registry()


def test_registry_only_root_is_forgotten(tmp_path):
    mk(tmp_path / "keep", "keep")        # the index must exist
    ghost = tmp_path / "ghost"           # gone: parent exists, never indexed
    config.register_root(str(ghost))
    r = runner.invoke(app, ["forget-project", "--missing"])
    assert "(registry only)" in r.stdout and "would be forgotten" in r.stdout
    assert str(ghost) in config.load_registry()
    assert runner.invoke(app, ["forget-project", "--missing", "--yes"]).exit_code == 0
    assert str(ghost) not in config.load_registry()


# ---------------------------------------------------------------- --missing selection

def test_missing_skips_unreachable_and_present(tmp_path):
    main, keep, wt, db = world(tmp_path)
    far = str(tmp_path / "unmounted" / "proj")
    foreign = searchstats.FOREIGN_PREFIX + "bob/proj"
    config.register_root(far)
    config.register_root(foreign)
    r = runner.invoke(app, ["forget-project", "--missing", "--yes"])
    assert r.exit_code == 0, r.output
    assert f"skipped: {far}" in r.stdout and f"skipped: {foreign}" in r.stdout
    reg = config.load_registry()
    assert far in reg and foreign in reg                       # unreachable stays
    assert str(keep) in reg and str(main) in reg               # present stays
    assert open_db().execute("SELECT count(*) FROM projects WHERE path='keep'").fetchone()[0] == 1


def test_explicit_target_may_forget_unreachable(tmp_path):
    mk(tmp_path / "keep", "keep")
    far = str(tmp_path / "unmounted" / "proj")
    config.register_root(far)
    assert runner.invoke(app, ["forget-project", far, "--yes"]).exit_code == 0
    assert far not in config.load_registry()


# ---------------------------------------------------------------- errors / lock

def test_unknown_target_exit_1(tmp_path):
    mk(tmp_path / "keep", "keep")
    r = runner.invoke(app, ["forget-project", "no/such"])
    assert r.exit_code == 1


def test_no_args_or_both_args_exit_2(tmp_path):
    assert runner.invoke(app, ["forget-project"]).exit_code == 2
    assert runner.invoke(app, ["forget-project", "x", "--missing"]).exit_code == 2


def test_busy_sync_lock_exit_1_and_nothing_written(tmp_path):
    main, keep, wt, db = world(tmp_path)
    before_c = counts(open_db())
    before_reg = sha(config.registry_path())
    with lock.sync_lock() as got:
        assert got
        r = runner.invoke(app, ["forget-project", "--missing", "--yes"])
    assert r.exit_code == 1
    assert "sync is running" in r.output
    assert counts(open_db()) == before_c and sha(config.registry_path()) == before_reg


# ---------------------------------------------------------------- hint in status / gc

def test_status_and_gc_hint_only_when_roots_missing(tmp_path):
    keep = tmp_path / "keep"
    mk(keep, "keep")
    assert "registry:" not in runner.invoke(app, ["status"]).stdout
    assert "registry:" not in runner.invoke(app, ["gc"]).stdout
    config.register_root(str(tmp_path / "ghost"))
    config.register_root(str(tmp_path / "nodisk" / "p"))
    st = runner.invoke(app, ["status"]).stdout
    assert "registry: 1 roots gone from disk (0 worktrees), 1 unreachable - run `myvec forget-project --missing` (dry run)" in st
    assert "registry: 1 roots gone" in runner.invoke(app, ["gc"]).stdout


def test_nothing_is_removed_automatically_by_gc_or_rebuild(tmp_path):
    main, keep, wt, db = world(tmp_path)
    before = sorted(config.load_registry())
    assert runner.invoke(app, ["gc"]).exit_code == 0
    assert sorted(config.load_registry()) == before
    assert runner.invoke(app, ["rebuild", "--no-import"]).exit_code == 0
    assert sorted(config.load_registry()) == before


def test_rebuild_after_forget_does_not_bring_worktree_back(tmp_path):
    main, keep, wt, db = world(tmp_path)
    assert runner.invoke(app, ["forget-project", "--missing", "--yes"]).exit_code == 0
    assert runner.invoke(app, ["rebuild", "--no-import"]).exit_code == 0
    assert open_db().execute("SELECT count(*) FROM projects WHERE path='main/E1-01'").fetchone()[0] == 0
    assert str(wt) not in config.load_registry()


# ---------------------------------------------------------------- units

def test_unregister_roots_returns_count_and_keeps_others():
    config.save_registry(["/a", "/b", "/c"])
    assert config.unregister_roots(["/b", "/zz"]) == 1
    assert config.load_registry() == ["/a", "/c"]
    assert config.unregister_roots(["/nope"]) == 0


def test_sweep_orphans_removes_only_unreferenced(tmp_path):
    keep = tmp_path / "keep"
    db = mk(keep, "keep")
    before = counts(db)
    assert indexer.sweep_orphans(db) == {"vectors": 0, "chunks": 0, "blobs": 0}
    assert counts(db) == before
    db.execute("DELETE FROM branch_files")
    db.commit()
    r = indexer.sweep_orphans(db)
    assert r["blobs"] > 0 and r["chunks"] > 0


def _swept(out):
    import re
    m = re.search(r"swept (\d+) vectors, (\d+) chunks, (\d+) blobs", out)
    return tuple(int(x) for x in m.groups())


def _previewed(out):
    import re
    m = re.search(r"sweep would also drop (\d+) vectors, (\d+) chunks, (\d+) blobs", out)
    return tuple(int(x) for x in m.groups())


def test_orphan_counts_equal_sweep(tmp_path):
    keep = tmp_path / "keep"
    db = mk(keep, "keep", extra="def other_fn():\n    return 3\n")
    assert indexer.orphan_counts(db) == {"vectors": 0, "chunks": 0, "blobs": 0}
    db.execute("DELETE FROM branch_files WHERE path='b.py'")
    db.commit()
    c = indexer.orphan_counts(db)
    assert c["blobs"] > 0 and c["chunks"] > 0 and c["vectors"] > 0
    assert indexer.sweep_orphans(db) == c


def test_dry_run_sweep_numbers_equal_yes(tmp_path):
    main, keep, wt, db = world(tmp_path)
    old = tmp_path / "old"
    mk(old, "old", extra="def old_only_fn():\n    return 9\n")
    db = open_db()
    rid = db.execute("SELECT id FROM repos WHERE project_path='old'").fetchone()[0]
    db.execute("DELETE FROM branch_files WHERE repo_id=?", (rid,))   # old orphans, unrelated to the forgotten project
    db.commit()
    before_c = counts(db)
    before_reg = sha(config.registry_path())
    r = runner.invoke(app, ["forget-project", "--missing"])
    assert r.exit_code == 0, r.output
    pre = _previewed(r.stdout)
    assert pre[0] > 0 and pre[1] > 0 and pre[2] > 0
    assert counts(open_db()) == before_c and sha(config.registry_path()) == before_reg
    r = runner.invoke(app, ["forget-project", "--missing", "--yes"])
    assert r.exit_code == 0, r.output
    assert _swept(r.stdout) == pre


def test_dry_run_empty_plan_has_no_sweep_line(tmp_path):
    keep = tmp_path / "keep"
    mk(keep, "keep")
    r = runner.invoke(app, ["forget-project", "--missing"])
    assert r.exit_code == 0 and "the sweep would also drop" not in r.stdout


# ---------------------------------------------------------------- review fixes

def test_classify_empty_parent_is_unreachable(tmp_path):
    mnt = tmp_path / "mnt_data"
    mnt.mkdir()                                              # empty mount point of an unmounted disk
    assert forget.classify(str(mnt / "proj")) == "unreachable"
    main = tmp_path / "emptymain"
    (main / ".worktrees").mkdir(parents=True)
    assert forget.classify(str(main / ".worktrees" / "E1-01")) == "gone"   # main checkout has content
    empty_main = tmp_path / "mnt2"
    empty_main.mkdir()
    assert forget.classify(str(empty_main / ".worktrees" / "E1-01")) == "unreachable"
    (mnt / "other").mkdir()
    assert forget.classify(str(mnt / "proj")) == "gone"


def test_relative_target(tmp_path, monkeypatch):
    main, keep, wt, db = world(tmp_path)
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["forget-project", "keep"])         # matches the project name first
    assert r.exit_code == 0 and "root=" + str(keep) in r.stdout
    r = runner.invoke(app, ["forget-project", "./keep/"])
    assert r.exit_code == 0 and "root=" + str(keep) in r.stdout


def test_symlinked_target(tmp_path):
    keep = tmp_path / "keep"
    mk(keep, "keep")
    link = tmp_path / "link"
    link.symlink_to(keep)
    r = runner.invoke(app, ["forget-project", str(link)])
    assert r.exit_code == 0, r.output
    assert "root=" + str(keep) in r.stdout


def test_yes_replans_inside_lock(tmp_path, monkeypatch):
    main, keep, wt, db = world(tmp_path)
    real = forget.plan
    calls = []

    def spy(db_, target=None, missing=False, items=None):
        calls.append(items is None)
        if len(calls) == 2:                                   # between first plan and write: worktree comes back
            wt.mkdir(parents=True)
        return real(db_, target, missing, items)

    monkeypatch.setattr(forget, "plan", spy)
    r = runner.invoke(app, ["forget-project", "--missing", "--yes"])
    assert r.exit_code == 0, r.output
    assert calls == [False, True]
    assert "nothing to forget" in r.stdout
    assert open_db().execute("SELECT count(*) FROM projects WHERE path='main/E1-01'").fetchone()[0] == 1
    assert str(wt) in config.load_registry()


def test_dry_run_without_db_does_not_create_it(tmp_path):
    assert not config.db_path().exists()
    r = runner.invoke(app, ["forget-project", "--missing"])
    assert r.exit_code == 0, r.output
    assert "no index yet" in r.stdout
    assert not config.db_path().exists()


def test_dry_run_explicit_target_sweep_numbers_equal_yes(tmp_path):
    main, keep, wt, db = world(tmp_path)
    old = tmp_path / "old"
    mk(old, "old", extra="def old_only_fn():\n    return 9\n")
    db = open_db()
    rid = db.execute("SELECT id FROM repos WHERE project_path='old'").fetchone()[0]
    db.execute("DELETE FROM branch_files WHERE repo_id=?", (rid,))
    db.commit()
    r = runner.invoke(app, ["forget-project", "main/E1-01"])
    assert r.exit_code == 0, r.output
    pre = _previewed(r.stdout)
    assert pre[1] > 0
    r = runner.invoke(app, ["forget-project", "main/E1-01", "--yes"])
    assert r.exit_code == 0, r.output
    assert _swept(r.stdout) == pre


def test_dry_run_takes_no_sync_lock(tmp_path):
    from myvec import lock as lockmod
    world(tmp_path)
    with lockmod.sync_lock() as got:      # a running sync holds the lock: dry run must not care
        assert got
        r = runner.invoke(app, ["forget-project", "--missing"])
    assert r.exit_code == 0, r.output
    assert "the sweep would also drop" in r.stdout and "sync already running" not in r.output


def test_sweep_preview_without_projects_equals_gc_counts(tmp_path):
    keep = tmp_path / "keep"
    db = mk(keep, "keep", extra="def other_fn():\n    return 3\n")
    db.execute("DELETE FROM branch_files WHERE path='b.py'")
    db.commit()
    it = forget.Item(root="/nowhere", state="gone", project=None, in_registry=True)
    assert forget.sweep_preview(db, [it]) == indexer.orphan_counts(db)
