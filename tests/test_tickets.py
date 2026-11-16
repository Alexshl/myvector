"""Канон тікета v1: парсер, статуси, перевірки, відсоток. Фікстури лише в tmp_path."""
import os
from pathlib import Path

import pytest

from myvec import tickets as T


def fm(**kw):
    lines = ["---"] + [f"{k}: {v}" for k, v in kw.items()] + ["---", ""]
    return "\n".join(lines)


def write(root: Path, rel: str, text: str):
    p = root / "docs" / "todo" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf8")


def codes(issues):
    return {i.code for i in issues}


# ---- YAML ----

def test_yaml_subset():
    text = (
        '---\nid: A-1\ntitle: "Заголовок: з двокрапкою # не коментар"  # коментар\n'
        "deps: [A-2, 'A-3']\nblocks:\n  - A-4\n  - A-5\npr:\nsummary: >\n  рядок один\n  рядок два\n"
        "note: |\n  a\n  b\nupdated: 2026-11-09\nzzz: 5\n---\nТіло\n"
    )
    d, body = T.parse_frontmatter(text)
    assert d["title"] == "Заголовок: з двокрапкою # не коментар"
    assert d["deps"] == ["A-2", "A-3"] and d["blocks"] == ["A-4", "A-5"]
    assert d["pr"] is None
    assert d["summary"] == "рядок один рядок два"
    assert d["note"] == "a\nb"
    assert d["zzz"] == "5" and body == "Тіло\n"


def test_no_frontmatter_and_unsupported():
    assert T.parse_frontmatter("# просто нотатка\n") is None
    for bad in ("---\na:\n  b: 1\n---\n", "---\nпросто текст\n---\n", "---\na: [1, 2\n---\n", '---\na: "x\n---\n'):
        with pytest.raises(T.FrontmatterError):
            T.parse_frontmatter(bad)


# ---- статуси ----

@pytest.mark.parametrize("raw,key", [
    ("todo", "todo"), ("⬜", "todo"), ("in_progress", "in_progress"), ("🟦", "in_progress"),
    ("review", "review"), ("🟨", "review"), ("waiting", "waiting"), ("⏳", "waiting"),
    ("blocked", "blocked"), ("⛔", "blocked"), ("partial", "partial"), ("🟧", "partial"),
    ("done", "done"), ("✅", "done"), ("cancelled", "cancelled"), ("🚫", "cancelled"),
    ("frozen", "frozen"), ("❄", "frozen"), ("❄️", "frozen"), ("DONE", "done"),
])
def test_status_synonyms(raw, key):
    assert T.normalize_status(raw) == (key, None)


def test_status_done_date_and_bad():
    assert T.normalize_status("✅ 2026-11-09") == ("done", "2026-11-09")
    assert T.normalize_status("done 2026-01-02") == ("done", "2026-01-02")
    assert T.normalize_status("wip") is None and T.normalize_status(None) is None
    assert T.normalize_status("🟩") is None


# ---- чекбокси ----

def test_acceptance_checkboxes():
    body = "- [x] поза\n## Acceptance criteria\n\n- [x] a\n- [ ] b\n  - [X] c\n```\n- [ ] код\n```\n## Log\n- [ ] ні\n"
    assert T.acceptance_checkboxes(body) == (2, 3)
    assert T.acceptance_checkboxes("нема") == (0, 0)


# ---- завантаження ----

def build(tmp_path, items):
    for rel, text in items.items():
        write(tmp_path, rel, text)
    return T.load_dir(tmp_path)


def test_load_basic_and_skips(tmp_path):
    ts = build(tmp_path, {
        "E1-x/index.md": fm(id="E1", title="Епік", kind="group", status="todo"),
        "E1-x/E1-01-a.md": fm(id="E1-01", title="A", status="✅ 2026-11-09", parent="E1", size="m",
                              deps="[]", blocks="[]") + "## Acceptance criteria\n- [x] a\n",
        "INDEX.md": "# індекс\n", "ticket-template.md": fm(id="X"), "inbox/n.md": fm(id="E1-99"),
        "notes.md": "без шапки\n",
    })
    assert set(ts.tickets) == {"E1", "E1-01"}
    t = ts.tickets["E1-01"]
    assert (t.status, t.done_date, t.size, t.status_raw) == ("done", "2026-11-09", "M", "✅ 2026-11-09")
    assert (t.ac_done, t.ac_total) == (1, 1)
    assert T.check(ts) == []


