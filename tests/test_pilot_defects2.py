"""E5-08: defects of scaffold/migrate found on the project-b and project-c.lv pilots."""
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec import scaffold as S
from myvec import ticketmigrate as M
from myvec import ticketops as ops
from myvec import tickets as T
from myvec.commands import app
from myvec.ticketcli import ticket_app

runner = CliRunner()
BASE = {"project": "demo", "description": "A demo app.", "stack": "PHP", "test_cmd": "php artisan test",
        "machines": "Mac, devbox"}
GIT_FILES = (".claude/rules/workflow.md", "CLAUDE.md", ".claude/agents/reviewer.md", ".claude/agents/docs-writer.md")


def render(**slots):
    return S.render(S.parse_answers({**BASE, "slots": slots}))


def w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf8")
    return p


# ---------------------------------------------------------------- 1: git slots

def test_default_git_text_unchanged_github_main_worktree():
    wf = render()[".claude/rules/workflow.md"]
    assert "`gh pr create`" in wf and "git worktree add .worktrees/<ticket-id> -b t/<ticket-id>-<slug> main" in wf
    assert "git diff main...HEAD" in wf and "PR open" in wf


def test_gitlab_dev_none_has_no_github_main_or_worktree_add():
    files = render(vcs_host="gitlab", base_branch="dev", parallel_mode="none", no_unrequested_commits=True)
    for rel in GIT_FILES:
        t = files[rel]
        assert "gh pr" not in t and "GitHub" not in t, rel
        assert "worktree add" not in t, rel
        assert "`main`" not in t and "main..." not in t, rel
    wf = files[".claude/rules/workflow.md"]
    assert "glab mr create" in wf and "git diff dev...HEAD" in wf and "MR open" in wf
    assert "No worktrees" in wf and "Add `.worktrees/`" not in wf
    assert "nobody commits, pushes, merges" in wf
    assert "no worktrees" in files["CLAUDE.md"] and "worktrees `.worktrees" not in files["CLAUDE.md"]
    assert "in a worktree" not in files[".claude/agents/qa.md"]


def test_no_remote_host_has_no_push_or_pr_text():
    files = render(vcs_host="none")
    wf = files[".claude/rules/workflow.md"]
    assert "gh pr" not in wf and "glab" not in wf and "opens the PR" not in wf
    assert "local merge" in wf
    assert "`git push`" not in wf       # machines line needs a remote


@pytest.mark.parametrize("slot,val", [("vcs_host", "svn"), ("parallel_mode", "many"), ("base_branch", "a b"),
                                      ("base_branch", "x`y"), ("base_branch", "../x")])
def test_git_slots_validated(slot, val):
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "slots": {slot: val}})


def test_unrequested_commits_must_be_boolean():
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "slots": {"no_unrequested_commits": "yes"}})


def test_gitignore_skips_worktrees_when_parallel_mode_none():
    a = S.parse_answers({**BASE, "slots": {"parallel_mode": "none"}})
    assert ".worktrees/" not in (S._merge_gitignore("", a) or "")
    assert ".worktrees/" in S._merge_gitignore("", S.parse_answers(BASE))


# ---------------------------------------------------------------- 2: --append

def test_append_adds_stack_and_pitfalls_once(tmp_path):
    ans = tmp_path / "a.json"
    ans.write_text(json.dumps({**BASE, "slots": {"pitfalls": ["- Sail only.", "- No docker prune."]}}))
    proj = tmp_path / "p"
    w(proj, "CLAUDE.md", "# Mine\n\n<laravel-boost-guidelines>\nx\n</laravel-boost-guidelines>\n")
    r = runner.invoke(app, ["scaffold", "--answers", str(ans), "--root", str(proj), "--append", "CLAUDE.md",
                            "--no-tickets"])
    assert r.exit_code == 0, r.output
    text = (proj / "CLAUDE.md").read_text()
    assert text.startswith("# Mine\n") and "## Stack and commands" in text and "- Sail only." in text
    assert text.index("## Pitfalls") < text.index("## Development process")
    again = runner.invoke(app, ["scaffold", "--answers", str(ans), "--root", str(proj), "--append", "CLAUDE.md",
                                "--no-tickets"])
    assert again.exit_code != 0
    assert (proj / "CLAUDE.md").read_text().count("## Pitfalls") == 1


def test_append_sections_skips_existing_headings():
    rendered = render()["CLAUDE.md"]
    add = S.append_sections(rendered, "# X\n\n## Pitfalls\n\nmine\n")
    assert "## Pitfalls" not in add and "## Stack and commands" in add and "## Development process" in add


# ---------------------------------------------------------------- 5: parser

