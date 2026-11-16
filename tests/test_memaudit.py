import hashlib
import json
import sqlite3

import pytest
from typer.testing import CliRunner

from myvec import config, db as dbmod, memaudit
from myvec.commands import app

runner = CliRunner()
DIMS = 64
AKIA = "AKIAIOSFODNN7EXAMPLE"
PEM = "-----BEGIN RSA PRIVATE KEY-----"


def vec(*axes):
    v = [0.0] * DIMS
    for a, w in axes:
        v[a] = w
    return v


class Builder:
    def __init__(self):
        self.db = dbmod.open_ready()
        self.k = 0

    def add(self, text, scope="acme", source="vault", v=None, vault_file=None, pinned=0, branch=None,
            superseded_by=None, created="2026-11-01T10:00:00+00:00", mid=None):
        self.k += 1
        mid = mid or f"m{self.k:03d}"
        n = self.db.execute(
            'INSERT INTO memories(id, scope, branch, vault_file, text, created, updated, pinned, source, superseded_by) '
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, scope, branch, vault_file, text, created, created, pinned, source, superseded_by)).lastrowid
        if v is not None:
            self.db.execute("INSERT INTO memories_vec(rowid, scope, embedding) VALUES(?,?,?)", (n, scope, dbmod.pack(v)))
        return mid

    def done(self):
        self.db.commit()
        self.db.close()


@pytest.fixture
def b():
    x = Builder()
    yield x


def ro():
    p = config.db_path()
    import sqlite_vec
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def run_cli(*args, code=0):
    r = runner.invoke(app, ["memory-audit", *args])
    assert r.exit_code == code, r.output
    return r


# ------------------------------------------------------------------ audit aggregates

def test_audit_aggregates_synthetic(b):
    b.add("Title\nbody one", scope="acme", pinned=1, branch="feat", created="2026-10-05T00:00:00")
    b.add("Title\nbody two", scope="global", created="2026-11-02T00:00:00")
    b.add("# R\nshared body", scope="acme", source="import", vault_file="/x/acme/CLAUDE.md")
    b.add("# R\nshared body", scope="other", source="import", vault_file="/x/other/CLAUDE.md")
    b.add("# R\nshared body", scope="third", source="import", vault_file="/x/third/CLAUDE.md")
    b.add("## S\nunique", scope="acme", source="import", vault_file="/x/acme/GRAPH_REPORT.md")
    b.add("note\nm", scope="acme", source="import", vault_file="/x/acme/feedback_x.md")
    b.add("old\nbody", scope="acme", superseded_by="m001")
    b.done()
    r = memaudit.audit(ro())
    assert r["active"] == {"import/project": 5, "vault/global": 1, "vault/project": 1}
    assert r["active_total"] == 7
    assert r["archived"] == {"vault/project": 1}
    assert r["scopes"] == 4 and r["scope_sizes"] == [4, 1, 1, 1]
    assert r["pinned"] == 1 and r["with_branch"] == 1
    assert r["by_month"]["2026-10"] == 1 and r["by_month"]["2026-11"] == 6
    assert r["import_by_type"] == {"CLAUDE.md": 3, "GRAPH_REPORT.md": 1, "auto-memory": 1}
    assert r["import_duplicate_groups"] == 1 and r["import_duplicate_max_scopes"] == 3


def test_audit_archived_excluded_from_active_and_scope_counts(b):
    b.add("a\nb", scope="gone", superseded_by="x")
    b.add("a\nb", scope="live")
    b.done()
    r = memaudit.audit(ro())
    assert r["scopes"] == 1 and r["active_total"] == 1


def test_audit_missing_created_goes_to_unknown(b):
    b.add("a\nb", created=None)
    b.done()
    assert memaudit.audit(ro())["by_month"] == {"unknown": 1}


def test_audit_import_without_vault_file_is_other(b):
    b.add("a\nb", source="import", vault_file=None)
    b.done()
    assert memaudit.audit(ro())["import_by_type"] == {"other": 1}


def test_audit_duplicates_same_scope_not_a_group(b):
    b.add("h\nsame", scope="acme", source="import", vault_file="/a/CLAUDE.md")
    b.add("h\nsame", scope="acme", source="import", vault_file="/a/sub/CLAUDE.md")
    b.done()
    r = memaudit.audit(ro())
    assert r["import_duplicate_groups"] == 0 and r["import_duplicate_max_scopes"] == 0