def test_legacy_epic_and_synthetic_group(tmp_path):
    ts = build(tmp_path, {
        "E2-y/index.md": "---\ntitle: Другий\n---\nтекст\n",
        "E2-y/E2-01-a.md": fm(id="E2-01", title="A", status="⬜", epic="E2", size="S"),
    })
    g = ts.tickets["E2"]
    assert (g.kind, g.title, g.legacy, g.status) == ("group", "Другий", True, "todo")
    t = ts.tickets["E2-01"]
    assert t.parent == "E2" and t.legacy
    issues = T.check(ts)
    assert codes(issues) == {"legacy_epic"} and not ts.errors


def test_parse_errors(tmp_path):
    ts = build(tmp_path, {
        "a/E9-01-a.md": fm(id="E9-01", title="A", status="wip", size="XL", kind="epic"),
        "a/E9-02-b.md": fm(id="E9-02", status="todo"),
        "a/E9-03-c.md": "---\nx:\n  y: 1\n---\n",
        "a/E9-04-d.md": fm(title="без id", status="todo"),
        "a/E9-05-e.md": fm(id="E9-01", title="дубль", status="todo"),
        "a/other.md": fm(id="E9-06", title="імʼя", status="todo"),
    })
    c = codes(ts.issues)
    assert {"bad_status", "bad_size", "bad_kind", "missing_field", "bad_frontmatter", "dup_id",
            "id_filename_mismatch"} <= c


# ---- перевірки зв'язків ----

def mk(tid, **kw):
    return T.Ticket(id=tid, title=tid, **kw)


def tset(*ts):
    return T.TicketSet(tickets={t.id: t for t in ts})


def test_check_links():
    a = mk("A", deps=["B", "Z"], parent="G", kind="ticket")
    b = mk("B", blocks=["A", "Y"])
    c = mk("C", blocks=["B"])
    g = mk("G", kind="ticket", parent="NOPE")
    got = codes(T.check(tset(a, b, c, g)))
    assert {"dangling_dep", "dangling_block", "asym_block", "parent_not_group", "dangling_parent"} <= got
    d = mk("D", deps=["B"])
    assert "asym_dep" in codes(T.check(tset(b, d)))


def test_check_cycles():
    a, b = mk("A", deps=["B"], blocks=["B"]), mk("B", deps=["A"], blocks=["A"])
    assert "dep_cycle" in codes(T.check(tset(a, b)))
    s = mk("S", deps=["S"], blocks=["S"])
    assert "dep_cycle" in codes(T.check(tset(s)))
    g1, g2 = mk("G1", kind="group", parent="G2"), mk("G2", kind="group", parent="G1")
    assert "parent_cycle" in codes(T.check(tset(g1, g2)))


def test_check_clean_chain():
    a = mk("A", blocks=["B"])
    b = mk("B", deps=["A"], parent="G")
    g = mk("G", kind="group")
    assert T.check(tset(a, b, g)) == []


# ---- відсоток ----

def leaf(tid, status, done=0, total=0, parent=None, kind="ticket"):
    return mk(tid, status=status, ac_done=done, ac_total=total, parent=parent, kind=kind)


def test_progress_leaf_rules():
    ts = tset(
        leaf("d", "done"), leaf("t", "todo", 1, 2), leaf("c", "cancelled"),
        leaf("i", "in_progress", 1, 4), leaf("r", "review", 3, 4), leaf("w", "waiting", 1, 2),
        leaf("b", "blocked", 1, 2), leaf("p", "partial", 1, 2), leaf("f", "frozen", 1, 4),
        leaf("n", "in_progress"),
    )
    p = T.progress_all(ts)
    assert p == {"d": 100, "t": 0, "c": None, "i": 25, "r": 75, "w": 50, "b": 50, "p": 50, "f": 25, "n": 0}
    assert T.progress(ts, "d") == 100


def test_progress_groups():
    ts = tset(
        leaf("G", "todo", kind="group"),
        leaf("A", "done", parent="G"), leaf("B", "in_progress", 1, 2, parent="G"),
        leaf("X", "cancelled", parent="G"),
        leaf("H", "done", kind="group"), leaf("Hc", "cancelled", parent="H"),
        leaf("O", "todo", kind="group"), leaf("O1", "todo", kind="group", parent="O"),
        leaf("O2", "done", parent="O1"), leaf("O3", "todo", parent="O1"),
    )
    p = T.progress_all(ts)
    assert p["G"] == 75.0
    assert p["H"] == 100.0  # усі діти скасовані: за власним статусом
    assert p["O1"] == 50.0 and p["O"] == 50.0


