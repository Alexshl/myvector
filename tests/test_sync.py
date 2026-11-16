from conftest import do_sync, git, write

from myvec import embed, filters


def count(db, sql, *a):
    return db.execute(sql, a).fetchone()[0]


def test_sync_indexes_and_filters(ws):
    st, db = do_sync(ws)
    assert st.repos == 3 and st.errors == []
    paths = {r[0] for r in db.execute("SELECT path FROM branch_files")}
    assert "src/Invoice.php" in paths and "README.md" in paths
    assert ".env" not in paths and "vendor/lib/x.php" not in paths and ".myvector.toml" not in paths
    # the private key chunk was skipped and counted, never stored
    assert st.secrets_skipped == 1
    assert count(db, "SELECT count(*) FROM chunks WHERE text LIKE '%PRIVATE KEY%'") == 0
    assert count(db, "SELECT sum(secret_skipped) FROM blobs") == 1
    assert count(db, "SELECT count(*) FROM chunks WHERE text LIKE '%hunter2%'") == 0
    # branch of pseudo-repo is '-'
    assert db.execute("SELECT current_branch FROM repos WHERE path='acme'").fetchone()[0] == "-"
    assert db.execute("SELECT current_branch FROM repos WHERE path='acme/site'").fetchone()[0] == "main"
    assert db.execute("SELECT file_state.sha FROM file_state").fetchone() is not None  # pseudo-repo hash cache


def test_sync_idempotent(ws):
    st1, db = do_sync(ws)
    n_chunks = count(db, "SELECT count(*) FROM chunks")
    n_vec = count(db, "SELECT count(*) FROM chunk_repo")
    embed.STATS.update(texts=0, calls=0)
    st2, db = do_sync(ws)
    assert st2.chunks_embedded == 0 and st2.new_blobs == 0
    assert embed.STATS["texts"] == 0
    assert count(db, "SELECT count(*) FROM chunks") == n_chunks
    assert count(db, "SELECT count(*) FROM chunk_repo") == n_vec
    assert count(db, "SELECT count(*) FROM chunks_vec") == n_vec


def test_working_tree_changes_and_untracked(ws):
    do_sync(ws)
    write(ws / "site/src/Invoice.php", (ws / "site/src/Invoice.php").read_text() + "\nfunction brand_new_fn() { return 'nebula'; }\n")
    write(ws / "site/untracked_file.py", "def untracked_symbol():\n    return 42\n")
    st, db = do_sync(ws)
    assert st.new_blobs == 2
    assert count(db, "SELECT count(*) FROM branch_files WHERE path='untracked_file.py'") == 1
    assert count(db, "SELECT count(*) FROM chunks WHERE text LIKE '%brand_new_fn%'") >= 1
    (ws / "site/untracked_file.py").unlink()
    do_sync(ws)
    assert count(db, "SELECT count(*) FROM branch_files WHERE path='untracked_file.py'") == 0


def test_branch_switch_reuses_embeddings(ws):
    site = ws / "site"
    do_sync(ws)
    git(site, "checkout", "-q", "-b", "feature")
    write(site / "src/Invoice.php", (site / "src/Invoice.php").read_text().replace("renderPdf", "renderFeatureOnly"))
    write(site / "feature_only.py", "def feature_only_fn():\n    return 'zeta'\n")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "feature")
    embed.STATS.update(texts=0, calls=0)
    st, db = do_sync(ws)
    assert st.new_blobs == 2                       # only the changed + the new file
    changed = embed.STATS["texts"]
    assert 0 < changed < 8
    assert {r[0] for r in db.execute("SELECT DISTINCT branch FROM branch_files WHERE repo_id=(SELECT id FROM repos WHERE path='acme/site')")} == {"main", "feature"}
    # back to main: zero embeddings
    git(site, "checkout", "-q", "main")
    embed.STATS.update(texts=0, calls=0)
    st, db = do_sync(ws)
    assert embed.STATS["texts"] == 0 and st.new_blobs == 0
    # and forth again: still zero
    git(site, "checkout", "-q", "feature")
    embed.STATS.update(texts=0, calls=0)
    do_sync(ws)
    assert embed.STATS["texts"] == 0


def test_same_blob_in_two_repos_reuses_vectors(ws):
    write(ws / "libs/Invoice_copy.php", (ws / "site/src/Invoice.php").read_text())
    git(ws / "libs", "add", "-A")
    git(ws / "libs", "commit", "-q", "-m", "copy")
    embed.STATS.update(texts=0, calls=0)
    st, db = do_sync(ws)
    assert st.vectors_reused > 0
    # one chunk row per blob, but one vector row per (repo, chunk)
    pairs = db.execute("SELECT chunk_id, count(*) FROM chunk_repo GROUP BY chunk_id HAVING count(*) > 1").fetchall()
    assert pairs


def test_gc_drops_deleted_branches(ws):
    site = ws / "site"
    do_sync(ws)
    git(site, "checkout", "-q", "-b", "temp")
    write(site / "temp_only.py", "def temp_only():\n    return 'ephemeral'\n")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "t")
    _, db = do_sync(ws)
    assert count(db, "SELECT count(*) FROM chunks WHERE text LIKE '%ephemeral%'") == 1
    git(site, "checkout", "-q", "main")
    git(site, "branch", "-D", "temp")
    from myvec import indexer
    res = indexer.gc(db)
    assert res["manifests"] >= 1 and res["chunks"] >= 1
    assert count(db, "SELECT count(*) FROM chunks WHERE text LIKE '%ephemeral%'") == 0
    assert count(db, "SELECT count(*) FROM chunks_vec") == count(db, "SELECT count(*) FROM chunk_repo")
    assert count(db, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'ephemeral'") == 0


def test_include_exclude_globs(ws):
    write(ws / ".myvector.toml", 'name = "acme"\ninclude = []\nexclude = ["site/docs/**"]\n')
    _, db = do_sync(ws)
    assert count(db, "SELECT count(*) FROM branch_files WHERE path LIKE 'docs/%'") == 0
    assert count(db, "SELECT count(*) FROM branch_files WHERE path LIKE 'src/%'") == 1


def test_large_and_binary_files_skipped(ws):
    write(ws / "site/big.txt", "x" * (filters.MAX_FILE_BYTES + 10))
    (ws / "site/blob.dat2").write_bytes(b"abc\x00def" * 10)
    git(ws / "site", "add", "-A")
    git(ws / "site", "commit", "-q", "-m", "big")
    _, db = do_sync(ws)
    assert count(db, "SELECT count(*) FROM chunks c JOIN blobs b ON b.sha=c.blob_sha WHERE b.skip_reason IS NOT NULL") == 0
    reasons = {r[0] for r in db.execute("SELECT skip_reason FROM blobs WHERE skip_reason IS NOT NULL")}
    assert "binary" in reasons and "too-large" in reasons


def test_reembed_after_dims_change(ws, env):
    from myvec import config, db as dbmod, indexer
    from myvec.embed import get_embedder
    do_sync(ws)
    (env / "config.toml").write_text(config.default_config_text().replace("dims = 768", "dims = 32"))
    cfg = config.load_config()
    try:
        dbmod.open_ready(cfg)
        assert False, "expected mismatch"
    except dbmod.ModelMismatch as e:
        assert "reembed" in str(e)
    db = dbmod.connect()
    dbmod.init_schema(db, cfg)
    indexer.reembed(db, cfg, get_embedder(cfg))
    db = dbmod.open_ready(cfg)  # now consistent
    n = db.execute("SELECT count(*) FROM chunk_repo").fetchone()[0]
    assert n == db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] > 0
