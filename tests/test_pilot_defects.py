"""E5-07: defects of scaffold/migrate found on the project-a pilot."""
import datetime
import json
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
BASE = {"project": "demo", "description": "A demo app.", "stack": "Python 3.12", "test_cmd": "uv run pytest -q"}


def answers(tmp_path, slots=None, **extra):
    p = tmp_path / "answers.json"
    d = {**BASE, **extra}
    if slots is not None:
        d["slots"] = slots
    p.write_text(json.dumps(d), encoding="utf8")
    return p


def w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf8")
    return p


def scaffold(*args):
    return runner.invoke(app, ["scaffold", *args])


def render(**slots):
    return S.render(S.parse_answers({**BASE, "slots": slots}))


# ------------------------------------------------------------ 1, 3, 4, 6: slots

def test_adr_path_slot_reaches_every_template_and_names_the_created_file():
    files = render(adr_path="docs/architecture/decisions.md")
    assert "docs/architecture/decisions.md" in files and "docs/decisions.md" not in files
    for rel in (".claude/agents/planner.md", ".claude/rules/tickets.md", ".claude/agents/docs-writer.md",
                ".claude/rules/workflow.md", "docs/todo/E1-foundation/index.md"):
        assert "docs/architecture/decisions.md" in files[rel], rel
        assert "docs/decisions.md" not in files[rel], rel


def test_adr_default_unchanged():
    files = render()
    assert "docs/decisions.md" in files and "`docs/decisions.md` (create it if missing)" in files[".claude/agents/planner.md"]


def test_parallel_lines_and_branch_pattern_slots_leave_no_hardcoded_values():
    files = render(parallel_lines="3", branch_pattern="t/<ticket-id>")
    wf, claude = files[".claude/rules/workflow.md"], files["CLAUDE.md"]
    assert "Up to 3 at a time" in wf and "up to 3," in claude
    assert "Up to 2" not in wf and "up to 2" not in claude
    for t in (wf, claude):
        assert "t/<ticket-id>-<slug>" not in t
    assert "`t/<ticket-id>`" in wf and "e.g. `t/E1-02`" in wf and "-b t/<ticket-id> main" in wf


def test_pitfalls_slot_fills_the_section_body():
    c = render(pitfalls=["- Deploy: `make ship`.", "- Registry: `docs/registry.md`."])["CLAUDE.md"]
    assert "- Deploy: `make ship`.\n- Registry: `docs/registry.md`." in c and "None yet" not in c
    assert "None yet - add what bites." in render()["CLAUDE.md"]


@pytest.mark.parametrize("slots", [
    {"adr_path": "/abs/decisions.md"}, {"adr_path": "../x.md"}, {"adr_path": "docs/`x`.md"},
    {"adr_path": "docs/todo/adr.md"}, {"adr_path": "docs/"}, {"adr_path": "./docs/adr.md"},
    {"adr_path": "docs//adr.md"}, {"adr_path": "docs/./adr.md"}, {"adr_path": "docs/adr.txt"},
    {"adr_path": ".git/adr.md"}, {"adr_path": ".claude/adr.md"}, {"adr_path": "docs\\adr.md"},
    {"adr_path": "CLAUDE.md"}, {"adr_path": "docs/lessons.md"}, {"adr_path": "docs/DECISIONS.md", "x": 1} if False else {"adr_path": "DOCS/LESSONS.md"},
    {"adr_path": ".claude/rules/workflow.md"}, {"adr_path": "docs/todo/INDEX.md"}, {"adr_path": " docs/adr.md"}, {"parallel_lines": "0"}, {"parallel_lines": "two"},
    {"branch_pattern": "t/slug"}, {"branch_pattern": "t/<ticket-id> x"}, {"branch_pattern": "`t/<ticket-id>`"},
])
def test_bad_new_slots_are_refused(slots):
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "slots": slots})


# ------------------------------------------------------------ 5: command slots