def test_russian_acceptance_headings_count_checkboxes():
    for head in ("Критерии приёмки", "Критерии приемки", "Критерии готовности", "Критерії прийняття"):
        assert T.acceptance_checkboxes(f"## {head}\n\n- [x] a\n- [ ] b\n") == (1, 2), head


def test_multiline_header_values_parse():
    text = ("---\nid: A-1\ntitle: T\nstatus: todo\ndesign: first line\n  second line\n"
            "note: \"quoted\n  over lines\"\ndeps: [A-2,\n  A-3]\nsummary: >\n  folded\n  text\nblocks:\n  - A-4\n---\nbody\n")
    d, body = T.parse_frontmatter(text)
    assert d["design"] == "first line second line" and d["note"] == "quoted over lines"
    assert d["deps"] == ["A-2", "A-3"] and d["summary"] == "folded text" and d["blocks"] == ["A-4"]
    assert body == "body\n"


def test_set_fm_line_refuses_multiline_value():
    text = "---\nid: A-1\ndesign: one\n  two\n---\n"
    with pytest.raises(ops.TicketOpError):
        ops.set_fm_line(text, "design", "x")


# ---------------------------------------------------------------- 6: language

def ticket(tid, title, body, parent=None, status="todo"):
    head = f"---\nid: {tid}\ntitle: {title}\nstatus: {status}\n" + (f"parent: {parent}\n" if parent else "") + "---\n"
    return head + body


def test_lang_from_ticket_headings_without_template(tmp_path):
    base = tmp_path / "docs/todo"
    w(base, "A-1.md", ticket("A-1", "x", "## За один заход\n\nТекст\n## Критерии приёмки\n- [ ] a\n"))
    assert ops.lang_for(base) == "ru"
    w(tmp_path / "u/todo", "A-1.md", ticket("A-1", "x", "## За один захід\n\nТекст\n"))
    assert ops.lang_for(tmp_path / "u/todo") == "uk"
    w(tmp_path / "e/todo", "A-1.md", ticket("A-1", "x", "## In one pass\n\nText\n"))
    assert ops.lang_for(tmp_path / "e/todo") == "en"
    assert ops.lang_for(tmp_path / "none") == "uk"


