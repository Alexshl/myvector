import json
import sqlite3

import pytest
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import chunk, config, db as dbmod, projects
from myvec import search as S
from myvec.commands import app
from myvec.config import Config

runner = CliRunner()


def search_json(*args):
    r = runner.invoke(app, ["search", *args, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


# ------------------------------------------------------------------ 1. model defaults

def test_default_model_is_embeddinggemma_768():
    c = Config()
    assert (c.model, c.dims) == ("embeddinggemma", 768)
    assert c.prefix_pair() == ("task: search result | query: ", "title: none | text: ")
    t = config.default_config_text()
    assert 'model = "embeddinggemma"' in t and "dims = 768" in t
    assert "output_budget_tokens = 2000" in t and "expand_top = 3" in t and "expand_lines = 25" in t and "k = 10" in t


def test_legacy_token_budget_key_and_new_keys(env):
    p = env / "config.toml"
    p.write_text(p.read_text().replace("output_budget_tokens = 2000", "token_budget = 700"))
    assert config.load_config().output_budget_tokens == 700
    p.write_text(p.read_text() + "\n")
    p.write_text(p.read_text().replace("expand_top = 3", "expand_top = 1").replace("k = 10", "k = 4"))
    c = config.load_config()
    assert (c.expand_top, c.k) == (1, 4)


def test_model_change_requires_reembed(ws, env):
    do_sync(ws)
    p = env / "config.toml"
    p.write_text(p.read_text().replace('model = "embeddinggemma"', 'model = "bge-m3"'))
    with pytest.raises(dbmod.ModelMismatch):
        dbmod.open_ready(config.load_config())


# ------------------------------------------------------------------ 2. chunk size cap

def test_oversized_chunks_are_split_not_truncated():
    cap = chunk.MAX_CHUNK_CHARS
    long_line = "x" * (cap * 2 + 100)
    lang, chs = chunk.chunk_file("data.txt", "first\n" + long_line + "\nlast\n")
    assert all(len(c.text) <= cap for c in chs)
    assert sum(c.text.count("x") for c in chs) == len(long_line)          # nothing lost
    body = "\n".join(f"    $v{i} = '{'y' * 180}';" for i in range(120))
    php = f"<?php\nfunction huge_function_zz() {{\n{body}\n}}\n"
    lang, chs = chunk.chunk_file("big.php", php)
    assert len(chs) > 3 and all(len(c.text) <= cap for c in chs)
    assert all(len(chunk.embed_text("some/long/path/big.php", c)) <= chunk.EMBED_MAX_CHARS for c in chs)
    md = "# T\n\n" + "\n".join("word " * 60 for _ in range(200))
    _, chs = chunk.chunk_file("a.md", md)
    assert all(len(c.text) <= cap for c in chs)


def test_windows_keep_line_numbers_and_overlap():
    lines = [f"line{i}" for i in range(1, 131)]
    chs = chunk._windows(lines, 1, 130, None)
    assert [(c.start_line, c.end_line) for c in chs] == [(1, 60), (51, 110), (101, 130)]


# ------------------------------------------------------------------ 3. no trigram table

def test_trigram_table_dropped_on_upgrade(ws):
    _, db = do_sync(ws)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_tri'").fetchone()
    # simulate a v2 database that still has the trigram table and its triggers
    db.execute("DROP TRIGGER chunks_ai")
    db.execute("DROP TRIGGER chunks_ad")
    db.execute("CREATE VIRTUAL TABLE chunks_tri USING fts5(text, symbol, content='chunks', content_rowid='id', tokenize='trigram')")
    db.execute("CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN "
               "INSERT INTO chunks_fts(rowid, text, symbol) VALUES (new.id, new.text, coalesce(new.symbol, '')); "
               "INSERT INTO chunks_tri(rowid, text, symbol) VALUES (new.id, new.text, coalesce(new.symbol, '')); END")
    db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    db2 = dbmod.open_ready()
    assert not db2.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_tri'").fetchone()
    assert dbmod.get_meta(db2, "schema_version") == str(dbmod.SCHEMA_VERSION)
    sql = " ".join(r[0] for r in db2.execute("SELECT sql FROM sqlite_master WHERE type='trigger'"))
    assert "chunks_tri" not in sql
    write(ws / "site/after_upgrade.py", "def after_upgrade_symbol():\n    return 1\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "u")
    do_sync(ws)                                                   # inserts go through the new trigger
    assert db2.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'after_upgrade_symbol'").fetchone()[0] == 1


# ------------------------------------------------------------------ 4. query routing

@pytest.mark.parametrize("q,route", [
    ("renderPdf", "hybrid"), ("snake_case_name", "hybrid"), ("Foo::bar", "hybrid"), ("$this->getToken", "hybrid"),
    ("a.b.c", "hybrid"), ("накладні", "hybrid"),
    ("where is the vat calculated", "vector"), ("де рахується ПДВ", "vector"), ("two words", "vector"),
])
def test_resolve_route_auto(q, route):
    assert S.resolve_route(q) == route


def test_resolve_route_override():
    assert S.resolve_route("renderPdf", "vector") == "vector"
    assert S.resolve_route("where is the vat", "hybrid") == "hybrid"


@pytest.fixture
def synced(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    return ws


def test_vector_route_does_not_touch_fts(synced, monkeypatch):
    calls = []
    orig_c, orig_m = S._code_fts, S._mem_fts
    monkeypatch.setattr(S, "_code_fts", lambda *a, **k: calls.append("code") or orig_c(*a, **k))
    monkeypatch.setattr(S, "_mem_fts", lambda *a, **k: calls.append("mem") or orig_m(*a, **k))
    assert search_json("invoice total price qty")                      # natural language -> vector only
    assert calls == []
    search_json("renderPdf")                                           # identifier -> hybrid
    assert calls == ["code", "mem"]
    calls.clear()
    search_json("invoice total price qty", "--mode", "hybrid")
    assert calls == ["code", "mem"]
    calls.clear()
    search_json("renderPdf", "--mode", "vector")
    assert calls == []
    assert runner.invoke(app, ["search", "x", "--mode", "bogus"]).exit_code != 0


def test_hybrid_finds_identifier_that_vector_misses(synced, ws):
    write(ws / "site/odd.py", "def zzqxq_unique_ident():\n    return 'q'\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "odd")
    do_sync(ws)
    assert search_json("zzqxq_unique_ident", "--kind", "code", "--k", "1")[0]["symbol"] == "zzqxq_unique_ident"


def test_memory_follows_same_rule(synced, monkeypatch):
    runner.invoke(app, ["remember", "the quarterly report is generated by cron"])
    calls = []
    orig = S._mem_fts
    monkeypatch.setattr(S, "_mem_fts", lambda *a, **k: calls.append(1) or orig(*a, **k))
    search_json("quarterly report cron", "--kind", "memory")
    assert not calls
    assert search_json("cron", "--kind", "memory") and calls


# ------------------------------------------------------------------ 5. output format

@pytest.fixture
def many(ws):
    for i in range(12):
        body = "\n".join(f"    value_{i}_{j} = compute_item({j})  # filler line {j} for bulkmarker" for j in range(30))
        write(ws / f"site/mod{i:02d}.py", f"def bulkmarker_{i}():\n{body}\n    return 0\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "bulk")
    do_sync(ws)


def test_output_top3_expanded_rest_short(many, ws, monkeypatch):
    monkeypatch.chdir(ws / "site")
    r = runner.invoke(app, ["search", "bulkmarker", "--kind", "code", "--expand"])
    assert r.exit_code == 0
    blocks = []
    for ln in r.stdout.splitlines():
        if ln.startswith("total in index"):   # the identifier total (E8-13), not a hit block
            continue
        if ln.startswith("["):
            blocks.append([ln])
        else:
            blocks[-1].append(ln)
    assert len(blocks) == 10                                           # default k = 10
    assert [len(b) - 1 for b in blocks[:3]] == [25, 25, 25]
    assert all(len(b) - 1 == 3 for b in blocks[3:])
    assert len(r.stdout) / 4 <= config.load_config().output_budget_tokens
    full = runner.invoke(app, ["search", "bulkmarker", "--kind", "code", "--full", "--k", "1"]).stdout
    assert full.count("\n") > 30


def test_output_budget_cuts_hits(many, ws, monkeypatch, env):
    monkeypatch.chdir(ws / "site")
    p = env / "config.toml"
    p.write_text(p.read_text().replace("output_budget_tokens = 2000", "output_budget_tokens = 300"))
    r = runner.invoke(app, ["search", "bulkmarker", "--kind", "code"])
    assert r.stdout.count("\n[") + 1 < 10


# ------------------------------------------------------------------ 6. bit[] coarse pass for big cross-project scans

def test_bit_index_maintained(ws):
    _, db = do_sync(ws)
    n = db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
    assert n > 0 and db.execute("SELECT count(*) FROM chunks_bit").fetchone()[0] == n
    from myvec import indexer
    write(ws / "site/new.py", "def n():\n    return 1\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "n")
    git(ws / "site", "rm", "-q", "new.py"); git(ws / "site", "commit", "-q", "-m", "rm")
    do_sync(ws)
    indexer.gc(db)
    assert db.execute("SELECT count(*) FROM chunks_bit").fetchone()[0] == db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]


def test_scope_all_uses_bit_pass_above_threshold(synced, ws, monkeypatch):
    plain = search_json("compute invoice total", "--scope", "all", "--kind", "code")
    used = []
    orig = S._bit_candidates
    monkeypatch.setattr(S, "_bit_candidates", lambda *a, **k: used.append(1) or orig(*a, **k))
    search_json("compute invoice total", "--scope", "all", "--kind", "code")
    assert not used                                                    # below 100k rows: plain float32 scan
    monkeypatch.setattr(S, "BIT_THRESHOLD", 0)
    bit = search_json("compute invoice total", "--scope", "all", "--kind", "code")
    assert used and [h["chunk_id"] for h in bit] == [h["chunk_id"] for h in plain]
    assert [h["score"] for h in bit] == [h["score"] for h in plain]    # float32 rescoring: same ranks
    used.clear()
    search_json("compute invoice total", "--scope", "project", "--kind", "code")
    assert not used                                                    # only scope=all


def test_bit_index_backfilled_when_missing(synced, monkeypatch):
    db = dbmod.open_ready()
    db.execute("DELETE FROM chunks_bit")                               # e.g. index built before the bit table existed
    monkeypatch.setattr(S, "BIT_THRESHOLD", 0)
    assert search_json("compute invoice total", "--scope", "all", "--kind", "code")
    assert db.execute("SELECT count(*) FROM chunks_bit").fetchone()[0] == db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] > 0


# ------------------------------------------------------------------ 7. exclude sub-dirs

def test_exclude_skips_subdirs_for_discovery_and_walk(ws):
    cloud = ws / "site-cloud"
    cloud.mkdir()
    git(cloud, "init", "-q", "-b", "main")
    write(cloud / "cloud.py", "def cloud_only_symbol():\n    return 1\n")
    git(cloud, "add", "-A"); git(cloud, "commit", "-q", "-m", "c")
    write(ws / "docs-old/old.md", "# Old\n\nObsolete docs about the legacy_thing which must not be indexed at all.\n")
    st, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM repos WHERE path='acme/site-cloud'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM branch_files WHERE path='docs-old/old.md'").fetchone()[0] == 1
    write(ws / ".myvector.toml", 'name = "acme"\ninclude = []\nexclude = ["site-cloud", "docs-old"]\n')
    m = projects.find_marker(ws)
    repos, _ = projects.discover(m)
    assert {r.path for r in repos} == {"acme", "acme/site", "acme/libs"}
    st, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM repos WHERE path='acme/site-cloud'").fetchone()[0] == 0   # dropped
    assert db.execute("SELECT count(*) FROM branch_files WHERE path LIKE 'docs-old/%'").fetchone()[0] == 0
    from myvec import indexer
    indexer.gc(db)                                                    # unreferenced chunks go with gc
    assert db.execute("SELECT count(*) FROM chunks WHERE text LIKE '%cloud_only_symbol%' OR text LIKE '%legacy_thing%'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM branch_files WHERE path='README.md'").fetchone()[0] == 1   # rest untouched


def test_exclude_patterns_with_globs_and_nested(ws):
    write(ws / "site/generated/out/a.py", "def gen_a():\n    return 1\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "g")
    write(ws / ".myvector.toml", 'name = "acme"\nexclude = ["site/generated"]\n')
    _, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM branch_files WHERE path LIKE 'generated/%'").fetchone()[0] == 0


def test_watcher_filter_honours_exclude(ws):
    from myvec.watcher import EventFilter
    write(ws / ".myvector.toml", 'name = "acme"\nexclude = ["docs-old"]\n')
    f = EventFilter.for_project(ws)
    assert not f.relevant(ws / "docs-old/x.md") and f.relevant(ws / "site/src/Invoice.php")