def test_progress_survives_parent_cycle():
    ts = tset(leaf("A", "done", kind="group", parent="B"), leaf("B", "todo", kind="group", parent="A"))
    assert set(T.progress_all(ts)) == {"A", "B"}


# ---- додаткове покриття (tester) ----

@pytest.mark.parametrize("raw", ["{a: 1}", "&anc x", "*ref", "!tag x", "| x"])
def test_yaml_unsupported_values_raise(raw):
    with pytest.raises(T.FrontmatterError):
        T.parse_frontmatter(f"---\nk: {raw}\n---\n")


def test_yaml_duplicate_key_and_nested_list_raise():
    with pytest.raises(T.FrontmatterError):
        T.parse_frontmatter("---\na: 1\na: 2\n---\n")
    with pytest.raises(T.FrontmatterError):
        T.parse_frontmatter("---\na:\n  - x\n  b: 1\n---\n")


def test_yaml_quotes_empty_and_lists():
    d, _ = T.parse_frontmatter(
        "---\na: 'x: y'\nb: \"it's # ok\"\nc: []\nd:\ne: [ ]\nf: ['a, b', c]\ng:\n  - 'q'\n  - r # note\n"
        "h: 'it''s'\n---\n"
    )
    assert d["a"] == "x: y" and d["b"] == "it's # ok"
    assert d["c"] == [] and d["d"] is None and d["e"] == []
    assert d["f"] == ["a, b", "c"] and d["g"] == ["q", "r"] and d["h"] == "it's"


def test_yaml_crlf_and_comments():
    d, body = T.parse_frontmatter("---\r\n# коментар\r\nid: A\r\n\r\ntitle: T\r\n---\r\nтіло")
    assert d == {"id": "A", "title": "T"} and body == "тіло"


def test_bad_frontmatter_is_issue_not_crash(tmp_path):
    ts = build(tmp_path, {"a/E9-01-a.md": "---\nid: E9-01\nx: {y: 1}\n---\n", "a/E9-02-b.md": fm(id="E9-02", title="ok", status="todo")})
    assert "bad_frontmatter" in codes(ts.issues) and set(ts.tickets) == {"E9-02"}


def test_status_with_fe0f_and_done_emoji_date():
    assert T.normalize_status("❄️") == ("frozen", None)
    assert T.normalize_status("✅️ 2026-11-09") == ("done", "2026-11-09")
    assert T.normalize_status("in-progress") == ("in_progress", None)
    assert T.normalize_status("✅ 09.11.2026") is None


def test_all_nine_status_keys_covered():
    assert set(T.STATUS_KEYS) == set(T.STATUS_EMOJI.values()) | set()
    assert len(T.STATUS_KEYS) == 9


def test_missing_status_and_title_reported(tmp_path):
    ts = build(tmp_path, {"a/E9-01-a.md": fm(id="E9-01")})
    msgs = [i.message for i in ts.issues if i.code == "missing_field"]
    assert len(msgs) == 2
    assert ts.tickets["E9-01"].status == "todo"


def test_bad_status_keeps_default_todo(tmp_path):
    ts = build(tmp_path, {"a/E9-01-a.md": fm(id="E9-01", title="a", status="wip")})
    assert ts.tickets["E9-01"].status == "todo" and ts.tickets["E9-01"].status_raw == "wip"


def test_epic_with_parent_not_legacy(tmp_path):
    ts = build(tmp_path, {
        "E3-z/index.md": fm(id="E3", title="E", kind="group", status="todo"),
        "E3-z/E3-01-a.md": fm(id="E3-01", title="A", status="todo", parent="E3", epic="E9"),
    })
    t = ts.tickets["E3-01"]
    assert t.parent == "E3" and not t.legacy and "legacy_epic" not in codes(ts.issues)
    assert t.extra == {"epic": "E9"}


def test_unknown_keys_go_to_extra_and_dir_without_epic_name(tmp_path):
    ts = build(tmp_path, {
        "misc/index.md": "---\ntitle: X\n---\n",
        "misc/E4-01-a.md": fm(id="E4-01", title="A", status="todo", foo="bar"),
    })
    assert ts.tickets["E4-01"].extra == {"foo": "bar"}
    assert "missing_field" in codes(ts.issues) and set(ts.tickets) == {"E4-01"}


