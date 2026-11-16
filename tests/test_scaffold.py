import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec import scaffold as S
from myvec.commands import app

runner = CliRunner()
FIX = Path(__file__).parent / "fixtures" / "scaffold" / "myvec.answers.json"

BASE = {"project": "demo", "description": "A demo app.", "stack": "Python 3.12 + FastAPI",
        "test_cmd": "uv run pytest -q"}


def write_answers(tmp_path, **extra):
    d = {**BASE, **extra}
    p = tmp_path / "answers.json"
    p.write_text(json.dumps(d), encoding="utf8")
    return p


def run(*args):
    return runner.invoke(app, ["scaffold", *args])


def snapshot(root: Path):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_creates_full_process_in_empty_dir(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path, lint_cmd="ruff check .")), "--root", str(root))
    assert r.exit_code == 0, r.output
    for rel in ("CLAUDE.md", ".claude/rules/workflow.md", ".claude/rules/tickets.md", ".claude/settings.json",
                "docs/todo/INDEX.md", "docs/todo/ticket-template.md", "docs/todo/E1-foundation/index.md",
                "docs/decisions.md", "docs/lessons.md", ".gitignore",
                *(f".claude/agents/{a}.md" for a in S.AGENTS)):
        assert (root / rel).is_file(), rel
    for p in root.rglob("*"):
        if p.is_file():
            t = p.read_text(encoding="utf8")
            assert "{{" not in t and "<!-- if:" not in t, p
    claude = (root / "CLAUDE.md").read_text(encoding="utf8")
    assert "# demo" in claude and "`uv run pytest -q`" in claude and "ruff check ." in claude
    # nothing of myvec's own project leaks into a generic project
    for rel in (".claude/agents/coder.md", ".claude/agents/qa.md", ".claude/rules/workflow.md"):
        t = (root / rel).read_text(encoding="utf8")
        for leak in ("filters.py", "bench/", ":7878", "MYVEC_HOME", "~/MyVector"):
            assert leak not in t, (rel, leak)
    assert "mcp__claude-in-chrome" not in (root / ".claude/agents/qa.md").read_text(encoding="utf8")
    check = subprocess.run(["uv", "run", "myvec", "ticket", "check", "--strict", "--root", str(root)],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr


def test_ui_answers_enable_browser_rules(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path, ui={"url": "http://localhost:3000", "check": "Open it."})),
            "--root", str(root))
    assert r.exit_code == 0, r.output
    qa = (root / ".claude/agents/qa.md").read_text(encoding="utf8")
    assert "mcp__claude-in-chrome__*" in qa and "http://localhost:3000" in qa
    assert "Live check (qa)" in (root / ".claude/rules/workflow.md").read_text(encoding="utf8")


def test_keys_status_style(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path, status_style="keys")), "--root", str(root))
    assert r.exit_code == 0, r.output
    assert "status: todo" in (root / "docs/todo/E1-foundation/index.md").read_text(encoding="utf8")
    assert "⬜ →" not in (root / ".claude/rules/workflow.md").read_text(encoding="utf8")


def test_rerun_is_idempotent(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    a = write_answers(tmp_path)
    assert run("--answers", str(a), "--root", str(root)).exit_code == 0
    before = snapshot(root)
    r = run("--answers", str(a), "--root", str(root), "--json")
    assert r.exit_code == 0, r.output
    assert {x["action"] for x in json.loads(r.stdout)} == {"same"}
    assert snapshot(root) == before


def test_conflict_is_all_or_nothing_and_overwrite_is_selective(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    a = write_answers(tmp_path)
    assert run("--answers", str(a), "--root", str(root)).exit_code == 0
    (root / "CLAUDE.md").write_text("hand written\n", encoding="utf8")
    (root / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    (root / ".claude/agents/tester.md").unlink()
    before = snapshot(root)
    r = run("--answers", str(a), "--root", str(root))
    assert r.exit_code == 3
    assert "CLAUDE.md" in r.output and "coder.md" in r.output
    assert snapshot(root) == before  # the missing tester.md was not created either
    r = run("--answers", str(a), "--root", str(root), "--overwrite", "CLAUDE.md")
    assert r.exit_code == 3 and (root / "CLAUDE.md").read_text(encoding="utf8") == "hand written\n"
    r = run("--answers", str(a), "--root", str(root), "--overwrite", "CLAUDE.md", "--overwrite", ".claude/agents/coder.md")
    assert r.exit_code == 0, r.output
    assert (root / "CLAUDE.md").read_text(encoding="utf8").startswith("# demo")
    assert (root / ".claude/agents/tester.md").is_file()


def test_overwrite_of_unknown_path_is_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root), "--overwrite", "README.md")
    assert r.exit_code == 2
    assert not any(root.iterdir())


def test_dry_run_writes_nothing_and_reports_json(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root), "--dry-run", "--json")
    assert r.exit_code == 0
    plan = json.loads(r.stdout)
    assert {x["action"] for x in plan} == {"create"} and {"path", "action"} == set(plan[0])
    assert not any(root.iterdir())


