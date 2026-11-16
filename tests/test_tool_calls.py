"""tool_calls in tokens.db (ADR-029): categories only, never the command text. Synthetic transcripts in tmp."""
import json
import sqlite3

import pytest

from myvec import config, relocate, tokens as T
from myvec import db as dbmod

SENTINEL = "ZQXSENTINELCMDZQX"
TS = "2026-11-09T10:00:00.000Z"


def tool(tid, name, inp, *, mid=None, cwd="/p/acme", sid="s", ts=TS, usage=True):
    msg = {"id": mid or f"m-{tid}", "model": "claude-sonnet-5-5",
           "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}
    if usage:
        msg["usage"] = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 5}
    d = {"type": "assistant", "requestId": f"r-{tid}", "cwd": cwd, "sessionId": sid, "timestamp": ts, "message": msg}
    return (json.dumps(d) + "\n").encode()


def bash(tid, cmd, **kw):
    return tool(tid, "Bash", {"command": cmd}, **kw)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "claude_projects"
    r.mkdir()
    return r


@pytest.fixture
def tdb():
    db = T.open_db()
    yield db
    db.close()


def put(root, rel, *lines):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "ab") as f:
        for ln in lines:
            f.write(ln)
    return p


def calls(tdb):
    return [tuple(r) for r in tdb.execute("SELECT tool_id, cat, seq, cwd FROM tool_calls ORDER BY file_id, seq")]


# ------------------------------------------------------------------ classifier

@pytest.mark.parametrize("name,inp,cat", [
    ("Grep", {"pattern": "foo"}, "self"),
    ("Glob", {"pattern": "**/*.py"}, "self"),
    ("Read", {"file_path": "/x"}, None),
    ("Edit", {}, None),
    (None, None, None),
    ("Bash", None, None),
    ("Bash", {"command": 5}, None),
    ("Bash", {"command": ""}, None),
    ("Bash", {"command": "grep -rn foo src/"}, "self"),
    ("Bash", {"command": "egrep foo x"}, "self"),
    ("Bash", {"command": "rg foo"}, "self"),
    ("Bash", {"command": "/usr/bin/rg foo"}, "self"),
    ("Bash", {"command": "cd x && rg foo"}, "self"),
    ("Bash", {"command": "cd x; grep foo y"}, "self"),
    ("Bash", {"command": "false || grep foo y"}, "self"),
    ("Bash", {"command": "echo hi\nfind . -name '*.py'"}, "self"),
    ("Bash", {"command": "find . -name x"}, "self"),
    ("Bash", {"command": "fd foo"}, "self"),
    ("Bash", {"command": "fdfind foo"}, "self"),
    ("Bash", {"command": "ag foo"}, "self"),
    ("Bash", {"command": "ack foo"}, "self"),
    ("Bash", {"command": "FOO=1 grep foo x"}, "self"),
    ("Bash", {"command": "FOO=1 BAR=2 grep foo x"}, "self"),
    ("Bash", {"command": "sudo grep foo x"}, "self"),
    ("Bash", {"command": "env grep foo x"}, "self"),
    ("Bash", {"command": "timeout 10 grep foo x"}, "self"),
    ("Bash", {"command": "timeout -k 5 10s rg x"}, "self"),
    ("Bash", {"command": "timeout --signal TERM 10 grep x"}, "self"),
    ("Bash", {"command": "ls -R src"}, "self"),
    ("Bash", {"command": "ls -laR src"}, "self"),
    ("Bash", {"command": "ls --recursive src"}, "self"),
    ("Bash", {"command": "ls -la src"}, None),
    ("Bash", {"command": "ls src"}, None),
    ("Bash", {"command": "cat f | grep x"}, None),
    ("Bash", {"command": "git log | rg fix"}, None),
    ("Bash", {"command": "echo grep"}, None),
    ("Bash", {"command": "git grep foo"}, None),
    ("Bash", {"command": "myvec search 'where is x'"}, "myvec"),
    ("Bash", {"command": "uv run myvec search foo"}, "myvec"),
    ("Bash", {"command": "cd /p && myvec search foo --scope all"}, "myvec"),
    ("Bash", {"command": "myvec recall foo"}, "recall"),
    ("Bash", {"command": "myvec status"}, None),
    ("Bash", {"command": "myvec"}, None),
    ("Bash", {"command": "uv run pytest -q"}, None),
])
def test_classify(name, inp, cat):
    assert T.classify(name, inp) == cat