def test_skipped_nested_inbox_and_index_files(tmp_path):
    ts = build(tmp_path, {
        "E1-x/inbox/n.md": fm(id="E1-77", title="n", status="todo"),
        "E1-x/INDEX.md": fm(id="E1-78", title="n", status="todo"),
        "E1-x/ticket-template.md": fm(id="E1-79", title="n", status="todo"),
    })
    assert ts.tickets == {} and ts.issues == []


def test_load_dir_missing_dir(tmp_path):
    ts = T.load_dir(tmp_path / "nope")
    assert ts.tickets == {} and ts.issues == []


def test_dup_id_keeps_first_and_reports_both_paths(tmp_path):
    ts = build(tmp_path, {
        "a/E9-01-a.md": fm(id="E9-01", title="first", status="todo"),
        "b/E9-01-b.md": fm(id="E9-01", title="second", status="todo"),
    })
    assert ts.tickets["E9-01"].title == "first"
    dup = [i for i in ts.issues if i.code == "dup_id"]
    assert len(dup) == 1 and "E9-01-a.md" in dup[0].message


def test_size_case_insensitive_and_kind_default(tmp_path):
    ts = build(tmp_path, {"a/E9-01-a.md": fm(id="E9-01", title="a", status="todo", size="l")})
    assert ts.tickets["E9-01"].size == "L" and ts.tickets["E9-01"].kind == "ticket"


def test_deps_scalar_and_empty_values(tmp_path):
    ts = build(tmp_path, {"a/E9-01-a.md": fm(id="E9-01", title="a", status="todo", deps="E9-02", blocks="")})
    t = ts.tickets["E9-01"]
    assert t.deps == ["E9-02"] and t.blocks == []


def test_check_asym_only_one_side():
    a, b = mk("A", deps=["B"]), mk("B")
    got = [i for i in T.check(tset(a, b)) if i.code == "asym_dep"]
    assert len(got) == 1 and got[0].ticket_id == "A"
    c, d = mk("C", blocks=["D"]), mk("D")
    assert codes(T.check(tset(c, d))) == {"asym_block"}


def test_check_three_node_dep_cycle_and_self_parent():
    a, b, c = (mk("A", deps=["B"], blocks=["C"]), mk("B", deps=["C"], blocks=["A"]),
               mk("C", deps=["A"], blocks=["B"]))
    assert "dep_cycle" in codes(T.check(tset(a, b, c)))
    s = mk("S", kind="group", parent="S")
    assert "parent_cycle" in codes(T.check(tset(s)))


def test_check_includes_parse_issues_and_no_mutation():
    ts = tset(mk("A"))
    ts.issues.append(T.Issue("warning", "x", "A", "p", "m"))
    out = T.check(ts)
    assert out[0].code == "x" and len(ts.issues) == 1


def test_progress_depth3_mixed():
    ts = tset(
        leaf("R", "todo", kind="group"),
        leaf("G1", "todo", kind="group", parent="R"),
        leaf("G2", "todo", kind="group", parent="G1"),
        leaf("L1", "done", parent="G2"),
        leaf("L2", "review", 1, 4, parent="G2"),   # 25
        leaf("L3", "blocked", 1, 2, parent="G1"),  # 50
        leaf("L4", "cancelled", parent="R"),
        leaf("L5", "frozen", 0, 3, parent="R"),    # 0
    )
    p = T.progress_all(ts)
    assert p["G2"] == 62.5
    assert p["G1"] == (62.5 + 50) / 2
    assert p["R"] == (p["G1"] + 0) / 2
    assert p["L4"] is None


def test_progress_cancelled_group_and_dangling_parent():
    ts = tset(leaf("G", "cancelled", kind="group"), leaf("A", "done", parent="G"),
              leaf("B", "partial", 2, 4, parent="MISSING"))
    p = T.progress_all(ts)
    assert p["G"] is None and p["A"] == 100 and p["B"] == 50


def test_progress_group_without_children_uses_own_status():
    ts = tset(leaf("G", "done", kind="group"), leaf("H", "in_progress", 1, 4, kind="group"))
    assert T.progress_all(ts) == {"G": 100, "H": 25}