@pytest.mark.parametrize("cmd", ["`npm run tickets:check`", "npm run tickets:check (or myvec ticket check)",
                                 "npm run a\nnpm run b"])
def test_command_slots_must_be_bare(cmd):
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "slots": {"tickets_check_cmd": cmd}})
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "slots": {"tickets_index_cmd": cmd}})


def test_test_and_lint_cmd_are_validated_too():
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "test_cmd": "pytest (see docs)"})
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "lint_cmd": "`ruff check .`"})


def test_adr_path_through_a_symlinked_directory_is_refused(tmp_path):
    root = tmp_path / "p"
    (tmp_path / "elsewhere").mkdir()
    root.mkdir()
    (root / "adr").symlink_to(tmp_path / "elsewhere")
    r = scaffold("--answers", str(answers(tmp_path, slots={"adr_path": "adr/decisions.md"})), "--root", str(root))
    assert r.exit_code == 2 and not list((tmp_path / "elsewhere").iterdir())


@pytest.mark.parametrize("cmd", [
    "uv run pytest -q && (cd ui && npm test)", "make a || make b", "(cd ui && npm test)", "FOO=1 BAR=2 pytest -q",
    "pytest -k 'a and b' \"x y\"", "npm run tickets:check", "cd site; npm test",
])
def test_legit_complex_commands_are_accepted(cmd):
    a = S.parse_answers({**BASE, "test_cmd": cmd, "slots": {"tickets_check_cmd": cmd, "tickets_index_cmd": cmd}})
    assert a.test_cmd == cmd


@pytest.mark.parametrize("cmd", ["npm test (or yarn test)", "npm test (see README)", "pytest (e.g. in ci)",
                                 "npm test — the main one"])
def test_explanation_tails_are_refused(cmd):
    with pytest.raises(S.ScaffoldError):
        S.parse_answers({**BASE, "test_cmd": cmd})


def test_bare_command_slot_is_accepted():
    c = render(tickets_check_cmd="npm run tickets:check")[".claude/rules/tickets.md"]
    assert "Check: `npm run tickets:check`;" in c


# ------------------------------------------------------------ 2: --keep on create