def test_ru_index_uses_russian_headings(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "x", "## За один заход\n\nТекст\n"))
    r = runner.invoke(ticket_app, ["index", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "docs/todo/INDEX.md").read_text().startswith("# Тикеты")


# ---------------------------------------------------------------- 7: INDEX collision

def test_index_refuses_case_twin_and_accepts_index_name(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "x", "## In one pass\n\nT\n"))
    w(tmp_path, "docs/todo/index.md", "human index\n")
    r = runner.invoke(ticket_app, ["index", "--root", str(tmp_path)])
    assert r.exit_code == 2 and "index.md" in r.output
    assert not (tmp_path / "docs/todo/INDEX.md").exists()
    assert (tmp_path / "docs/todo/index.md").read_text() == "human index\n"
    r = runner.invoke(ticket_app, ["index", "--root", str(tmp_path), "--index-name", "TICKETS.md"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "docs/todo/TICKETS.md").is_file()
    assert (tmp_path / "docs/todo/index.md").read_text() == "human index\n"
    bad = runner.invoke(ticket_app, ["index", "--root", str(tmp_path), "--index-name", "../x.md"])
    assert bad.exit_code == 2


def test_scaffold_plan_marks_case_twin_as_conflict(tmp_path):
    w(tmp_path, "docs/todo/index.md", "human\n")
    a = S.parse_answers(BASE)
    pl = S.plan(tmp_path, S.render(a), a)
    assert dict(pl)["docs/todo/INDEX.md"] == "conflict"


# ---------------------------------------------------------------- 4, 8: --fix

def test_fix_assigns_ids_parent_title_deterministically(tmp_path):
    base = "docs/todo"
    w(tmp_path, f"{base}/site/index.md", "---\ntitle: Site\n---\n# Site\n")
    w(tmp_path, f"{base}/site/01-home.md", "---\nstatus: done\n---\n# Home page\n\n## За один заход\n\nT\n")
    w(tmp_path, f"{base}/site/02-about.md", "---\nstatus: todo\n---\n# About\n")
    w(tmp_path, f"{base}/E3-x/E3-07-seven.md", "---\nstatus: todo\n---\n# Seven\n")
    w(tmp_path, f"{base}/E3-x/notes.md", "---\nstatus: todo\n---\n# Notes\n")
    w(tmp_path, f"{base}/E3-x/index.md", "---\nstatus: todo\n---\n# E3\n")
    plan = M.fix_plan(tmp_path, base)
    M.apply_fixes(tmp_path, plan)
    ts = T.load_dir(tmp_path, base)
    assert not [i for i in ts.issues if i.severity == "error"], ts.issues
    assert ts.tickets["SITE"].kind == "group"
    assert ts.tickets["01-home"].parent == "SITE" and ts.tickets["02-about"].title == "About"
    assert ts.tickets["E3-07"].parent == "E3" and ts.tickets["notes"].path.endswith("notes.md")
    assert M.fix_plan(tmp_path, base) == []          # idempotent
    # bodies are untouched
    assert (tmp_path / base / "site/02-about.md").read_text().endswith("---\n# About\n")


def test_fix_never_reuses_an_existing_id(tmp_path):
    base = "docs/todo"
    w(tmp_path, f"{base}/g/index.md", "---\nid: G\ntitle: G\nkind: group\nstatus: todo\n---\n")
    w(tmp_path, f"{base}/g/G-01-a.md", "---\nid: G-01\ntitle: A\nstatus: todo\nparent: G\n---\n")
    w(tmp_path, f"{base}/g/b.md", "---\nstatus: todo\n---\n# B\n")
    M.apply_fixes(tmp_path, M.fix_plan(tmp_path, base))
    ts = T.load_dir(tmp_path, base)
    assert ts.tickets["b"].parent == "G" and len(ts.tickets) == 3


def test_fix_does_not_add_goal_without_flag_and_adds_it_with_flag(tmp_path):
    base = "docs/todo"
    w(tmp_path, f"{base}/A-1.md", ticket("A-1", "Alpha", "# Alpha\n\nIntro paragraph.\n\n## Log\n"))
    w(tmp_path, f"{base}/A-2.md", ticket("A-2", "Beta", "## Log\n"))
    w(tmp_path, f"{base}/A-3.md", ticket("A-3", "Gamma", "## За один заход\n\nHave.\n"))
    w(tmp_path, f"{base}/A-4.md", ticket("A-4", "Delta", ""))
    before = {p.name: p.read_text() for p in (tmp_path / base).glob("*.md")}
    assert M.fix_plan(tmp_path, base) == []
    plan = M.fix_plan(tmp_path, base, add_goal=True)
    assert {f.path.rsplit("/", 1)[1] for f in plan} == {"A-1.md", "A-2.md", "A-4.md"}
    M.apply_fixes(tmp_path, plan)
    a1 = (tmp_path / base / "A-1.md").read_text()
    assert "## За один заход\n\nIntro paragraph." in a1     # the project language (A-3 is Russian)
    ts = T.load_dir(tmp_path, base)
    assert T.body_sections(ts.tickets["A-1"].body)["goal"] == "Intro paragraph."
    assert T.body_sections(ts.tickets["A-2"].body)["goal"] == "Beta"
    assert T.body_sections(ts.tickets["A-4"].body)["goal"] == "Delta"
    for name in ("A-3.md",):
        assert (tmp_path / base / name).read_text() == before[name]
    assert M.fix_plan(tmp_path, base, add_goal=True) == []


def test_no_goal_is_not_a_leftover_by_default(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "x", "## Log\n"))
    issues, _ = M.migration_check(tmp_path, "docs/todo")
    assert any(i.code == "no_goal" for i in issues)
    assert not [i for i in M.left_over(issues) if i.code == "no_goal"]


def test_cli_add_goal_heading_requires_fix(tmp_path):
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--add-goal-heading"])
    assert r.exit_code == 2


def test_group_status_with_partial_child_is_in_progress(tmp_path):
    g = tmp_path / "E1-x"
    w(g, "E1-01-a.md", ticket("E1-01", "a", "", status="done 2026-11-01"))
    w(g, "E1-02-b.md", ticket("E1-02", "b", "", status="partial"))
    assert M.group_status_from_children(g, False) == "in_progress"
    assert M.group_status_from_children(g, True) == "🟦"


# ---------------------------------------------------------------- 3: --split

TODO = """# План

Преамбула проєкту.

## Задачи

### T1. Первая задача

- [x] a
- [x] b

Подробности один.

### T2. Вторая задача

- [x] c
- [ ] d

#### Подзаголовок

внутри

### T3. Третья

нет чекбоксов

## Сделано сегодня

Строка журнала.

## Журнал

- запись
"""


def split_proj(tmp_path):
    w(tmp_path, "docs/todo.md", TODO)
    return tmp_path