def test_acceptance_checkboxes_edge_cases():
    assert T.acceptance_checkboxes("## Acceptance criteria\n* [x] a\n- [] b\n-[ ] c\n") == (1, 1)
    assert T.acceptance_checkboxes("## acceptance criteria\n- [ ] a\n## Acceptance criteria\n- [x] b\n") == (0, 1)
    assert T.acceptance_checkboxes("## Acceptance criteria\n### sub\n- [x] a\n") == (1, 1)


# ---- ревю E1-01 ----

def test_load_dir_symlinks_and_unreadable(tmp_path):
    base = tmp_path / "docs" / "todo"
    base.mkdir(parents=True)
    ext = tmp_path / "outside.md"
    ext.write_text("---\nid: X-1\ntitle: t\nstatus: todo\n---\nSECRET\n", encoding="utf8")
    (base / "broken.md").symlink_to(tmp_path / "nope.md")
    (base / "ext.md").symlink_to(ext)
    ts = T.load_dir(tmp_path)
    assert "X-1" not in ts.tickets
    assert [i.code for i in ts.issues].count("symlink_skipped") == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root читає все")
def test_load_dir_unreadable_file(tmp_path):
    write(tmp_path, "a.md", fm(id="A-1", title="a", status="todo"))
    write(tmp_path, "b.md", fm(id="A-2", title="b", status="todo"))
    bad = tmp_path / "docs" / "todo" / "a.md"
    bad.chmod(0)
    try:
        ts = T.load_dir(tmp_path)
    finally:
        bad.chmod(0o644)
    assert "read_error" in codes(ts.issues) and "A-2" in ts.tickets


def test_list_for_scalar_field_is_bad_frontmatter():
    t, issues = T.parse_ticket("---\nid: A-1\ntitle: [x, y]\nstatus: todo\nsize: [S]\n---\n", "a.md")
    assert "bad_frontmatter" in codes(issues)
    assert t.title == "" and t.size is None


def test_nested_inline_list_rejected():
    with pytest.raises(T.FrontmatterError):
        T.parse_frontmatter("---\ndeps: [a, [b]]\n---\n")


def test_done_date_validated():
    assert T.normalize_status("✅ 2026-11-09") == ("done", "2026-11-09")
    assert T.normalize_status("✅ 2026-13-99") is None
    assert T.normalize_status("done 2026-02-30") is None


def test_epic_index_without_frontmatter(tmp_path):
    write(tmp_path, "E7-foo/index.md", "# Епік без шапки\n")
    write(tmp_path, "E7-foo/E7-01-x.md", fm(id="E7-01", title="x", status="todo", parent="E7"))
    ts = T.load_dir(tmp_path)
    assert ts.tickets["E7"].kind == "group"
    assert "dangling_parent" not in codes(T.check(ts))


def test_progress_unknown_id():
    assert T.progress(T.TicketSet(), "nope") is None


# ---- E1-06: body_sections ----

def test_body_sections_matches_acceptance_checkboxes():
    bodies = [
        "## Acceptance criteria\n- [x] a\n- [ ] b\n",
        "## Acceptance criteria\n```\n- [x] fenced\n```\n- [x] real\n## Log\n- [ ] not ac\n",
        "## acceptance criteria\n- [ ] a\n## Acceptance criteria\n- [x] b\n",
        "нема",
    ]
    for b, want in zip(bodies, [(1, 2), (1, 1), (0, 1), (0, 0)]):
        ac = T.body_sections(b)["ac"]
        assert (sum(x["done"] for x in ac), len(ac)) == want
        assert T.acceptance_checkboxes(b) == want
    assert T.body_sections(bodies[1])["ac"] == [{"text": "real", "done": True}]


def test_body_sections_log_goal_and_template_comment():
    body = ("## За один захід\n\nЗробити X.\n\n## Log\n\n<!-- агенти дописують: ### YYYY-MM-DD агент -->\n\n"
            "### 2026-11-08 planner\n\nПлан.\n\n### 2026-11-09 coder\n\nГотово <!-- hidden --> тут.\n")
    s = T.body_sections(body)
    assert s["goal"] == "Зробити X."
    assert s["log"] == [{"date": "2026-11-08", "agent": "planner", "text": "План."},
                        {"date": "2026-11-09", "agent": "coder", "text": "Готово  тут."}]
    assert T.body_sections("# t")["goal"] is None and T.body_sections("# t")["log"] == []


