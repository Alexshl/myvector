import json
import time

import pytest
from typer.testing import CliRunner

from conftest import do_sync, write
from myvec import config, daemon, db as dbmod, filters, projects, ticketdb
from myvec.commands import app

runner = CliRunner()

GROUP = """---
id: E9
title: Epic nine
kind: group
status: in_progress
---
# E9
"""

T1 = """---
id: E9-01
title: Zebra feature
status: in_progress
parent: E9
size: S
deps: []
blocks: [E9-02]
summary: Does the quokka thing
---
# Zebra feature

Detailed description of the quokka marshalling subsystem.

## Acceptance criteria

- [x] first
- [ ] second
"""

T2 = """---
id: E9-02
title: Second
status: todo
parent: E9
size: S
deps: [E9-01]
blocks: []
---
# Second

## Acceptance criteria

- [ ] a
"""


def add_tickets(root, d="docs/todo"):
    write(root / d / "E9-x/index.md", GROUP)
    write(root / d / "E9-x/E9-01-zebra.md", T1)
    write(root / d / "E9-x/E9-02-second.md", T2)


def open_db():
    return dbmod.open_ready()


def rows(db, sql, *a):
    return [dict(r) for r in db.execute(sql, a)]


def test_sync_fills_tickets_and_read_api(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM tickets WHERE project_path='acme'").fetchone()[0] == 3
    tree = ticketdb.ticket_tree(db, "acme")
    assert [t["id"] for t in tree] == ["E9", "E9-01", "E9-02"]   # parents before children
    t1 = next(t for t in tree if t["id"] == "E9-01")
    assert t1["parent"] == "E9" and t1["blocks"] == ["E9-02"] and t1["progress"] == 50.0
    assert next(t for t in tree if t["id"] == "E9-02")["deps"] == ["E9-01"]
    g = next(t for t in tree if t["id"] == "E9")
    assert g["progress"] == 25.0
    full = ticketdb.get_ticket(db, "acme", "E9-01")
    assert "quokka marshalling" in full["body"] and full["issues"] == []
    assert ticketdb.get_ticket(db, "acme", "nope") is None
    assert ticketdb.ticket_issues(db, "acme") == []
    s = ticketdb.projects_summary(db)
    assert [x["project_path"] for x in s] == ["acme"] and s[0]["n_tickets"] == 3 and s[0]["n_errors"] == 0
    assert s[0]["progress"] == 25.0


def test_project_without_canon_has_no_tickets(ws):
    # no dir at all
    _, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ticket_dirs").fetchone()[0] == 0
    assert ticketdb.projects_summary(db) == []
    # dir with notes without frontmatter
    write(ws / "docs/todo/notes.md", "# just a note\n\nno header\n")
    r = ticketdb.refresh_project(db, "acme", ws)
    assert r.changed and r.n_tickets == 0
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
    assert ticketdb.projects_summary(db) == []


def test_unchanged_fingerprint_writes_nothing(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    before = rows(db, "SELECT * FROM ticket_dirs")
    wc = db.total_changes
    r = ticketdb.refresh_project(db, "acme", ws)
    assert not r.changed and r.n_tickets == 3
    assert db.total_changes == wc and rows(db, "SELECT * FROM ticket_dirs") == before
    # a change is picked up
    p = ws / "docs/todo/E9-x/E9-02-second.md"
    time.sleep(0.01)
    p.write_text(T2.replace("- [ ] a", "- [x] a"))
    r = ticketdb.refresh_project(db, "acme", ws)
    assert r.changed
    assert db.execute("SELECT ac_done FROM tickets WHERE id='E9-02'").fetchone()[0] == 1
    # removed dir clears everything
    import shutil

    shutil.rmtree(ws / "docs/todo")
    r = ticketdb.refresh_project(db, "acme", ws)
    assert r.changed
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ticket_dirs").fetchone()[0] == 0
    assert not ticketdb.refresh_project(db, "acme", ws).changed


def test_custom_tickets_dir_from_marker(ws):
    add_tickets(ws, "plan")
    (ws / ".myvector.toml").write_text('name = "acme"\nshare_global = true\ninclude = []\nexclude = []\n'
                                       '\n[tickets]\ndir = "plan"\n')
    m = projects.read_marker(ws)
    assert m.tickets_dir == "plan" and m.bad_tickets_dir is None
    _, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3
    assert db.execute("SELECT dir FROM ticket_dirs").fetchone()[0] == "plan"
    assert ticketdb.get_ticket(db, "acme", "E9-01")["path"] == "plan/E9-x/E9-01-zebra.md"


@pytest.mark.parametrize("bad", ["../outside", "/abs/path", "a//b", "./x", ""])
def test_invalid_tickets_dir_falls_back_with_warning(ws, bad):
    add_tickets(ws)
    (ws / ".myvector.toml").write_text('name = "acme"\nshare_global = true\ninclude = []\nexclude = []\n'
                                       f'[tickets]\ndir = "{bad}"\n')
    m = projects.read_marker(ws)
    assert m.tickets_dir == "docs/todo" and m.bad_tickets_dir == bad
    st, db = do_sync(ws)
    assert not st.errors
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3
    codes = [i["code"] for i in ticketdb.ticket_issues(db, "acme")]
    assert "bad_tickets_dir" in codes
    assert db.execute("SELECT n_errors FROM ticket_dirs").fetchone()[0] == 0   # warning, not error
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0         # project still indexed


def test_write_marker_preserves_unknown_sections(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / ".myvector.toml").write_text('name = "p"\nfoo = "bar"\nlevel = 3\ninclude = []\nexclude = []\n'
                                         '[tickets]\ndir = "plan"\n[other]\nitems = ["a", "b"]\nflag = true\n')
    projects.write_marker(root, "p2", None, ["x"], ["y"], False)
    m = projects.read_marker(root)
    assert m.name == "p2" and m.include == ["x"] and m.exclude == ["y"] and m.share_global is False
    assert m.tickets_dir == "plan"
    import tomllib

    d = tomllib.loads((root / ".myvector.toml").read_text())
    assert d["foo"] == "bar" and d["level"] == 3 and d["other"] == {"items": ["a", "b"], "flag": True}
    projects.write_marker(root, "p2", None, [], [], True)   # idempotent
    assert tomllib.loads((root / ".myvector.toml").read_text())["tickets"] == {"dir": "plan"}


def test_init_keeps_tickets_section(ws, monkeypatch):
    monkeypatch.chdir(ws)
    (ws / ".myvector.toml").write_text('name = "acme"\nshare_global = true\ninclude = []\nexclude = []\n'
                                       '[tickets]\ndir = "plan"\n')
    r = runner.invoke(app, ["init", "--name", "acme", "--exclude", "zzz"])
    assert r.exit_code == 0, r.output
    assert projects.read_marker(ws).tickets_dir == "plan"


def test_secrets_masked_and_hard_secret_body_dropped(ws):
    fake_key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    write(ws / "docs/todo/E9-01-a.md", T1.replace("title: Zebra feature", 'title: Zebra password = "hunter2hunter2"')
          .replace("Does the quokka thing", 'token = "abcdef123456"'))
    write(ws / "docs/todo/E9-02-b.md", T2 + f"\n{fake_key}\n")
    write(ws / "docs/todo/secrets.env.md", T2.replace("E9-02", "E9-03"))   # not denied by name: sanity (kept)
    _, db = do_sync(ws)
    r1 = ticketdb.get_ticket(db, "acme", "E9-01")
    assert "hunter2hunter2" not in r1["title"] and "•••" in r1["title"]
    assert "abcdef123456" not in (r1["summary"] or "")
    r2 = ticketdb.get_ticket(db, "acme", "E9-02")
    assert r2["body"] is None and r2["body_dropped"] is True
    for col in ("title", "summary", "body"):
        for (v,) in db.execute(f"SELECT {col} FROM tickets"):
            assert "PRIVATE KEY" not in (v or "")


def test_denied_paths_skipped(ws):
    assert filters.is_denied_path("docs/todo/.hidden/E9-01-x.md")
    write(ws / "docs/todo/.hidden/E9-05-x.md", T2.replace("E9-02", "E9-05"))
    write(ws / "docs/todo/E9-01-a.md", T1)
    _, db = do_sync(ws)
    ids = [r[0] for r in db.execute("SELECT id FROM tickets")]
    assert ids == ["E9-01"]


def test_v4_database_gets_ticket_tables(ws):
    _, db = do_sync(ws)
    for t in ("tickets", "ticket_edges", "ticket_issues", "ticket_dirs"):
        db.execute(f"DROP TABLE {t}")
    db.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    db.close()
    db = open_db()
    assert dbmod.get_meta(db, "schema_version") == str(dbmod.SCHEMA_VERSION)
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
    add_tickets(ws)
    do_sync(ws)
    assert open_db().execute("SELECT count(*) FROM tickets").fetchone()[0] == 3


def test_search_marks_ticket_hits(ws, monkeypatch):
    add_tickets(ws)
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    r = runner.invoke(app, ["search", "quokka marshalling subsystem", "--json"])
    assert r.exit_code == 0, r.output
    hits = json.loads(r.stdout)
    tk = [h for h in hits if h.get("ticket")]
    assert tk and tk[0]["path"].endswith("E9-01-zebra.md")
    assert tk[0]["ticket"]["id"] == "E9-01" and tk[0]["ticket"]["project"] == "acme"
    assert tk[0]["ticket"]["status"] == "in_progress"
    assert all("ticket" not in h for h in hits if not h["path"].startswith("docs/todo"))
    r = runner.invoke(app, ["search", "quokka marshalling subsystem"])
    assert "· ticket E9-01 [in_progress]" in r.stdout


# ------------------------------------------------------------------ daemon fast path

@pytest.fixture
def dstate(ws):
    add_tickets(ws)
    do_sync(ws)
    st = daemon.DaemonState(7878, "t", tickets_debounce=0.2)
    st.refresh()
    yield st
    st.ticket_executor.shutdown(wait=True)
    st.executor.shutdown(wait=True)
    st.close()


def wait_for(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_daemon_event_refreshes_tickets_without_sync(dstate, ws, monkeypatch):
    from myvec import indexer

    monkeypatch.setattr(indexer, "sync_project", lambda *a, **k: pytest.fail("sync must not run"))
    st = dstate
    st._last_tickets_rescan = time.monotonic()   # isolate the fast path from the periodic rescan
    p = ws / "docs/todo/E9-x/E9-02-second.md"
    p.write_text(T2.replace("- [ ] a", "- [x] a"))
    st._on_fs_event("acme", str(p))
    assert "acme" in st._tickets_dirty
    st.tick()                                          # within the debounce: nothing yet
    db = open_db()
    assert db.execute("SELECT ac_done FROM tickets WHERE id='E9-02'").fetchone()[0] == 0
    time.sleep(0.25)
    st.tick()
    assert wait_for(lambda: open_db().execute("SELECT ac_done FROM tickets WHERE id='E9-02'").fetchone()[0] == 1)
    assert wait_for(lambda: any(e["type"] == "tickets_updated" for e in st.bus.recent(100)))
    # an event outside the tickets dir does not mark tickets dirty
    st._on_fs_event("acme", str(ws / "site/web/app.js"))
    assert "acme" not in st._tickets_dirty


def test_daemon_rescan_covers_inactive_projects(ws):
    do_sync(ws)
    add_tickets(ws)                      # written while nobody watches
    st = daemon.DaemonState(7878, "t")
    try:
        st.tick()                        # first tick: rescan of all registered projects
        assert wait_for(lambda: open_db().execute("SELECT count(*) FROM tickets").fetchone()[0] == 3)
    finally:
        st.ticket_executor.shutdown(wait=True)
        st.executor.shutdown(wait=True)
        st.close()


def test_ticket_tree_parents_precede_children_and_cycles_lose_nothing(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    tree = ticketdb.ticket_tree(db, "acme")
    pos = {t["id"]: i for i, t in enumerate(tree)}
    assert all(pos[t["parent"]] < pos[t["id"]] for t in tree if t["parent"])
    db.execute("UPDATE tickets SET parent='E9-02' WHERE id='E9-01'")
    db.execute("UPDATE tickets SET parent='E9-01' WHERE id='E9-02'")
    db.execute("UPDATE tickets SET parent='E9-01' WHERE id='E9'")
    assert sorted(t["id"] for t in ticketdb.ticket_tree(db, "acme")) == ["E9", "E9-01", "E9-02"]


def test_deleting_ticket_file_removes_rows_edges_and_issues(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM ticket_edges").fetchone()[0] > 0
    (ws / "docs/todo/E9-x/E9-02-second.md").unlink()
    r = ticketdb.refresh_project(db, "acme", ws)
    assert r.changed and r.n_tickets == 2
    assert db.execute("SELECT count(*) FROM tickets WHERE id='E9-02'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ticket_edges WHERE src='E9-02'").fetchone()[0] == 0
    # E9-01 still declares blocks: [E9-02] -> dangling reference reported as an issue
    assert any(i["ticket_id"] == "E9-01" for i in ticketdb.ticket_issues(db, "acme"))
    for f in (ws / "docs/todo/E9-x").glob("*.md"):
        f.unlink()
    r = ticketdb.refresh_project(db, "acme", ws)
    assert r.n_tickets == 0
    for tbl in ("tickets", "ticket_edges", "ticket_issues"):
        assert db.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0] == 0
    assert ticketdb.projects_summary(db) == []


def test_single_ticket_edit_refresh_does_not_embed(ws, monkeypatch):
    from myvec import embed

    add_tickets(ws)
    _, db = do_sync(ws)

    def boom(*a, **k):
        raise AssertionError("embedder must not be used")

    monkeypatch.setattr(embed, "get_embedder", boom, raising=False)
    p = ws / "docs/todo/E9-x/E9-01-zebra.md"
    time.sleep(0.01)
    p.write_text(T1.replace("- [ ] second", "- [x] second"))
    r = ticketdb.refresh_project(db, "acme", ws)
    assert r.changed
    assert db.execute("SELECT ac_done, progress FROM tickets WHERE id='E9-01'").fetchone()[:] == (2, 100.0)


def test_extra_numbers_survive_masking_and_bad_json_does_not_crash(ws):
    add_tickets(ws)
    p = ws / "docs/todo/E9-x/E9-02-second.md"
    p.write_text(p.read_text().replace("size: S\n", "size: S\ntoken_budget: 1200000\nnote: hello\n"))
    _, db = do_sync(ws)
    t = ticketdb.get_ticket(db, "acme", "E9-02")
    assert str(t["extra"].get("token_budget")) == "1200000" and t["extra"].get("note") == "hello"
    assert ticketdb.ticket_tree(db, "acme")
    db.execute("UPDATE tickets SET extra='{broken' WHERE id='E9-02'")
    assert ticketdb.get_ticket(db, "acme", "E9-02")["extra"] == {}
    assert ticketdb.ticket_tree(db, "acme")


def test_write_marker_refuses_unpreservable_content(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    orig = 'name = "p"\ninclude = []\nexclude = []\n[tickets]\ndir = "plan"\n[tickets.deep]\nx = 1\n'
    (root / ".myvector.toml").write_text(orig)
    with pytest.raises(projects.MarkerRewriteRefused):
        projects.write_marker(root, "p", None, [], [], True)
    assert (root / ".myvector.toml").read_text() == orig


def test_tickets_dir_symlink_outside_root_is_ignored(ws, tmp_path):
    outside = tmp_path / "outside"
    add_tickets(outside)
    (ws / "link").symlink_to(outside / "docs/todo")
    (ws / ".myvector.toml").write_text('name = "acme"\nshare_global = true\ninclude = []\nexclude = []\n'
                                       '[tickets]\ndir = "link"\n')
    _, db = do_sync(ws)
    assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0


def test_projects_summary_counts_include_empty_and_old_call(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    s = ticketdb.projects_summary(db)
    assert s[0]["counts"] == {"in_progress": 1, "todo": 1}          # the group is not counted
    assert s[0]["n_issues"] == 0 and s[0]["updated_max"] is None
    # a dir without canonical tickets: hidden by default, shown with include_empty
    db.execute("INSERT INTO ticket_dirs VALUES('empty','docs/todo','x',0,0,0)")
    assert [x["project_path"] for x in ticketdb.projects_summary(db)] == ["acme"]
    e = [x for x in ticketdb.projects_summary(db, include_empty=True) if x["project_path"] == "empty"]
    assert e and e[0]["n_tickets"] == 0 and e[0]["counts"] == {} and e[0]["progress"] is None


def test_project_tickets_and_card_sections(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    d = ticketdb.project_tickets(db, "acme")
    assert d["project"] == "acme" and d["progress"] == 25.0 and [n["id"] for n in d["nodes"]] == ["E9", "E9-01", "E9-02"]
    assert ticketdb.project_tickets(db, "nope") is None
    sec = ticketdb.get_ticket(db, "acme", "E9-01")["sections"]
    assert [a["text"] for a in sec["ac"]] == ["first", "second"]


def test_summary_n_problems_counts_tickets_once(ws):
    add_tickets(ws)
    _, db = do_sync(ws)
    pp = db.execute("SELECT project_path FROM ticket_dirs").fetchone()[0]
    db.execute("UPDATE tickets SET status='blocked' WHERE project_path=? AND kind!='group' AND id=(SELECT id FROM tickets WHERE project_path=? AND kind!='group' ORDER BY id LIMIT 1)", (pp, pp))
    tid = db.execute("SELECT id FROM tickets WHERE project_path=? AND status='blocked'", (pp,)).fetchone()[0]
    for code in ("a", "b"):          # two issues on the same blocked ticket + one without ticket
        db.execute("INSERT INTO ticket_issues VALUES(?,?,?,?,?,?)", (pp, "error", code, tid, "p", "m"))
    db.execute("INSERT INTO ticket_issues VALUES(?,?,?,?,?,?)", (pp, "error", "c", None, "p", "m"))
    s = ticketdb.projects_summary(db)[0]
    assert s["n_issues"] == 3 and s["n_problems"] == 1


def _worst(ws, monkeypatch, issues=(), process=(), health=()):
    from myvec import problems
    add_tickets(ws)
    _, db = do_sync(ws)
    pp = db.execute("SELECT project_path FROM ticket_dirs").fetchone()[0]
    db.execute("DELETE FROM ticket_issues WHERE project_path=?", (pp,))
    for i, sev in enumerate(issues):
        db.execute("INSERT INTO ticket_issues VALUES(?,?,?,?,?,?)", (pp, sev, f"c{i}", None, "p", "m"))
    monkeypatch.setattr(problems, "process_issues", lambda root: [dict(x) for x in process])
    monkeypatch.setattr(ticketdb, "_health", lambda *a, **k: [{"ticket_id": f"h{i}"} for i in range(len(health))])
    return ticketdb.projects_summary(db)[0]["worst"]


def test_process_issues_carry_severity(tmp_path):
    from myvec import problems
    agents = tmp_path / ".claude" / "agents"      # agents without search.md -> search_rule_missing
    agents.mkdir(parents=True)
    (agents / "coder.md").write_text("# coder\n")
    issues = problems.process_issues(str(tmp_path))
    assert issues
    for i in issues:
        assert i["severity"] in ("error", "warning", "health")


def test_worst_none_without_problems(ws, monkeypatch):
    assert _worst(ws, monkeypatch) is None


def test_worst_only_warning_issue(ws, monkeypatch):
    assert _worst(ws, monkeypatch, issues=["warning"]) == "warning"


def test_worst_error_beats_warning(ws, monkeypatch):
    assert _worst(ws, monkeypatch, issues=["warning", "error"], process=[{"severity": "warning"}]) == "error"


def test_worst_only_health_issue(ws, monkeypatch):
    assert _worst(ws, monkeypatch, issues=["health"]) == "health"


def test_worst_health_list_alone(ws, monkeypatch):
    assert _worst(ws, monkeypatch, health=[1]) == "health"


def test_worst_only_process_warning(ws, monkeypatch):
    assert _worst(ws, monkeypatch, process=[{"severity": "warning"}]) == "warning"


def test_worst_process_health_only(ws, monkeypatch):
    assert _worst(ws, monkeypatch, process=[{"severity": "health"}]) == "health"


def test_worst_warning_beats_health(ws, monkeypatch):
    assert _worst(ws, monkeypatch, issues=["health"], process=[{"severity": "warning"}]) == "warning"


def test_worst_unknown_severity_counts_as_warning(ws, monkeypatch):
    assert _worst(ws, monkeypatch, issues=["weird"], health=[1]) == "warning"
    assert _worst(ws, monkeypatch, process=[{}]) == "warning"


# ------------------------------------------------------------------ sessions on tickets (E2-02)

def _bind_sessions(ws):
    add_tickets(ws)
    do_sync(ws)
    from myvec import sessions as sess
    db = open_db()
    sess.ensure_table(db)
    now = time.time()
    for sid, ts, state in (("s-old", now - 100, "working"), ("s-new", now - 10, "waiting")):
        db.execute("INSERT INTO sessions(session_id, state, last_seen, started_at, cwd, branch, project_path) "
                   "VALUES(?,?,?,?,?,?,?)", (sid, state, ts, ts, str(ws), "t/E9-01-zebra", "acme"))
    db.commit()
    return db


def test_get_ticket_and_tree_carry_sessions(ws):
    db = _bind_sessions(ws)
    t = ticketdb.get_ticket(db, "acme", "E9-01")
    assert [s["session_id"] for s in t["sessions"]] == ["s-new", "s-old"]
    assert [s["effective_state"] for s in t["sessions"]] == ["waiting", "working"]
    assert ticketdb.get_ticket(db, "acme", "E9-02")["sessions"] == []
    nodes = {n["id"]: n for n in ticketdb.project_tickets(db, "acme")["nodes"]}
    assert [s["session_id"] for s in nodes["E9-01"]["sessions"]] == ["s-new", "s-old"]
    assert set(nodes["E9-01"]["sessions"][0]) == {"session_id", "effective_state", "ticket_source", "last_seen", "wait_s"}
    assert nodes["E9-02"]["sessions"] == []
    # idle_min is honoured: 100 s silent working session turns idle with a tiny threshold
    t = ticketdb.get_ticket(db, "acme", "E9-01", idle_min=0.5)
    assert {s["session_id"]: s["effective_state"] for s in t["sessions"]}["s-old"] == "idle"


def test_api_ticket_has_sessions(ws, monkeypatch):
    from starlette.testclient import TestClient
    _bind_sessions(ws).close()
    st = daemon.DaemonState(7878, "t")
    try:
        c = TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878")
        d = c.get("/api/tickets/acme/ticket/E9-01").json()
        assert [s["session_id"] for s in d["sessions"]] == ["s-new", "s-old"]
        assert c.get("/api/tickets/acme/ticket/E9-02").json()["sessions"] == []
        tree = c.get("/api/tickets/acme/tree").json()
        assert all("sessions" in n for n in tree["nodes"])
        real = config.load_config()
        cfg = type("Cfg", (), {"__getattr__": lambda self, k: getattr(real, k) if k != "session_idle_minutes"
                               else (_ for _ in ()).throw(ValueError("bad"))})()
        monkeypatch.setattr(config, "load_config", lambda: cfg)   # broken session_idle_minutes -> default 15
        assert c.get("/api/tickets/acme/ticket/E9-01").status_code == 200
    finally:
        st.ticket_executor.shutdown(wait=True)
        st.executor.shutdown(wait=True)
        st.close()


# ------------------------------------------------------------------ health at read time (ADR-020)

def _health_tree(ws):
    write(ws / "docs/todo/E9-x/index.md", GROUP)
    write(ws / "docs/todo/E9-x/E9-01-zebra.md", T1.replace("size: S", "size: L").replace(
        "status: in_progress", "status: in_progress\nupdated: 2026-11-01"))
    write(ws / "docs/todo/E9-x/E9-02-second.md", T2)
    _, db = do_sync(ws)
    return db


def test_health_in_read_api_not_in_table(ws):
    import datetime
    db = _health_tree(ws)
    today = datetime.date(2026, 11, 10)
    assert ticketdb.ticket_issues(db, "acme") == []          # the table never holds advice
    d = ticketdb.project_tickets(db, "acme", today=today, now=time.time())
    got = {(i["ticket_id"], i["code"]) for i in d["issues"]}
    assert got == {("E9-01", "too_big"), ("E9-01", "stale"), ("E9-01", "no_session")}
    assert all(i["severity"] == "health" and set(i) == {"severity", "code", "ticket_id", "path", "message"} for i in d["issues"])
    t = ticketdb.get_ticket(db, "acme", "E9-01", today=today)
    assert {i["code"] for i in t["issues"]} == {"too_big", "stale", "no_session"}
    assert ticketdb.get_ticket(db, "acme", "E9-02", today=today)["issues"] == []
    # stale appears with a later date, without a refresh
    early = ticketdb.get_ticket(db, "acme", "E9-01", today=datetime.date(2026, 11, 5))
    assert {i["code"] for i in early["issues"]} == {"too_big", "no_session"}
    assert ticketdb.ticket_issues(db, "acme") == []


def test_health_counts_in_n_problems_without_double_count(ws):
    import datetime
    db = _health_tree(ws)
    today = datetime.date(2026, 11, 10)
    s = ticketdb.projects_summary(db, today=today, now=time.time())[0]
    assert s["n_health"] == 3 and s["n_problems"] == 1 and s["n_issues"] == 0
    # a ticket that already has a stored issue is counted once
    db.execute("INSERT INTO ticket_issues VALUES(?,?,?,?,?,?)", ("acme", "error", "c", "E9-01", "p", "m"))
    s = ticketdb.projects_summary(db, today=today, now=time.time())[0]
    assert s["n_problems"] == 1
    # active session removes no_session
    from myvec import sessions as sess
    sess.ensure_table(db)
    now = time.time()
    db.execute("INSERT INTO sessions(session_id, state, last_seen, started_at, cwd, branch, project_path) "
               "VALUES(?,?,?,?,?,?,?)", ("s1", "working", now, now, str(ws), "t/E9-01-zebra", "acme"))
    db.commit()
    s = ticketdb.projects_summary(db, today=today, now=now)[0]
    assert s["n_health"] == 2
    t = ticketdb.get_ticket(db, "acme", "E9-01", today=today, now=now)
    assert "no_session" not in {i["code"] for i in t["issues"]}