def test_split_dry_run_writes_nothing_and_write_is_verbatim(tmp_path):
    split_proj(tmp_path)
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--dir", "docs/todo.md", "--split"])
    assert r.exit_code == 0 and "dry-run" in r.output and "3 tickets" in r.output
    assert (tmp_path / "docs/todo.md").read_text() == TODO and not (tmp_path / "docs/todo").exists()
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--dir", "docs/todo.md", "--split", "--write",
                                   "--group-id", "E1", "--group-slug", "prod"])
    assert r.exit_code == 0, r.output
    g = tmp_path / "docs/todo/E1-prod"
    names = sorted(p.name for p in g.glob("*.md"))
    assert names == ["E1-01-t1.md", "E1-02-t2.md", "E1-03-t3.md", "index.md"]
    out = "\n".join(p.read_text() for p in sorted(g.glob("*.md")))
    for line in TODO.splitlines():
        if line.strip():
            want = "# " + line[4:] if re.match(r"### T\d\.", line) else line   # the item heading becomes the H1
            assert want in out, line
    ts = T.load_dir(tmp_path, "docs/todo")
    assert not [i for i in ts.issues if i.severity == "error"], ts.issues
    assert ts.tickets["E1-01"].status == "done" and ts.tickets["E1-02"].status == "partial"
    assert ts.tickets["E1-03"].status == "todo" and ts.tickets["E1"].status == "in_progress"
    assert ts.tickets["E1-02"].ac_total == 0       # bodies are not edited: no Acceptance heading invented
    idx = (g / "index.md").read_text()
    assert "Строка журнала." in idx and "Преамбула проєкту." in idx
    assert M.POINTER_MARK in (tmp_path / "docs/todo.md").read_text()


def test_split_pointer_is_not_a_candidate_any_more(tmp_path):
    split_proj(tmp_path)
    assert "docs/todo.md" in M.find_candidates(tmp_path)
    M.apply_split(tmp_path, M.split_plan(tmp_path, "docs/todo.md"))
    assert "docs/todo.md" not in M.find_candidates(tmp_path)
    issues, _ = M.migration_check(tmp_path, "docs/todo")
    assert not [i for i in issues if i.code == "leftover_todo"]


def test_split_refuses_existing_dest_and_files_without_items(tmp_path):
    split_proj(tmp_path)
    (tmp_path / "docs/todo/E1-backlog").mkdir(parents=True)
    with pytest.raises(ops.TicketOpError):
        M.split_plan(tmp_path, "docs/todo.md")
    w(tmp_path, "empty.md", "# Just text\n\nno items\n")
    with pytest.raises(ops.TicketOpError):
        M.split_plan(tmp_path, "empty.md", dest="out")


def test_split_falls_back_to_h2_sections(tmp_path):
    w(tmp_path, "todo.md", "# Todo\n\n## Alpha\n\n- [ ] a\n\n## Beta\n\ntext\n")
    files = M.split_plan(tmp_path, "todo.md")
    assert [f.kind for f in files] == ["group", "ticket", "ticket", "pointer"]


def test_split_requires_dir_and_stays_inside_root(tmp_path):
    assert runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--split"]).exit_code == 2
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--dir", "../x.md", "--split"])
    assert r.exit_code == 2


# ---------------------------------------------------------------- minor items

def test_index_head_has_statuses_and_canon_link_when_index_is_created(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "x", "## In one pass\n\nT\n"))
    assert runner.invoke(ticket_app, ["index", "--root", str(tmp_path)]).exit_code == 0
    head = (tmp_path / "docs/todo/INDEX.md").read_text()
    assert "## Statuses" in head and "tickets.md" in head and "<!-- generated:start -->" in head


def test_ticket_template_flag_writes_only_the_template_under_no_tickets(tmp_path):
    ans = tmp_path / "a.json"
    ans.write_text(json.dumps(BASE))
    proj = tmp_path / "p"
    proj.mkdir()
    r = runner.invoke(app, ["scaffold", "--answers", str(ans), "--root", str(proj), "--no-tickets",
                            "--ticket-template", "--json", "--dry-run"])
    assert r.exit_code == 0, r.output
    acts = {x["path"]: x["action"] for x in json.loads(r.stdout)}
    assert acts["docs/todo/ticket-template.md"] == "create" and acts["docs/todo/INDEX.md"] == "skip"


def test_detect_sail_commands_and_cyrillic_shares(tmp_path):
    from myvec.scaffold import detect as D

    w(tmp_path, "composer.json", json.dumps({"require": {"laravel/framework": "^11"},
                                              "require-dev": {"laravel/sail": "^1", "laravel/pint": "^1"}}))
    w(tmp_path, "artisan", "#!/usr/bin/env php\n")
    w(tmp_path, "compose.yaml", "services: {}\n")
    w(tmp_path, "README.md", "# Проект\n\nЭто описание проекта на русском языке для проверки доли кириллицы.\n")
    rep = D.detect(tmp_path)
    assert rep["answers"]["test_cmd"] == "./vendor/bin/sail artisan test"
    assert rep["answers"]["lint_cmd"] == "./vendor/bin/sail bin pint --test"
    assert rep["docs_language_cyrillic_percent"]["README.md"] > 90