def test_audit_ignores_vault_duplicates(b):
    b.add("h\nsame", scope="a")
    b.add("h\nsame", scope="b")
    b.done()
    assert memaudit.audit(ro())["import_duplicate_groups"] == 0


def test_audit_duplicate_ignores_title_line(b):
    b.add("Title A\nsame body", scope="a", source="import", vault_file="/a/CLAUDE.md")
    b.add("Title B\nsame body", scope="b", source="import", vault_file="/b/CLAUDE.md")
    b.done()
    assert memaudit.audit(ro())["import_duplicate_groups"] == 1


def test_audit_output_has_no_text_or_scope_names(b):
    b.add("Secret title zebra\nbody quokka", scope="my-private-scope", vault_file="/home/u/vault/projects/x.md")
    b.done()
    dump = json.dumps(memaudit.audit(ro()))
    for s in ("zebra", "quokka", "my-private-scope", "/home/u"):
        assert s not in dump


def test_body_hash_one_line_text():
    assert memaudit._body_hash("only title") == hashlib.sha1(b"").hexdigest()


# ------------------------------------------------------------------ candidates

def test_candidates_only_active_vault_project(b):
    b.add("vault proj", scope="acme", v=vec((0, 1)), mid="vp")
    b.add("vault glob", scope="global", v=vec((0, 1)), mid="vg")
    b.add("imported", scope="beta", source="import", v=vec((0, 1)), vault_file="/x/CLAUDE.md", mid="imp")
    b.add("archived", scope="acme", v=vec((0, 1)), superseded_by="vp", mid="arch")
    b.done()
    out = memaudit.candidates(ro())
    assert [c["id"] for c in out] == ["vp"]


def test_candidates_import_never_listed_but_counts_as_similarity_peer(b):
    b.add("vault proj", scope="acme", v=vec((0, 1)), mid="vp")
    b.add("imported", scope="beta", source="import", v=vec((0, 1)), vault_file="/x/CLAUDE.md")
    b.done()
    c = memaudit.candidates(ro())
    assert [x["id"] for x in c] == ["vp"]
    assert c[0]["other_scopes_075"] == 1 and c[0]["max_cos_other"] == 1.0


def test_candidates_global_is_not_a_similarity_peer(b):
    b.add("proj", scope="acme", v=vec((0, 1)), mid="vp")
    b.add("glob", scope="global", v=vec((0, 1)))
    b.done()
    c = memaudit.candidates(ro())
    assert c[0]["other_scopes_075"] == 0 and c[0]["max_cos_other"] == 0.0


def test_candidates_same_scope_not_a_peer(b):
    b.add("a", scope="acme", v=vec((0, 1)), mid="a1")
    b.add("b", scope="acme", v=vec((0, 1)), mid="a2")
    b.done()
    assert all(c["other_scopes_075"] == 0 for c in memaudit.candidates(ro()))


def test_candidates_threshold_075(b):
    # cos(vp, near) = 0.75 exactly-ish (>=), cos(vp, far) < 0.75
    b.add("vp", scope="acme", v=vec((0, 1)), mid="vp")
    b.add("near", scope="s1", v=vec((0, 0.8), (1, 0.6)))   # cos 0.8
    b.add("far", scope="s2", v=vec((0, 0.7), (1, (1 - 0.49) ** 0.5)))   # cos 0.7
    b.done()
    c = next(x for x in memaudit.candidates(ro()) if x["id"] == "vp")
    assert c["other_scopes_075"] == 1 and c["max_cos_other"] == 0.8


def test_candidates_counts_scopes_not_memories(b):
    b.add("vp", scope="acme", v=vec((0, 1)), mid="vp")
    for i in range(3):
        b.add(f"n{i}", scope="s1", v=vec((0, 1)))
    b.done()
    c = next(x for x in memaudit.candidates(ro()) if x["id"] == "vp")
    assert c["other_scopes_075"] == 1


def test_candidates_sorted_by_scopes_then_cos(b):
    b.add("lowcos", scope="a", v=vec((5, 1)), mid="lo")
    b.add("one scope", scope="b", v=vec((0, 1)), mid="one")
    b.add("two scopes", scope="c", v=vec((1, 1)), mid="two")
    b.add("peer1", scope="p1", v=vec((0, 0.9), (9, 0.43)))
    b.add("peer2", scope="p2", v=vec((1, 1)))
    b.add("peer3", scope="p3", v=vec((1, 1)))
    b.done()
    ids = [c["id"] for c in memaudit.candidates(ro())]
    assert ids.index("two") < ids.index("one") < ids.index("lo")


