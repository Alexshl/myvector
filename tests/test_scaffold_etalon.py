"""The process of this repo IS a render of the templates (ADR-021): change one without the other and this fails."""
from pathlib import Path

import pytest

from myvec import scaffold as S

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "fixtures" / "scaffold" / "myvec.answers.json"


@pytest.fixture(scope="module")
def rendered():
    return S.render(S.load_answers(FIX))


@pytest.mark.parametrize("rel", [f".claude/agents/{a}.md" for a in S.AGENTS]
                         + [".claude/rules/workflow.md", ".claude/rules/tickets.md", S.SEARCH_RULE])
def test_render_matches_the_repo_process_byte_for_byte(rendered, rel):
    assert rendered[rel] == (REPO / rel).read_text(encoding="utf8"), (
        f"{rel} differs from the render of src/myvec/scaffold/templates; update the template or the file "
        "(then regenerate with tests/fixtures/scaffold/myvec.answers.json)")


def test_every_repo_agent_has_a_template():
    on_disk = {p.name for p in (REPO / ".claude/agents").glob("*.md")}
    assert on_disk == {f"{a}.md" for a in S.AGENTS}


def test_claude_md_template_renders_the_answers(rendered):
    t = rendered["CLAUDE.md"]
    assert t.startswith("# MyVector (myvec)\n")
    for must in ("Python 3.12 (typer, SQLite, sqlite-vec, FTS5)", "`uv run pytest -q`", "http://127.0.0.1:7878",
                 "Machines: Mac, devbox", "## Pitfalls", "## Development process",
                 "`uv run python scripts/tickets.py check`"):
        assert must in t, must
    assert "{{" not in t


def test_search_rule_render_content(rendered):
    t = rendered[S.SEARCH_RULE]
    assert "{{" not in t
    for must in ("--kind docs", "--by-project", "sub-agent brief", "Graphify is not used", "grep", "rg", "find", "--expand", "--json", "not a terminal", "compact"):
        assert must in t, must
    for bad in ("/home/", "/Users/", "vector-local", "MyVector/vault"):
        assert bad not in t, bad
    assert t.isascii()


def test_process_version_is_5_and_matches_fixture():
    import json
    assert S.PROCESS_VERSION == 5
    assert json.loads((FIX.parent / "process_version.json").read_text())["version"] == S.PROCESS_VERSION


def test_every_repo_agent_mentions_myvec_search():
    for a in S.AGENTS:
        assert "myvec search" in (REPO / f".claude/agents/{a}.md").read_text(encoding="utf8"), a


@pytest.mark.parametrize("profile", ["myvec.answers.json", "minimal.answers.json"])
@pytest.mark.parametrize("role", ["planner", "reviewer"])
def test_planner_and_reviewer_start_with_search(profile, role):
    t = S.render(S.load_answers(FIX.parent / profile))[f".claude/agents/{role}.md"]
    for must in ("myvec search", "--kind docs", ".myvector.toml", "search: \"<query>\""):
        assert must in t, must
    body = t.split("---", 2)[2]
    assert body.index("myvec search") < body.index("Read/Grep"), "search must come first"


@pytest.mark.parametrize("agent", ["coder", "tester", "debugger", "qa", "docs-writer"])
def test_other_agents_do_not_get_the_search_log_line(rendered, agent):
    assert 'search: "<query>"' not in rendered[f".claude/agents/{agent}.md"]


def test_planner_search_step_is_first_in_what_to_do(rendered):
    body = rendered[".claude/agents/planner.md"].split("## What to do\n", 1)[1]
    assert body.startswith("1. If the project is indexed by MyVector")