def test_dry_run_reports_conflicts_with_code_3(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("mine\n", encoding="utf8")
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root), "--dry-run", "--json")
    assert r.exit_code == 3
    assert {"path": "CLAUDE.md", "action": "conflict"} in json.loads(r.stdout)


def test_gitignore_and_settings_are_merged_not_replaced(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".gitignore").write_text("node_modules/\n.worktrees/", encoding="utf8")
    (root / ".claude/settings.json").write_text(json.dumps(
        {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]}}), encoding="utf8")
    a = write_answers(tmp_path)
    r = run("--answers", str(a), "--root", str(root), "--json")
    assert r.exit_code == 0, r.output
    acts = {x["path"]: x["action"] for x in json.loads(r.stdout)}
    assert acts[".gitignore"] == "merge" and acts[".claude/settings.json"] == "merge"
    assert (root / ".gitignore").read_text(encoding="utf8") == "node_modules/\n.worktrees/\n*.bak-*\n"
    data = json.loads((root / ".claude/settings.json").read_text(encoding="utf8"))
    assert data["model"] == "opus" and data["permissions"]["deny"] == ["Bash(rm:*)"]
    assert data["permissions"]["allow"] == ["Bash(ls:*)", "Bash(uv run pytest:*)", "Bash(myvec ticket:*)"]
    assert list((root / ".claude").glob("settings.json.bak-*"))
    (root / ".gitignore").write_text("dist/", encoding="utf8")
    assert run("--answers", str(a), "--root", str(root)).exit_code == 0
    assert (root / ".gitignore").read_text(encoding="utf8") == "dist/\n.worktrees/\n*.bak-*\n"


def test_broken_settings_json_is_a_conflict(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude/settings.json").write_text("{not json", encoding="utf8")
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root))
    assert r.exit_code == 3
    assert (root / ".claude/settings.json").read_text(encoding="utf8") == "{not json"


@pytest.mark.parametrize("extra,drop", [
    ({"surprise": 1}, None),
    ({}, "test_cmd"),
    ({"slots": {"nope": "x"}}, None),
    ({"slots": {"has_live_check": "yes"}}, None),
    ({"status_style": "words"}, None),
    ({"ui": {"url": "http://x"}}, None),
    ({"first_group": {"id": "../x", "slug": "a", "title": "T"}}, None),
    ({"description": "line1\nline2"}, None),
    ({"description": "uses {{project}}"}, None),
])
def test_invalid_answers_exit_2_and_write_nothing(tmp_path, extra, drop):
    d = {**BASE, **extra}
    if drop:
        d.pop(drop)
    p = tmp_path / "a.json"
    p.write_text(json.dumps(d), encoding="utf8")
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(p), "--root", str(root))
    assert r.exit_code == 2, r.output
    assert not any(root.iterdir())


def test_unreadable_answers_exit_2(tmp_path):
    r = run("--answers", str(tmp_path / "missing.json"), "--root", str(tmp_path))
    assert r.exit_code == 2


@pytest.mark.parametrize("secret", [
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "AKIAIOSFODNN7EXAMPLE",
])
def test_secret_in_answers_is_rejected_without_echo(tmp_path, secret):
    p = write_answers(tmp_path, slots={"coder_rules": secret.replace("\n", " ")}, description=f"x {secret.replace(chr(10), ' ')}")
    root = tmp_path / "proj"
    root.mkdir()
    r = runner.invoke(app, ["scaffold", "--answers", str(p), "--root", str(root)])
    assert r.exit_code == 2
    assert "AKIAIOSFODNN7EXAMPLE" not in r.output and "BEGIN RSA" not in r.output
    assert not any(root.iterdir())