# ------------------------------------------------------------------ health (ADR-020)

import datetime as _dt

TODAY = _dt.date(2026, 11, 10)


def hn(tid, status="todo", **kw):
    d = dict(id=tid, kind="ticket", status=status, size="S", ac_total=1, updated="2026-11-10", path=f"{tid}.md", deps=[])
    d.update(kw)
    return d


def hc(nodes, **kw):
    return [(i.code, i.ticket_id) for i in T.health(nodes, TODAY, **kw)]


def test_health_too_big_and_no_ac():
    assert hc([hn("A", size="L"), hn("B", size="M"), hn("C", ac_total=0)]) == [("too_big", "A"), ("no_ac", "C")]
    assert all(i.severity == "health" for i in T.health([hn("A", size="L")], TODAY))


@pytest.mark.parametrize("status,limit", [("todo", 30), ("in_progress", 7), ("review", 7)])
def test_health_stale_boundary(status, limit):
    def at(days):
        return hn("A", status, updated=(TODAY - _dt.timedelta(days=days)).isoformat())
    assert hc([at(limit - 1)]) == []
    assert hc([at(limit)]) == [("stale", "A")]
    assert T.health([at(limit)], TODAY)[0].message == f"Не оновлювався {T._days(limit)}"
    assert hc([hn("A", status, updated="garbage")]) == [] and hc([hn("A", status, updated=None)]) == []


def test_health_skips_groups_and_closed():
    old = "2020-01-01"
    nodes = [hn("G", kind="group", size="L", ac_total=0, updated=old)] + [
        hn(s, status=st, size="L", ac_total=0, updated=old) for s, st in (("D", "done"), ("C", "cancelled"), ("F", "frozen"))]
    assert hc(nodes, sessions={}) == []


def test_health_accepts_ticket_objects():
    t = T.Ticket(id="X", status="todo", size="L", ac_total=0, updated="2026-11-10", path="x.md")
    assert [i.code for i in T.health([t], TODAY)] == ["too_big", "no_ac"]


def test_health_no_session():
    ago = lambda d: (_dt.datetime.combine(TODAY, _dt.time(12)) - _dt.timedelta(days=d)).timestamp()
    n = hn("A", "in_progress", updated="2026-11-09")
    assert hc([n]) == []                                    # without sessions the rule is off
    assert hc([n], sessions={}) == [("no_session", "A")]
    assert T.health([n], TODAY, sessions={})[0].message == "В роботі без сесії 1 день"
    assert hc([hn("A", "in_progress")], sessions={}) == []   # updated today: inside the grace day
    assert hc([n], sessions={"A": [{"effective_state": "working", "last_seen": ago(0)}]}) == []
    assert hc([n], sessions={"A": [{"effective_state": "waiting", "last_seen": ago(5)}]}) == []
    ended = {"A": [{"effective_state": "ended", "last_seen": ago(3)}]}
    old = hn("A", "in_progress", updated="2026-11-01")
    assert [i.message for i in T.health([old], TODAY, sessions=ended)][1] == "В роботі без сесії 3 дні"   # last_seen newer than updated
    assert hc([hn("A", "review", updated="2026-11-01")], sessions={}) == [("stale", "A")]   # only in_progress
    both = hc([hn("A", "in_progress", updated="2026-10-01")], sessions={})
    assert both == [("stale", "A"), ("no_session", "A")]


def test_health_blocked_ready_and_dep_cancelled():
    nodes = [hn("D1", "done"), hn("D2", "done"), hn("P", "partial"), hn("X", "cancelled"),
             hn("B1", "blocked", deps=["D1", "D2"]), hn("B2", "blocked", deps=["D1", "P"]),
             hn("B3", "blocked", deps=[]), hn("B4", "blocked", deps=["GONE"]),
             hn("T", "todo", deps=["D1", "X"]), hn("Z", "done", deps=["X"])]
    assert hc(nodes) == [("blocked_ready", "B1"), ("dep_cancelled", "T")]


@pytest.mark.parametrize("n,w", [(1, "день"), (2, "дні"), (4, "дні"), (5, "днів"), (11, "днів"), (12, "днів"),
                                 (21, "день"), (22, "дні"), (30, "днів"), (111, "днів")])
def test_days_plural(n, w):
    assert T._days(n) == f"{n} {w}"