def test_classify_never_returns_the_command_text():
    assert T.classify("Bash", {"command": f"grep {SENTINEL} x"}) == "self"


# ------------------------------------------------------------------ parse_tool

def test_parse_tool_whitelist_only():
    out = T.parse_tool(bash("t1", f"rg {SENTINEL}"))
    assert out == [{"tool_id": "t1", "ts": out[0]["ts"], "cwd": "/p/acme", "cat": "self",
                    "kind": "exact", "doc": 0}]
    assert out[0]["ts"] > 0 and SENTINEL not in json.dumps(out)


def test_parse_tool_multiple_blocks_and_unclassified():
    d = {"type": "assistant", "cwd": "/c", "timestamp": TS, "message": {"content": [
        {"type": "text", "text": "x"},
        {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
        {"type": "tool_use", "id": "b", "name": "Grep", "input": {}}]}}
    out = T.parse_tool(json.dumps(d).encode())
    assert [(t["tool_id"], t["cat"]) for t in out] == [("a", None), ("b", "self")]


@pytest.mark.parametrize("line", [
    b'{"type": "user", "message": {"content": [{"type": "tool_use"}]}}\n',
    b'{"type": "assistant", "message": {"content": "tool_use"}}\n',
    b'{"type": "assistant", "message": {"content": [{"type": "text"}]}}\n',
    b'{"type": "assistant", "tool_use": broken\n',
    b'[1, "tool_use"]\n',
    b'',
])
def test_parse_tool_ignores_other_lines(line):
    assert T.parse_tool(line) == []


# ------------------------------------------------------------------ scan

def test_scan_writes_categories_without_command_text(root, tdb):
    put(root, "pr/s1.jsonl",
        bash("t1", f"grep {SENTINEL} src"), bash("t2", f"echo {SENTINEL}"),
        tool("t3", "Grep", {"pattern": SENTINEL}), bash("t4", "myvec search foo"))
    T.scan(tdb, root)
    # `seq` counts every tool_use block, unclassified included
    assert calls(tdb) == [("t1", "self", 1, "/p/acme"), ("t3", "self", 3, "/p/acme"), ("t4", "myvec", 4, "/p/acme")]
    cols = {r[1] for r in tdb.execute("PRAGMA table_info(tool_calls)")}
    assert cols == {"tool_id", "file_id", "ts", "seq", "cat", "cwd", "kind", "doc"}
    tdb.close()
    assert SENTINEL.encode() not in (config.tokens_db_path()).read_bytes()
    for suffix in ("-wal", "-shm"):
        p = config.tokens_db_path().with_name(config.tokens_db_path().name + suffix)
        assert not p.exists() or SENTINEL.encode() not in p.read_bytes()


def test_seq_continues_across_incremental_scans(root, tdb):
    put(root, "pr/s1.jsonl", bash("t1", "ls"), bash("t2", "rg a"))
    T.scan(tdb, root)
    put(root, "pr/s1.jsonl", bash("t3", "rg b"))
    T.scan(tdb, root)
    assert [(c[0], c[2]) for c in calls(tdb)] == [("t2", 2), ("t3", 3)]
    T.scan(tdb, root)
    assert len(calls(tdb)) == 2


def test_fork_and_resume_dedup_by_tool_id(root, tdb):
    put(root, "pr/s1.jsonl", bash("t1", "rg a"), bash("t2", "rg b"))
    put(root, "pr/s2.jsonl", bash("t1", "rg a"), bash("t2", "rg b"), bash("t3", "myvec search c"))
    T.scan(tdb, root)
    assert sorted(c[0] for c in calls(tdb)) == ["t1", "t2", "t3"]


def test_truncated_file_restarts_seq_without_duplicates(root, tdb):
    p = put(root, "pr/s1.jsonl", bash("t1", "rg a"), bash("t2", "rg b"))
    T.scan(tdb, root)
    p.write_bytes(bash("t1", "rg a"))
    T.scan(tdb, root)
    assert sorted(c[0] for c in calls(tdb)) == ["t1", "t2"]


def test_partial_last_line_waits(root, tdb):
    line = bash("t2", "rg b")
    p = put(root, "pr/s1.jsonl", bash("t1", "rg a"))
    with open(p, "ab") as f:
        f.write(line[:50])
    T.scan(tdb, root)
    assert [c[0] for c in calls(tdb)] == ["t1"]
    with open(p, "ab") as f:
        f.write(line[50:])
    T.scan(tdb, root)
    assert [(c[0], c[2]) for c in calls(tdb)] == [("t1", 1), ("t2", 2)]


def test_subagent_file_calls_joined_to_agent(root, tdb):
    put(root, "pr/s1/subagents/agent-x.jsonl", bash("t1", "rg a"))
    T.scan(tdb, root)
    r = tdb.execute("SELECT f.agent_id FROM tool_calls c JOIN token_files f USING(file_id)").fetchone()
    assert r[0] == "x"


# ------------------------------------------------------------------ migration v1 -> v2

def make_v1(path):
    """A tokens.db as the v1 build left it: no tool_calls table, no tool_seq column."""
    db = T.open_db()
    db.close()
    c = sqlite3.connect(path)
    c.execute("DROP TABLE tool_calls")
    c.execute("ALTER TABLE token_files DROP COLUMN tool_seq")
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.commit()
    c.close()


def test_migration_v1_to_v2_rereads_without_changing_usage(root):
    p = put(root, "pr/s1.jsonl", bash("t1", "rg a"), bash("t2", "myvec search b"), bash("t3", "ls"))
    put(root, "pr/s2.jsonl", bash("t1", "rg a"))                       # fork repeating t1
    db = T.open_db()
    T.scan(db, root)
    before = tuple(db.execute("SELECT count(*), sum(input), sum(output), sum(cache_read), sum(cache_write) "
                              "FROM token_usage").fetchone())
    db.close()
    path = config.tokens_db_path()
    make_v1(path)
    c = sqlite3.connect(path)
    c.execute('UPDATE token_files SET "offset"=size')
    c.commit()
    c.close()
    db = T.open_db()
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM token_files WHERE "offset"=0 AND tool_seq=0').fetchone()[0] == 2
    T.scan(db, root)
    after = tuple(db.execute("SELECT count(*), sum(input), sum(output), sum(cache_read), sum(cache_write) "
                             "FROM token_usage").fetchone())
    assert after == before
    assert sorted(c[0] for c in calls(db)) == ["t1", "t2"]
    T.scan(db, root)
    assert len(calls(db)) == 2 and tuple(db.execute("SELECT count(*) FROM token_usage").fetchone()) == (before[0],)
    db.close()
    assert p.exists()


def test_migration_skips_gone_files_and_is_one_off(root):
    put(root, "pr/s1.jsonl", bash("t1", "rg a"))
    p2 = put(root, "pr/s2.jsonl", bash("t2", "rg b"))
    db = T.open_db()
    T.scan(db, root)
    p2.unlink()
    T.scan(db, root)
    assert db.execute("SELECT gone FROM token_files WHERE path LIKE '%s2.jsonl'").fetchone()[0] == 1
    db.close()
    make_v1(config.tokens_db_path())
    db = T.open_db()
    live = db.execute('SELECT "offset" FROM token_files WHERE path LIKE \'%s1.jsonl\'').fetchone()[0]
    gone = db.execute('SELECT gone, "offset" FROM token_files WHERE path LIKE \'%s2.jsonl\'').fetchone()
    assert live == 0 and gone[0] == 1 and gone[1] > 0                    # gone file keeps its offset
    T.scan(db, root)
    off = db.execute('SELECT "offset" FROM token_files WHERE gone=0').fetchone()[0]
    db.close()
    db = T.open_db()                                                    # v2 again: no second reset
    assert db.execute('SELECT "offset" FROM token_files WHERE gone=0').fetchone()[0] == off > 0
    db.close()


def test_open_readonly_on_fresh_v2(root):
    T.open_db().close()
    db = T.open_db(readonly=True)
    assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == 0
    db.close()


# ------------------------------------------------------------------ relocate

def test_relocate_rewrites_tool_calls_cwd(root, tdb):
    put(root, "pr/s1.jsonl", bash("t1", "rg a", cwd="/Users/me/p/acme"), bash("t2", "rg b", cwd="/srv/other"))
    T.scan(tdb, root)
    idx = dbmod.open_ready(config.load_config())
    counts = relocate.relocate(idx, "/Users/me", "/home/me", dry_run=True)
    assert counts["tokens"] >= 2        # token_usage + tool_calls rows
    assert {r[0] for r in tdb.execute("SELECT cwd FROM tool_calls")} == {"/Users/me/p/acme", "/srv/other"}
    relocate.relocate(idx, "/Users/me", "/home/me")
    assert {r[0] for r in tdb.execute("SELECT cwd FROM tool_calls")} == {"/home/me/p/acme", "/srv/other"}
    idx.close()


# ------------------------------------------------------------------ E8-16: kind/doc, migration v2 -> v3

def make_v2(path):
    """A tokens.db as the v2 build left it: tool_calls without kind/doc, schema_version 2."""
    db = T.open_db()
    db.close()
    c = sqlite3.connect(path)
    c.execute("ALTER TABLE tool_calls DROP COLUMN kind")
    c.execute("ALTER TABLE tool_calls DROP COLUMN doc")
    c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    c.commit()
    c.close()


def kinds(db):
    return {r[0]: (r[1], r[2], r[3]) for r in db.execute("SELECT tool_id, cat, kind, doc FROM tool_calls")}


def test_scan_stores_kind_and_doc_labels_only(root, tdb):
    put(root, "pr/s1.jsonl",
        bash("t1", f"rg 'a|{SENTINEL}' src"), bash("t2", f"grep -rn {SENTINEL} docs/a.md"),
        tool("t3", "Read", {"file_path": f"/p/docs/{SENTINEL}.md"}), tool("t4", "Read", {"file_path": "/p/src/a.py"}),
        bash("t5", "myvec search foo"), tool("t6", "Glob", {"pattern": "**/*.md"}), bash("t7", "rg --files"))
    T.scan(tdb, root)
    assert kinds(tdb) == {"t1": ("self", "explore", 0), "t2": ("self", "file", 1), "t3": ("read", None, 1),
                          "t5": ("myvec", None, None), "t6": ("self", "name", 1), "t7": ("self", None, 0)}
    blob = json.dumps([list(r) for r in tdb.execute("SELECT * FROM tool_calls")])
    assert SENTINEL not in blob and "docs/a.md" not in blob and "src" not in blob
    tdb.close()
    assert SENTINEL.encode() not in config.tokens_db_path().read_bytes()


def test_migration_v2_to_v3_labels_history_and_keeps_rows(root):
    put(root, "pr/s1.jsonl", bash("t1", "rg 'a|b'", cwd="/Users/me/acme"), bash("t2", "myvec search b"),
        tool("t3", "Read", {"file_path": "/p/docs/x.md"}))
    p2 = put(root, "pr/s2.jsonl", bash("g1", "rg old", cwd="/Users/me/acme"))
    db = T.open_db()
    T.scan(db, root)
    p2.unlink()
    T.scan(db, root)
    sums = tuple(db.execute("SELECT count(*), sum(input), sum(output), sum(cache_read) FROM token_usage").fetchone())
    db.close()
    path = config.tokens_db_path()
    make_v2(path)
    c = sqlite3.connect(path)
    c.execute("DELETE FROM tool_calls WHERE cat='read'")          # v2 never stored Read
    c.execute("UPDATE tool_calls SET cwd='/home/me/acme'")        # relocate rewrote the cwd
    c.commit()
    c.close()
    db = T.open_db()
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    assert {r[1] for r in db.execute("PRAGMA table_info(tool_calls)")} >= {"kind", "doc"}
    assert db.execute('SELECT count(*) FROM token_files WHERE gone=0 AND "offset"=0').fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM tool_calls WHERE kind IS NOT NULL").fetchone()[0] == 0
    T.scan(db, root)
    k = kinds(db)
    assert k["t1"] == ("self", "explore", 0) and k["t3"] == ("read", None, 1)
    assert k["g1"] == ("self", None, None)                        # transcript gone: stays unclassified
    assert {r[0] for r in db.execute("SELECT cwd FROM tool_calls WHERE tool_id IN ('t1','g1')")} == {"/home/me/acme"}
    assert tuple(db.execute("SELECT count(*), sum(input), sum(output), sum(cache_read) FROM token_usage").fetchone()) == sums
    off = db.execute('SELECT "offset" FROM token_files WHERE gone=0').fetchone()[0]
    db.close()
    db = T.open_db()                                              # reopening does not reset the offset again
    assert db.execute('SELECT "offset" FROM token_files WHERE gone=0').fetchone()[0] == off > 0
    db.close()


def test_migration_v1_straight_to_v3(root):
    put(root, "pr/s1.jsonl", bash("t1", "rg a"))
    db = T.open_db()
    T.scan(db, root)
    db.close()
    make_v1(config.tokens_db_path())
    db = T.open_db()
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    T.scan(db, root)
    assert kinds(db) == {"t1": ("self", "exact", 0)}
    db.close()


def test_migration_v3_stamp_without_kind_columns_still_adds_them(root):
    """schema_version 3 but a tool_calls without kind/doc (e.g. a half-applied upgrade): the early exit must not skip."""
    db = T.open_db()
    db.close()
    path = config.tokens_db_path()
    c = sqlite3.connect(path)
    c.execute("ALTER TABLE tool_calls DROP COLUMN kind")
    c.execute("ALTER TABLE tool_calls DROP COLUMN doc")
    c.commit()
    c.close()
    db = T.open_db()
    assert {r[1] for r in db.execute("PRAGMA table_info(tool_calls)")} >= {"kind", "doc"}
    db.close()
    db = T.open_db()                                              # a second open is a no-op
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    db.close()


def test_rescan_does_not_overwrite_existing_labels_or_cwd(root, tdb):
    put(root, "pr/s1.jsonl", bash("t1", "rg 'a|b'", cwd="/Users/me/acme"))
    T.scan(tdb, root)
    tdb.execute("UPDATE tool_calls SET cwd='/home/me/acme'")
    tdb.execute('UPDATE token_files SET "offset"=0, tool_seq=0')
    tdb.commit()
    T.scan(tdb, root)
    assert [tuple(r) for r in tdb.execute("SELECT cwd, kind, doc FROM tool_calls")] == [("/home/me/acme", "explore", 0)]


def test_migration_v3_to_v4_labels_null_rows_and_keeps_counts(root):
    put(root, "pr/s1.jsonl", bash("t1", "rg 'a|b'", cwd="/p/acme"), bash("t2", "myvec search b"),
        tool("t3", "Read", {"file_path": "/p/docs/x.md"}))
    db = T.open_db()
    T.scan(db, root)
    n = db.execute("SELECT count(*) FROM tool_calls").fetchone()[0]
    sums = tuple(db.execute("SELECT count(*), sum(input), sum(output), sum(cache_read) FROM token_usage").fetchone())
    db.execute("UPDATE tool_calls SET kind=NULL, doc=NULL")          # what the v3 build left after its re-read
    db.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    db.commit()
    db.close()
    ro = T.open_db(readonly=True)                                    # a read-only v3 DB still reads
    try:
        assert ro.execute("SELECT count(*) FROM tool_calls WHERE kind IS NULL").fetchone()[0] == n
    finally:
        ro.close()
    db = T.open_db()
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    assert db.execute('SELECT count(*) FROM token_files WHERE gone=0 AND "offset"=0').fetchone()[0] == 1
    T.scan(db, root)
    k = kinds(db)
    assert k["t1"] == ("self", "explore", 0) and k["t3"] == ("read", None, 1)
    assert db.execute("SELECT count(*) FROM tool_calls").fetchone()[0] == n
    assert tuple(db.execute("SELECT count(*), sum(input), sum(output), sum(cache_read) FROM token_usage").fetchone()) == sums
    off = db.execute('SELECT "offset" FROM token_files WHERE gone=0').fetchone()[0]
    db.close()
    db = T.open_db()                                                 # one-off: reopening keeps the offset
    assert db.execute('SELECT "offset" FROM token_files WHERE gone=0').fetchone()[0] == off > 0
    db.close()