def test_render_text_language_errors():
    ctx = {"a": "x", "yes": True, "text": "t"}
    assert S.render_text("v={{a}}\n<!-- if:yes -->\nin\n<!-- endif -->\n", ctx) == "v=x\nin\n"
    with pytest.raises(S.ScaffoldError):
        S.render_text("{{missing}}", ctx)
    with pytest.raises(S.ScaffoldError):
        S.render_text("<!-- if:yes -->\nnever closed", ctx)
    with pytest.raises(S.ScaffoldError):
        S.render_text("<!-- endif -->", ctx)
    with pytest.raises(S.ScaffoldError):
        S.render_text("<!-- if:text -->\nx\n<!-- endif -->", ctx)
    with pytest.raises(S.ScaffoldError):
        S.render_text("<!-- if:nokey -->\nx\n<!-- endif -->", ctx)
    with pytest.raises(S.ScaffoldError):
        S.render_text("<!-- if:yes -->\n<!-- if:yes -->\n<!-- endif -->\n<!-- endif -->", ctx)
    with pytest.raises(S.ScaffoldError):
        S.render_text("{{yes}}", ctx)


def test_symlinked_parent_is_refused(tmp_path):
    root = tmp_path / "proj"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".claude").symlink_to(outside)
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root))
    assert r.exit_code == 2
    assert not list(outside.iterdir())


def test_allow_prefix():
    assert S.allow_prefix("uv run pytest -q") == "uv run pytest"
    assert S.allow_prefix("npm test") == "npm test"
    assert S.allow_prefix("php artisan test --parallel") == "php artisan test"


def test_templates_are_packaged_as_package_data():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf8")
    assert 'packages = ["src/myvec"]' in pyproject  # hatch ships every file under the package, templates included
    assert (S.TEMPLATES / ".claude/agents/coder.md").is_file()
    assert (S.TEMPLATES / "docs/todo/group-index.md").is_file()


def test_generated_gitignore_makes_git_ignore_worktrees(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert run("--answers", str(write_answers(tmp_path)), "--root", str(root)).exit_code == 0
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    (root / ".worktrees/x").mkdir(parents=True)
    (root / ".worktrees/x/f.txt").write_text("x", encoding="utf8")
    out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=root, capture_output=True, text=True, env=env).stdout
    assert ".worktrees" not in out and "CLAUDE.md" in out


@pytest.mark.parametrize("bad", ['{"permissions": []}', '{"permissions": {"allow": "x"}}', '[]'])
def test_settings_with_unexpected_shape_is_a_conflict_not_a_traceback(tmp_path, bad):
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude/settings.json").write_text(bad, encoding="utf8")
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root), "--dry-run", "--json")
    assert r.exit_code == 3 and isinstance(r.exception, SystemExit)
    assert {"path": ".claude/settings.json", "action": "conflict"} in json.loads(r.stdout)


def test_overwrite_keeps_a_backup_of_the_replaced_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text("hand written\n", encoding="utf8")
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root), "--overwrite", "CLAUDE.md")
    assert r.exit_code == 0, r.output
    baks = list(root.glob("CLAUDE.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf8") == "hand written\n"


def test_docs_language_and_pitfalls_section_are_slots(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path)), "--root", str(root))
    assert r.exit_code == 0
    assert "Documents are in English" in (root / ".claude/agents/docs-writer.md").read_text(encoding="utf8")
    assert 'section "Pitfalls"' in (root / ".claude/agents/coder.md").read_text(encoding="utf8")


def test_bootstrap_skill_file_covers_the_contract():
    root = Path(__file__).parent.parent / "skill/myvector"
    text = (root / "bootstrap.md").read_text(encoding="utf8")
    for must in ("AskUserQuestion", "up to 4 questions", "--dry-run", "MarkerRewriteRefused", "git add -A",
                 "gh repo create", "--private", "chore: bootstrap process (myvec scaffold)"):
        assert must in text, must
    assert "bootstrap.md" in (root / "SKILL.md").read_text(encoding="utf8")


def _ticket_body(goal, ac, log):
    return (f"## {goal}\n\nShip it.\n\n## Context\n\nx\n\n## Acceptance criteria\n\n- [x] a\n- [ ] b\n\n"
            f"## {log}\n\n### 2026-11-10 coder\n\ndone\n")