def test_candidates_limit(b):
    for i in range(5):
        b.add(f"t{i}", scope=f"s{i}", v=vec((i, 1)))
    b.done()
    assert len(memaudit.candidates(ro(), limit=3)) == 3


def test_candidates_without_vector_does_not_crash(b):
    b.add("no vec", scope="acme", mid="nv")
    b.add("other", scope="beta", v=vec((0, 1)))
    b.done()
    c = memaudit.candidates(ro())
    nv = next(x for x in c if x["id"] == "nv")
    assert nv["max_cos_other"] == 0.0 and nv["other_scopes_075"] == 0


def test_candidates_zero_vector_ignored(b):
    b.add("zero", scope="acme", v=vec(), mid="z")
    b.add("other", scope="beta", v=vec((0, 1)))
    b.done()
    z = next(x for x in memaudit.candidates(ro()) if x["id"] == "z")
    assert z["max_cos_other"] == 0.0


@pytest.mark.parametrize("text,expected", [
    ("use /etc/nginx for it", True),
    ("edit config.toml now", True),
    ("call `foo` here", True),
    ("plain preference about tea", False),
])
def test_candidates_project_refs(b, text, expected):
    b.add(text, scope="acme", v=vec((0, 1)))
    b.done()
    assert memaudit.candidates(ro())[0]["has_project_refs"] is expected


def test_candidates_scope_leaf_name_counts_as_ref(b):
    b.add("the Billing service is slow", scope="/home/u/billing", v=vec((0, 1)))
    b.done()
    assert memaudit.candidates(ro())[0]["has_project_refs"] is True


def test_first_line_truncated_to_100(b):
    b.add("x" * 300 + "\nsecond", scope="acme", v=vec((0, 1)))
    b.done()
    assert memaudit.candidates(ro())[0]["first"] == "x" * 100


def test_first_line_masks_literal_secret(b):
    b.add("config password = hunter2hunter2 ok\nmore", scope="acme", v=vec((0, 1)))
    b.done()
    first = memaudit.candidates(ro())[0]["first"]
    assert "hunter2hunter2" not in first and "•••" in first


@pytest.mark.parametrize("secret", [AKIA, PEM, "ghp_" + "a" * 36, "sk_live_abcdefgh12345678"])
def test_first_line_with_hard_secret_is_hidden(b, secret):
    b.add(f"key is {secret} keep\nbody", scope="acme", v=vec((0, 1)))
    b.done()
    first = memaudit.candidates(ro())[0]["first"]
    assert first == "[hidden]" and secret not in first


def test_first_line_secret_beyond_100_chars_not_leaked(b):
    # the cut happens before the check: the part of a token past char 100 is gone
    b.add("y" * 98 + AKIA + "\nbody", scope="acme", v=vec((0, 1)))
    b.done()
    first = memaudit.candidates(ro())[0]["first"]
    assert AKIA not in first


def test_first_line_hard_secret_on_second_line_not_in_first(b):
    b.add(f"title\n{AKIA}", scope="acme", v=vec((0, 1)))
    b.done()
    assert memaudit.candidates(ro())[0]["first"] == "title"


def test_hidden_marker_matches_searchlog_policy():
    from myvec import searchlog
    line = f"x {AKIA}"
    assert searchlog.mask_q(line) == searchlog.HIDDEN
    assert AKIA not in memaudit._first_line(line)


# ------------------------------------------------------------------ CLI

def seed_cli(b):
    b.add("Zebra title /srv/app\nquokka body", scope="acme", v=vec((0, 1)), mid="vp1",
          vault_file="/home/u/MyVector/vault/projects/acme/z.md")
    b.add("Other\nbody", scope="beta", v=vec((0, 1)), mid="vp2")
    b.add("# R\nshared", scope="acme", source="import", vault_file="/x/acme/CLAUDE.md")
    b.done()


def test_cli_missing_db_exits_1(env):
    assert not config.db_path().exists()
    r = run_cli(code=1)
    assert "no index database" in r.output
    assert not config.db_path().exists()