def test_keep_on_a_create_row_does_not_create_the_file(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    a = answers(tmp_path)
    r = scaffold("--answers", str(a), "--root", str(root), "--keep", "docs/lessons.md", "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    rows = {x["path"]: x["action"] for x in json.loads(r.stdout)}
    assert rows["docs/lessons.md"] == "keep" and rows["docs/decisions.md"] == "create"
    r = scaffold("--answers", str(a), "--root", str(root), "--keep", "docs/lessons.md")
    assert r.exit_code == 0, r.output
    assert not (root / "docs/lessons.md").exists() and (root / "docs/decisions.md").is_file()


# ------------------------------------------------------------ 11: first group in an existing project

def test_no_first_group_for_foreign_tickets_and_no_tickets(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    w(root, "docs/todo/E0-workflow/E0-01-x.md", "---\nid: E0-01\ntitle: A\nstatus: ⬜\nepic: E0\n---\n\n## За один захід\n")
    a = answers(tmp_path, first_group={"id": "E0", "slug": "workflow", "title": "W"})
    r = scaffold("--answers", str(a), "--root", str(root), "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    assert not [x for x in json.loads(r.stdout) if x["path"].endswith("index.md")]
    r = scaffold("--answers", str(a), "--root", str(tmp_path / "q"), "--no-tickets", "--dry-run", "--json")
    assert not [x for x in json.loads(r.stdout) if x["path"].startswith("docs/todo/E")]


def test_first_group_still_created_in_an_empty_project(tmp_path):
    r = scaffold("--answers", str(answers(tmp_path)), "--root", str(tmp_path / "e"), "--dry-run", "--json")
    assert any(x["path"] == "docs/todo/E1-foundation/index.md" for x in json.loads(r.stdout))


# ------------------------------------------------------------ 7: legacy project scripts

def legacy_project(root: Path) -> Path:
    w(root, "docs/todo/ticket-template.md", "---\nid:\n---\n\n## За один заход\n")
    w(root, "package.json", json.dumps({"scripts": {"tickets:check": "node scripts/tickets.mjs check",
                                                    "tickets:ok": "myvec ticket check", "test": "vitest"}}, indent=2))
    w(root, "scripts/tickets.mjs", "const epic = fm.epic; // reads tickets\n")
    w(root, "scripts/other.sh", "echo hello\n")
    w(root, "Makefile", "tickets:\n\tnode scripts/tickets.mjs index # epic\n")
    w(root, "docs/todo/E1-a/index.md", "---\nid: E1\ntitle: A\nkind: group\nstatus: ⬜\n---\n")
    return root


def test_legacy_scripts_reported_with_names_and_lines_only(tmp_path):
    legacy_project(tmp_path)
    hits = M.legacy_scripts(tmp_path)
    files = {f for f, _ in hits}
    assert files == {"package.json", "scripts/tickets.mjs", "Makefile"}
    assert ("package.json", 3) in hits   # the `tickets:check` line
    issues = M.legacy_script_issues(tmp_path)
    assert all(i.code == "legacy_script" and "myvec ticket" in i.message for i in issues)
    assert "const epic" not in " ".join(i.message for i in issues)


def test_legacy_scripts_cli_detect_and_check_warn_without_failing(tmp_path):
    legacy_project(tmp_path)
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--detect"])
    assert r.exit_code == 0 and "legacy_script" in r.stdout and "scripts/tickets.mjs" in r.stdout
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--check"])
    assert "legacy_script" in r.stdout and "package.json" in r.stdout and r.exit_code == 0, r.output
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--detect", "--json"])
    json.loads(r.stdout)   # stdout stays valid JSON
    assert "legacy_script" not in r.stdout
    r = runner.invoke(ticket_app, ["migrate", "--root", str(tmp_path), "--detect", "--json"])
    assert "legacy_script" in r.stderr and "legacy_script" not in r.stdout and json.loads(r.stdout)


def test_legacy_scan_never_opens_denied_files(tmp_path, monkeypatch):
    for name in (".env", ".env.local", ".dev.vars", "tickets.key", "tickets-secret.sh"):
        w(tmp_path, f"scripts/{name}", "tickets epic\n")
    w(tmp_path, "scripts/tickets.mjs", "tickets epic\n")
    opened = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    hits = M.legacy_scripts(tmp_path)
    monkeypatch.setattr(Path, "read_text", real)
    assert [f for f, _ in hits] == ["scripts/tickets.mjs"]
    assert opened == ["tickets.mjs"]


def test_migrated_scripts_are_not_reported(tmp_path):
    w(tmp_path, "package.json", json.dumps({"scripts": {"tickets:check": "myvec ticket check"}}))
    w(tmp_path, "scripts/tickets.sh", "myvec ticket check # epic\n")
    assert M.legacy_scripts(tmp_path) == []


# ------------------------------------------------------------ 9: group status from children

def group_tree(root: Path, statuses: list[str], emoji=True) -> Path:
    w(root, "docs/todo/E1-g/index.md", "# Group without a header\n")
    for i, st in enumerate(statuses, 1):
        w(root, f"docs/todo/E1-g/E1-0{i}-t.md", f"---\nid: E1-0{i}\ntitle: T\nstatus: {st}\nparent: E1\n---\n")
    return root


def group_status(root: Path) -> str:
    M.apply_fixes(root, M.fix_plan(root, "docs/todo"))
    return T.parse_frontmatter((root / "docs/todo/E1-g/index.md").read_text(encoding="utf8"))[0]["status"]


def test_fix_group_status_all_done_takes_latest_date(tmp_path):
    assert group_status(group_tree(tmp_path, ["✅ 2026-11-03", "✅ 2026-11-09", "🚫"])) == "✅ 2026-11-09"


def test_fix_group_status_in_progress_and_todo(tmp_path):
    assert group_status(group_tree(tmp_path / "a", ["✅ 2026-11-03", "🟨"])) == "🟦"
    assert group_status(group_tree(tmp_path / "b", ["⬜", "⬜"])) == "⬜"


def test_fix_group_status_keys_form(tmp_path):
    assert group_status(group_tree(tmp_path / "a", ["done 2026-11-02", "done 2026-11-05"])) == "done 2026-11-05"
    assert group_status(group_tree(tmp_path / "b", ["todo", "in_progress"])) == "in_progress"


def test_fix_keeps_an_existing_valid_group_status(tmp_path):
    group_tree(tmp_path, ["✅ 2026-11-03"])
    w(tmp_path, "docs/todo/E1-g/index.md", "---\nid: E1\ntitle: G\nkind: group\nstatus: 🟨\n---\n")
    assert M.fix_plan(tmp_path, "docs/todo") == []


# ------------------------------------------------------------ 8: counters

def tree(tmp_path: Path, lang_marker="## За один заход") -> T.TicketSet:
    w(tmp_path, "docs/todo/ticket-template.md", f"---\nid:\n---\n\n{lang_marker}\n")
    w(tmp_path, "docs/todo/E1-g/index.md", "---\nid: E1\ntitle: G\nkind: group\nstatus: 🟦\n---\n")
    w(tmp_path, "docs/todo/E1-g/E1-01-a.md",
      "---\nid: E1-01\ntitle: A\nstatus: 🟨\nparent: E1\n---\n\n## Acceptance criteria\n\n- [x] a\n- [x] b\n- [x] c\n- [ ] d\n")
    w(tmp_path, "docs/todo/E1-g/E1-02-b.md", "---\nid: E1-02\ntitle: B\nstatus: ✅ 2026-11-09\nparent: E1\n---\n")
    return T.load_dir(tmp_path, "docs/todo")


@pytest.mark.parametrize("lang,head", [("uk", "Загальний прогрес"), ("en", "Overall progress"), ("ru", "Общий прогресс")])
def test_index_total_has_the_same_counter_as_groups(tmp_path, lang, head):
    ts = tree(tmp_path)
    out = ops.render_index(ts, "docs/todo", lang)
    assert out.splitlines()[0] == f"{head}: 88% (1/2)"
    assert "— 88% (1/2 " in out   # the group line: weighted percent, then the finished-leaf counter


def test_group_table_counter_matches(tmp_path):
    ts = tree(tmp_path)
    assert ops.render_group_table(ts, ts.tickets["E1"], "uk").startswith("Прогрес: 88% (1/2)")


# ------------------------------------------------------------ 10: check output language

def test_health_messages_follow_language():
    n = {"id": "A", "kind": "ticket", "status": "todo", "size": "L", "ac_total": 0, "path": "p", "updated": "2026-10-01"}
    today = datetime.date(2026, 11, 10)
    for lang, size_word, stale in (("uk", "Завеликий", "Не оновлювався 40 днів"), ("ru", "Слишком", "Не обновлялся 40 дней"),
                                   ("en", "Too big", "Not updated for 40 days")):
        msgs = [i.message for i in T.health([n], today, lang=lang)]
        assert msgs[0].startswith(size_word) and stale in msgs
    assert T.health([n], today)[0].message == T.health([n], today, lang="uk")[0].message


@pytest.mark.parametrize("marker,tag,word", [("## In one pass", "tip", "tickets"), ("## За один заход", "советов", "тикетов"),
                                             ("## За один захід", "порад", "тікетів")])
def test_ticket_check_summary_language(tmp_path, marker, tag, word):
    tree(tmp_path, marker)
    r = runner.invoke(ticket_app, ["check", "--root", str(tmp_path)])
    last = r.stdout.strip().splitlines()[-1]
    assert word in last and tag in last, last