def test_english_and_ukrainian_sections_give_the_same_parse(tmp_path):
    from myvec import tickets as T

    uk = T.body_sections(_ticket_body("За один захід", "Acceptance criteria", "Log"))
    en = T.body_sections(_ticket_body("In one pass", "Acceptance criteria", "Log"))
    assert uk["goal"] == en["goal"] == "Ship it."
    assert uk["ac"] == en["ac"] and len(en["ac"]) == 2 and uk["log"] == en["log"]


def test_ticket_with_english_sections_passes_check_and_has_progress_and_no_ac_health(tmp_path):
    from myvec import tickets as T

    root = tmp_path / "proj"
    root.mkdir()
    assert run("--answers", str(write_answers(tmp_path)), "--root", str(root)).exit_code == 0
    tpl = (root / "docs/todo/ticket-template.md").read_text(encoding="utf8")
    assert "## In one pass" in tpl and "## File ownership" in tpl and "## Live check" in tpl
    base = root / "docs/todo/E1-foundation"
    fmt = ("---\nid: {id}\ntitle: {id}\nkind: ticket\nstatus: in_progress\nparent: E1\nsize: S\ndeps: []\nblocks: []\n"
           "updated: 2026-11-10\n---\n\n# {id}\n\n")
    (base / "E1-01-en.md").write_text(fmt.format(id="E1-01") + _ticket_body("In one pass", "", "Log"), encoding="utf8")
    (base / "E1-02-uk.md").write_text(fmt.format(id="E1-02") + _ticket_body("За один захід", "", "Log"), encoding="utf8")
    (base / "E1-03-noac.md").write_text(fmt.format(id="E1-03") + "## In one pass\n\nNothing to check.\n", encoding="utf8")
    ts = T.load_dir(root, "docs/todo")
    assert not [i for i in T.check(ts) if i.severity == "error"]
    p = T.progress_all(ts)
    assert p["E1-01"] == p["E1-02"] == 50
    assert ts.tickets["E1-01"].ac_total == ts.tickets["E1-02"].ac_total == 2
    health = [i for i in T.health(list(ts.tickets.values()), __import__("datetime").date(2026, 11, 10)) if i.code == "no_ac"]
    assert [i.ticket_id for i in health] == ["E1-03"]


def test_new_project_is_english_end_to_end(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(write_answers(tmp_path, ui={"url": "http://localhost:3000", "check": "open the home page"})),
            "--root", str(root))
    assert r.exit_code == 0, r.output
    cyr = __import__("re").compile("[а-яіїєґА-ЯІЇЄҐ]")
    for rel in ("CLAUDE.md", "docs/todo/INDEX.md", "docs/todo/E1-foundation/index.md", "docs/todo/ticket-template.md",
                ".claude/rules/workflow.md", ".claude/agents/qa.md"):
        t = (root / rel).read_text(encoding="utf8")
        assert not cyr.search(t), rel
    assert "Live UI check: http://localhost:3000 - open" in (root / "CLAUDE.md").read_text(encoding="utf8")
    assert "None yet" in (root / "CLAUDE.md").read_text(encoding="utf8")
    assert "--agent orchestrator " in (root / ".claude/rules/workflow.md").read_text(encoding="utf8")
    # `ticket index` keeps the English headings for this project
    r2 = CliRunner().invoke(__import__("myvec.ticketcli", fromlist=["ticket_app"]).ticket_app, ["index", "--root", str(root)])
    assert r2.exit_code == 0, r2.output
    idx = (root / "docs/todo/INDEX.md").read_text(encoding="utf8")
    assert "Overall progress" in idx and "Загальний" not in idx
    assert "Progress:" in (root / "docs/todo/E1-foundation/index.md").read_text(encoding="utf8")


def test_existing_ukrainian_projects_keep_ukrainian_index(tmp_path):
    from myvec import ticketops as ops

    base = tmp_path / "docs/todo"
    base.mkdir(parents=True)
    assert ops.lang_for(base) == "uk"
    (base / "ticket-template.md").write_text("## За один захід\n", encoding="utf8")
    assert ops.lang_for(base) == "uk"


def test_bootstrap_forces_the_marker_into_the_first_commit():
    text = (Path(__file__).parent.parent / "skill/myvector/bootstrap.md").read_text(encoding="utf8")
    assert "git add -f .myvector.toml" in text