def test_cli_empty_index_exits_1(env):
    dbmod.open_ready().close()
    r = run_cli(code=1)
    assert "no memories" in r.output


def test_cli_text_aggregates_only(b):
    seed_cli(b)
    out = run_cli().output
    assert "active memories: 3 in 2 scopes" in out
    for s in ("Zebra", "quokka", "acme", "beta", "/srv/app", "/home/u", "candidates for global"):
        assert s not in out


def test_cli_json_aggregates_only(b):
    seed_cli(b)
    r = run_cli("--json")
    d = json.loads(r.stdout)
    assert d["active_total"] == 3 and "candidates" not in d
    for s in ("Zebra", "quokka", "acme", "beta", "/srv/app", "/home/u", "vp1", '"first"', '"text"'):
        assert s not in r.stdout


def test_cli_json_candidates_adds_array(b):
    seed_cli(b)
    d = json.loads(run_cli("--json", "--candidates").stdout)
    assert {c["id"] for c in d["candidates"]} == {"vp1", "vp2"}
    assert all(set(c) == {"id", "scope", "first", "max_cos_other", "other_scopes_075", "has_project_refs"}
               for c in d["candidates"])


def test_cli_candidates_text_table_masks_hard_secret(b):
    b.add(f"token {AKIA}\nbody", scope="acme", v=vec((0, 1)))
    b.done()
    out = run_cli("--candidates").output
    assert "candidates for global: 1" in out and AKIA not in out and "[hidden]" in out


def test_cli_candidates_json_masks_secrets(b):
    b.add(f"{PEM} x\nbody", scope="acme", v=vec((0, 1)))
    b.add("password = hunter2hunter2\nbody", scope="beta", v=vec((1, 1)))
    b.done()
    out = run_cli("--json", "--candidates").stdout
    assert "PRIVATE KEY" not in out and "hunter2hunter2" not in out


def test_cli_only_archived_still_reports(b):
    b.add("a\nb", superseded_by="x")
    b.done()
    out = run_cli().output
    assert "active memories: 0" in out and "archived:" in out


# ------------------------------------------------------------------ read-only

def snapshot(env):
    files = {}
    for p in sorted(env.rglob("*")):
        if p.is_file() and p.name not in ("index.db-shm", "index.db-wal"):   # WAL sidecars: opened by any reader
            files[str(p.relative_to(env))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def test_cli_writes_nothing_anywhere(b, env):
    seed_cli(b)
    (env / "vault").mkdir(exist_ok=True)
    (env / "vault/note.md").write_text("x")
    (env / "logs").mkdir(exist_ok=True)
    before = snapshot(env)
    dirs_before = sorted(str(p.relative_to(env)) for p in env.rglob("*") if p.is_dir())
    for args in ([], ["--json"], ["--candidates"], ["--json", "--candidates"]):
        run_cli(*args)
    assert snapshot(env) == before
    assert sorted(str(p.relative_to(env)) for p in env.rglob("*") if p.is_dir()) == dirs_before
    wal = env / "index.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0   # no data written through the WAL


def test_cli_does_not_touch_db_mtime_or_wal(b, env):
    seed_cli(b)
    st = config.db_path().stat()
    run_cli("--candidates")
    st2 = config.db_path().stat()
    assert (st.st_mtime_ns, st.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert not list(env.glob("index.db-wal")) or (env / "index.db-wal").stat().st_size == 0


def test_cli_does_not_call_embedder(b, monkeypatch):
    from myvec import embed
    seed_cli(b)

    def boom(*a, **k):
        raise AssertionError("embedder used")
    monkeypatch.setattr(embed, "get_embedder", boom)
    monkeypatch.setattr(embed.FakeEmbedder, "_raw", boom)
    run_cli("--candidates")
    assert embed.STATS["calls"] == 0


def test_connection_is_read_only(b):
    seed_cli(b)
    db = ro()
    with pytest.raises(sqlite3.OperationalError):
        db.execute("UPDATE memories SET pinned=1")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO memories(id, scope, text) VALUES('a','b','c')")


def test_memaudit_module_has_no_heavy_imports():
    import re
    from pathlib import Path
    src = Path(memaudit.__file__).read_text()
    assert not re.search(r"^\s*(import|from)\s+(typer|httpx|requests)", src, re.M)
    assert not re.search(r"^\s*(import|from)\s+myvec(\.| import )?\s*embed", src, re.M)