def test_index_hides_zero_percent_without_acceptance_criteria(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "Half", "## In one pass\n\nT\n", status="partial"))
    w(tmp_path, "docs/todo/A-2.md", ticket("A-2", "Boxes", "## Acceptance criteria\n\n- [ ] a\n", status="in_progress"))
    ts = T.load_dir(tmp_path, "docs/todo")
    text = ops.render_index(ts, "docs/todo", "en")
    assert "Half — 0%" not in text and "Half" in text
    assert "Boxes — 0%" in text


# ---------------------------------------------------------------- review fixes

def test_fix_inserts_keys_on_a_key_boundary_and_keeps_multiline_values(tmp_path):
    base = "docs/todo"
    w(tmp_path, f"{base}/E1-g/index.md", "---\nid: E1\ntitle: G\nkind: group\nstatus: todo\n---\n")
    w(tmp_path, f"{base}/E1-g/E1-01-a.md",
      "---\ntitle: Very long title\n  that continues here\nsummary: |\n  l1\n  l2\nblocks:\n  - X\nstatus: todo\n---\n# H\n")
    M.apply_fixes(tmp_path, M.fix_plan(tmp_path, base))
    d, _ = T.parse_frontmatter((tmp_path / base / "E1-g/E1-01-a.md").read_text())
    assert d["title"] == "Very long title that continues here" and d["parent"] == "E1"
    assert d["summary"] == "l1\nl2" and d["blocks"] == ["X"] and d["id"] == "E1-01"


def test_fix_then_strict_check_is_green(tmp_path):
    base = "docs/todo"
    w(tmp_path, f"{base}/site/index.md", "---\ntitle: Site\n---\n# Site\n")
    w(tmp_path, f"{base}/site/01-home.md", "---\nstatus: done\n---\n# Home\n")
    w(tmp_path, f"{base}/review/zz.md", "---\nstatus: todo\n---\n# ZZ\n")
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--dir", base, "--fix", "--write"])
    assert r.exit_code == 0, r.output
    c = runner.invoke(ticket_app, ["check", "--root", str(tmp_path), "--strict"])
    assert c.exit_code == 0, c.output


def test_migrate_check_accepts_custom_index_name(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "x", "## In one pass\n\nT\n\n## Acceptance criteria\n\n- [ ] a\n"))
    w(tmp_path, "docs/todo/index.md", "human\n")
    assert runner.invoke(ticket_app, ["index", "--root", str(tmp_path), "--index-name", "TICKETS.md"]).exit_code == 0
    issues, _ = M.migration_check(tmp_path, "docs/todo")
    assert not [i for i in issues if i.path.endswith("TICKETS.md")]


def test_repeated_split_is_a_noop(tmp_path):
    split_proj(tmp_path)
    args = ["migrate", "--root", str(tmp_path), "--dir", "docs/todo.md", "--split", "--write"]
    assert runner.invoke(ticket_app, args).exit_code == 0
    r = runner.invoke(ticket_app, args)
    assert r.exit_code == 0 and "already split" in r.output


def test_split_pointer_rechecks_source(tmp_path):
    split_proj(tmp_path)
    files = M.split_plan(tmp_path, "docs/todo.md")
    (tmp_path / "docs/todo.md").write_text(TODO + "\nnew line\n")
    with pytest.raises(ops.TicketOpError):
        M.apply_split(tmp_path, files)
    assert not (tmp_path / "docs/todo").exists()


def test_gitlab_article_and_no_push_line_with_commit_rule():
    files = render(vcs_host="gitlab", no_unrequested_commits=True)
    assert "opens an MR" in files[".claude/rules/workflow.md"] and "opens a MR" not in files["CLAUDE.md"]
    assert "git push" not in files[".claude/rules/workflow.md"].split("Project rule")[0].split("## Git")[1].replace("pushes and opens", "")


def test_localized_status_legend_in_new_index(tmp_path):
    w(tmp_path, "docs/todo/A-1.md", ticket("A-1", "x", "## За один заход\n\nT\n"))
    runner.invoke(ticket_app, ["index", "--root", str(tmp_path)])
    assert "в работе" in (tmp_path / "docs/todo/INDEX.md").read_text()
