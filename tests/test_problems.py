"""Словник проблем (E3-04): покриття кодів, групування, API."""
import re
import subprocess
from pathlib import Path

import pytest

from conftest import do_sync, write
from myvec import daemon, problems, ticketdb

SRC = Path(__file__).parent.parent / "src" / "myvec"
LEGACY_EPIC = """---
id: E9-0{n}
title: Old {n}
status: todo
epic: E9
---
# Old
"""


def _codes_in_code() -> set[str]:
    found: set[str] = set()
    for f in ("tickets.py", "ticketmigrate.py", "ticketdb.py"):
        src = (SRC / f).read_text(encoding="utf-8")
        found |= set(re.findall(r'Issue\(\s*"(?:error|warning|health)",\s*"([a-z_]+)"', src))
        found |= set(re.findall(r'\badd\(\s*"(?:error|warning|health)",\s*"([a-z_]+)"', src))
        found |= set(re.findall(r'\badd\(\s*"([a-z_]+)",\s*(?:t\b|tid\b|T\[)', src))   # check(): add(code, t, msg)
        found |= set(re.findall(r'\badd\(\s*"([a-z_]+)",\s*M\[', src))           # health(): add(code, msg)
    return found


def test_dictionary_covers_every_code_in_the_code():
    found = _codes_in_code()
    assert {"legacy_epic", "dangling_dep", "asym_dep", "dep_cycle", "too_big", "stale", "legacy_group",
            "bad_tickets_dir", "symlink_skipped"} <= found      # the scanner itself works
    missing = found - problems.codes()
    assert not missing, f"codes without a dictionary entry: {sorted(missing)}"
    for code in problems.codes():
        d = problems.describe(code)
        assert d["severity"] in problems.SEVERITIES and d["title"] and d["explain"] and (d["fix"] or d["cmd"]), code


def test_unknown_code_falls_back_to_the_code():
    d = problems.describe("brand_new", "error")
    assert d["title"] == "brand_new" and d["severity"] == "error" and d["cmd"] is None
    assert problems.describe("brand_new", "nonsense")["severity"] == "warning"


def test_group_by_code_and_severity_order():
    iss = [{"severity": "health", "code": "too_big", "ticket_id": "A-1", "path": "a.md", "message": "m"},
           {"severity": "warning", "code": "legacy_epic", "ticket_id": "A-1", "path": "a.md", "message": "m"},
           {"severity": "warning", "code": "legacy_epic", "ticket_id": "A-2", "path": "b.md", "message": "m"},
           {"severity": "warning", "code": "bad_tickets_dir", "ticket_id": None, "path": ".myvector.toml", "message": "x"},
           {"severity": "error", "code": "dup_id", "ticket_id": "A-3", "path": "c.md", "message": "m"}]
    g = problems.group(iss)
    assert [x["code"] for x in g["groups"]] == ["dup_id", "legacy_epic", "bad_tickets_dir", "too_big"]
    assert g["counts"] == {"error": 1, "warning": 3, "health": 1} and g["total"] == 5
    epic = g["groups"][1]
    assert epic["n"] == 2 and epic["tickets"] == ["A-1", "A-2"] and epic["cmd"] == problems.MIGRATE_FIX
    assert g["groups"][2]["tickets"] == [] and g["groups"][2]["project_items"] == [{"path": ".myvector.toml", "message": "x"}]
    assert g["migration_branch"] is None
    assert "message" not in epic and "body" not in str(g)


def test_migration_branch_only_when_it_exists(tmp_path):
    iss = [{"severity": "warning", "code": "legacy_epic", "ticket_id": "A-1", "path": "a.md", "message": "m"}]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert problems.attach_branch(problems.group(iss, str(tmp_path)))["migration_branch"] is None
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": __import__("os").environ["PATH"]}
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "i"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "branch", problems.MIGRATION_BRANCH], check=True, env=env)
    assert (d := problems.attach_branch(problems.group(iss, str(tmp_path))))["migration_branch"] == problems.MIGRATION_BRANCH
    assert d["merge_cmd"].startswith("git -C ") and d["merge_cmd"].endswith(" merge " + problems.MIGRATION_BRANCH)
    other = [{"severity": "health", "code": "too_big", "ticket_id": "A-1", "path": "a.md", "message": "m"}]
    assert problems.attach_branch(problems.group(other, str(tmp_path)))["migration_branch"] is None   # only for legacy codes


def test_project_problems_and_api(ws):
    from starlette.testclient import TestClient
    write(ws / "docs/todo/E9-x/index.md", "---\nid: E9\ntitle: Nine\nkind: group\nstatus: in_progress\n---\n# E9\n")
    for n in (1, 2):
        write(ws / f"docs/todo/E9-x/E9-0{n}-old{n}.md", LEGACY_EPIC.format(n=n))
    _, db = do_sync(ws)
    d = ticketdb.project_problems(db, "acme")
    codes = {g["code"]: g for g in d["groups"]}
    assert codes["legacy_epic"]["tickets"] == ["E9-01", "E9-02"] and codes["legacy_epic"]["severity"] == "warning"
    assert d["project"] == "acme" and d["total"] == sum(d["counts"].values())
    assert ticketdb.project_problems(db, "nope") is None
    db.close()
    st = daemon.DaemonState(7878, "t")
    try:
        c = TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878")
        r = c.get("/api/tickets/acme/problems")
        assert r.status_code == 200 and {g["code"] for g in r.json()["groups"]} >= {"legacy_epic"}
        assert c.get("/api/tickets/nope/problems").status_code == 404
    finally:
        st.ticket_executor.shutdown(wait=True)
        st.executor.shutdown(wait=True)
        st.close()


def test_attach_branch_git_failure_or_timeout_gives_no_hint(tmp_path, monkeypatch):
    iss = [{"severity": "warning", "code": "legacy_epic", "ticket_id": "A-1", "path": "a.md", "message": "m"}]

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 2)
    monkeypatch.setattr(problems.subprocess, "run", boom)
    d = problems.attach_branch(problems.group(iss, str(tmp_path)))
    assert d["migration_branch"] is None and d["merge_cmd"] is None


def test_summary_column_equals_tab_count(ws):
    write(ws / "docs/todo/E9-x/index.md", "---\nid: E9\ntitle: Nine\nkind: group\nstatus: in_progress\n---\n# E9\n")
    for n in (1, 2):
        write(ws / f"docs/todo/E9-x/E9-0{n}-old{n}.md", LEGACY_EPIC.format(n=n))
    _, db = do_sync(ws)
    s = next(p for p in ticketdb.projects_summary(db, include_empty=True) if p["project_path"] == "acme")
    d = ticketdb.project_problems(db, "acme")
    assert s["n_issues"] + s["n_health"] == d["total"] > 0


# ------------------------------------------------------------------ process_outdated (E0-10, ADR-036)
def _state(root, version=1, fmt=1):
    p = Path(root) / ".myvec" / "scaffold.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(__import__("json").dumps({"format": fmt, "version": version, "answers": {}, "files": {}}))
    return p


def test_process_issues_outdated_gives_one_issue(tmp_path):
    from myvec.scaffold import PROCESS_VERSION
    _state(tmp_path, PROCESS_VERSION - 1)
    (i,) = problems.process_issues(str(tmp_path))
    assert i["code"] == "process_outdated" and i["severity"] == "warning" and i["ticket_id"] is None
    assert i["path"] == ".myvec/scaffold.json"
    assert i["message"] == f"процес застарів v{PROCESS_VERSION - 1} → v{PROCESS_VERSION}"


def test_process_issues_no_problem_for_current_none_newer_invalid_and_no_root(tmp_path):
    from myvec.scaffold import PROCESS_VERSION
    assert problems.process_issues(None) == [] and problems.process_issues("") == []
    assert problems.process_issues(str(tmp_path)) == []                       # none
    _state(tmp_path, PROCESS_VERSION)
    assert problems.process_issues(str(tmp_path)) == []                       # current
    _state(tmp_path, PROCESS_VERSION + 1)
    assert problems.process_issues(str(tmp_path)) == []                       # newer
    _state(tmp_path, PROCESS_VERSION - 1, fmt=99)
    assert problems.process_issues(str(tmp_path)) == []                       # newer format
    (tmp_path / ".myvec" / "scaffold.json").write_text("{broken")
    assert problems.process_issues(str(tmp_path)) == []                       # invalid
    (tmp_path / ".myvec" / "scaffold.json").write_text('{"format":1,"version":true}')
    assert problems.process_issues(str(tmp_path)) == []


def test_process_issues_symlinked_dir_is_no_problem_and_does_not_raise(tmp_path):
    from myvec.scaffold import PROCESS_VERSION
    real = tmp_path / "real"
    real.mkdir()
    _state(real, PROCESS_VERSION - 1)
    root = tmp_path / "root"
    root.mkdir()
    (root / ".myvec").symlink_to(real / ".myvec")
    assert problems.process_issues(str(root)) == []


def test_process_issues_cache_follows_file_and_version(tmp_path, monkeypatch):
    import myvec.scaffold as scaffold
    from myvec.scaffold import PROCESS_VERSION
    p = _state(tmp_path, PROCESS_VERSION - 1)
    assert len(problems.process_issues(str(tmp_path))) == 1
    got = problems.process_issues(str(tmp_path))
    got[0]["message"] = "mutated"                                              # copies, not the cache itself
    assert problems.process_issues(str(tmp_path))[0]["message"] != "mutated"
    p.write_text(p.read_text().replace(f'"version": {PROCESS_VERSION - 1}', f'"version": {PROCESS_VERSION}'))
    assert problems.process_issues(str(tmp_path)) == []                        # size/mtime changed
    monkeypatch.setattr(scaffold, "PROCESS_VERSION", PROCESS_VERSION + 1)      # templates got newer
    assert len(problems.process_issues(str(tmp_path))) == 1
    monkeypatch.setattr(scaffold, "PROCESS_VERSION", PROCESS_VERSION)
    assert problems.process_issues(str(tmp_path)) == []


def test_process_issues_state_read_exception_gives_empty(tmp_path, monkeypatch):
    from myvec.scaffold import PROCESS_VERSION, state
    _state(tmp_path, PROCESS_VERSION - 1)

    def boom(root):
        raise RuntimeError("x")
    monkeypatch.setattr(state, "read", boom)
    assert problems.process_issues(str(tmp_path)) == []


def test_process_cache_is_bounded(tmp_path):
    from myvec.scaffold import PROCESS_VERSION
    for n in range(problems._PROCESS_CACHE_MAX + 5):
        d = tmp_path / f"p{n}"
        d.mkdir()
        _state(d, PROCESS_VERSION - 1)
        problems.process_issues(str(d))
    assert len(problems._PROCESS_CACHE) <= problems._PROCESS_CACHE_MAX


def test_process_outdated_is_described_with_cmd_and_not_legacy():
    d = problems.describe("process_outdated")
    assert d["severity"] == "warning" and d["cmd"] == problems.UPDATE_CMD and not d["legacy"]
    assert "process_outdated" not in problems.LEGACY_CODES
    assert problems.UPDATE_PHRASE in d["fix"] and problems.UPDATE_WRITE in d["fix"]


def test_project_problems_includes_process_outdated_and_api(ws):
    from starlette.testclient import TestClient
    from myvec.scaffold import PROCESS_VERSION
    write(ws / "docs/todo/E9-x/index.md", "---\nid: E9\ntitle: Nine\nkind: group\nstatus: in_progress\n---\n# E9\n")
    write(ws / "docs/todo/E9-x/E9-01-a.md", LEGACY_EPIC.format(n=1))
    _state(ws, PROCESS_VERSION - 1)
    _, db = do_sync(ws)
    d = ticketdb.project_problems(db, "acme")
    g = next(x for x in d["groups"] if x["code"] == "process_outdated")
    assert g["n"] == 1 and g["project_items"][0]["message"].startswith("процес застарів v")
    assert d["total"] == sum(d["counts"].values()) and d["counts"]["warning"] >= 2
    s = next(p for p in ticketdb.projects_summary(db, include_empty=True) if p["project_path"] == "acme")
    assert s["n_process"] == 1 and s["n_issues"] + s["n_health"] + s["n_process"] == d["total"]
    db.close()
    st = daemon.DaemonState(7878, "t")
    try:
        c = TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878")
        r = c.get("/api/tickets/acme/problems")
        assert r.status_code == 200 and "process_outdated" in {x["code"] for x in r.json()["groups"]}
    finally:
        st.ticket_executor.shutdown(wait=True)
        st.executor.shutdown(wait=True)
        st.close()


def test_project_without_state_has_no_process_group_and_zero_n_process(ws):
    write(ws / "docs/todo/E9-x/index.md", "---\nid: E9\ntitle: Nine\nkind: group\nstatus: in_progress\n---\n# E9\n")
    write(ws / "docs/todo/E9-x/E9-01-a.md", LEGACY_EPIC.format(n=1))
    _, db = do_sync(ws)
    assert "process_outdated" not in {g["code"] for g in ticketdb.project_problems(db, "acme")["groups"]}
    s = next(p for p in ticketdb.projects_summary(db, include_empty=True) if p["project_path"] == "acme")
    assert s["n_process"] == 0
    db.close()


# ------------------------------------------------------------------ search rule codes (E8-12, ADR-041)
def _agents(root):
    (Path(root) / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    (Path(root) / ".claude" / "agents" / "coder.md").write_text("# coder\n")


def _codes(root):
    return sorted(i["code"] for i in problems.process_issues(str(root)))


def test_search_rule_constant_equals_scaffold_constant():
    from myvec import scaffold
    assert problems.SEARCH_RULE == scaffold.SEARCH_RULE


def test_new_codes_are_in_the_dictionary():
    for c in ("search_rule_missing", "foreign_search_hook", "graphify_leftover"):
        assert c in problems.codes()
    assert problems._P["search_rule_missing"][4] == problems.ADOPT_CMD


def test_search_rule_missing_appears_and_disappears(tmp_path):
    _agents(tmp_path)
    (i,) = problems.process_issues(str(tmp_path))
    assert i["code"] == "search_rule_missing" and i["severity"] == "warning" and i["path"] == ".claude"
    (tmp_path / ".claude" / "rules").mkdir()
    (tmp_path / ".claude" / "rules" / "search.md").write_text("# s\n")
    assert _codes(tmp_path) == []


def test_search_rule_missing_not_when_claude_md_mentions_myvec_search(tmp_path):
    _agents(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("use `myvec search` first\n")
    assert _codes(tmp_path) == []
    (tmp_path / "CLAUDE.md").write_text("nothing relevant\n")
    assert _codes(tmp_path) == ["search_rule_missing"]
    (tmp_path / ".claude" / "CLAUDE.md").write_text("myvec search\n")
    assert _codes(tmp_path) == []


def test_search_rule_missing_not_without_agents_or_with_scaffold_state(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert _codes(tmp_path) == []
    _agents(tmp_path)
    from myvec.scaffold import PROCESS_VERSION
    _state(tmp_path, PROCESS_VERSION)
    assert _codes(tmp_path) == []


def test_search_rule_missing_in_nested_git_repo_first_level_only(tmp_path):
    sub = tmp_path / "repo-a"
    (sub / ".git").mkdir(parents=True)
    _agents(sub)
    deep = tmp_path / "x" / "repo-b"
    (deep / ".git").mkdir(parents=True)
    _agents(deep)
    (i,) = problems.process_issues(str(tmp_path))
    assert i["code"] == "search_rule_missing" and i["path"] == "repo-a/.claude"


def test_nested_dir_without_git_is_not_checked(tmp_path):
    _agents(tmp_path / "plain")
    assert _codes(tmp_path) == []


def _settings(root, hooks_cmd, name="settings.json"):
    p = Path(root) / ".claude" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(__import__("json").dumps({"hooks": {"PreToolUse": [{"matcher": "Glob", "hooks": [{"type": "command", "command": hooks_cmd}]}]}}))
    return p


def test_foreign_search_hook_appears_and_disappears(tmp_path):
    p = _settings(tmp_path, "[ -f graphify-out/graph.json ] && echo hint")
    (i,) = problems.process_issues(str(tmp_path))
    assert i["code"] == "foreign_search_hook" and i["path"] == ".claude/settings.json"
    p.write_text('{"hooks": {"PreToolUse": [{"hooks": [{"command": "myvec hook pre-search"}]}]}}')
    assert _codes(tmp_path) == []


def test_foreign_search_hook_in_settings_local(tmp_path):
    _settings(tmp_path, "GRAPHIFY run", name="settings.local.json")
    (i,) = problems.process_issues(str(tmp_path))
    assert i["path"] == ".claude/settings.local.json"


def test_foreign_search_hook_only_for_pretooluse(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text('{"hooks": {"PostToolUse": [{"hooks": [{"command": "graphify x"}]}]}}')
    assert _codes(tmp_path) == []


@pytest.mark.parametrize("text", ["{broken", "[]", "null", '{"hooks": 5}', '{"hooks": {"PreToolUse": "x"}}',
                                  '{"hooks": {"PreToolUse": [1, {"hooks": 3}, {"hooks": [7, {"command": 5}]}]}}',
                                  "[" * 100000])
def test_broken_settings_json_does_not_fail(tmp_path, text):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text(text)
    assert problems.process_issues(str(tmp_path)) == []


def test_huge_settings_json_is_skipped(tmp_path):
    p = _settings(tmp_path, "graphify")
    p.write_text(p.read_text() + " " * (problems._ADOPT_MAX_BYTES + 10))
    assert _codes(tmp_path) == []


def test_huge_claude_md_counts_as_mentioning_search(tmp_path):
    _agents(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x" * (problems._ADOPT_MAX_BYTES + 10))
    assert _codes(tmp_path) == []


def test_symlinked_settings_is_not_read(tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"hooks": {"PreToolUse": [{"hooks": [{"command": "graphify"}]}]}}')
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").symlink_to(real)
    assert _codes(tmp_path) == []


def test_graphify_leftover_depends_on_cli_presence(tmp_path, monkeypatch):
    (tmp_path / "graphify-out").mkdir()
    monkeypatch.setattr(problems.shutil, "which", lambda n: None)
    (i,) = problems.process_issues(str(tmp_path))
    assert i["code"] == "graphify_leftover" and i["severity"] == "health" and i["path"] == "graphify-out"
    monkeypatch.setattr(problems.shutil, "which", lambda n: "/usr/bin/graphify")
    assert _codes(tmp_path) == []                                              # cache key includes the CLI presence


def test_graphify_leftover_not_for_file_or_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(problems.shutil, "which", lambda n: None)
    (tmp_path / "graphify-out").write_text("x")
    assert _codes(tmp_path) == []


def test_adoption_issues_are_cached_and_follow_files(tmp_path, monkeypatch):
    _agents(tmp_path)
    calls = []
    real = problems._adoption_for
    monkeypatch.setattr(problems, "_adoption_for", lambda *a: calls.append(a) or real(*a))
    problems.process_issues(str(tmp_path))
    problems.process_issues(str(tmp_path))
    assert len(calls) == 1
    got = problems.process_issues(str(tmp_path))
    got[0]["message"] = "mutated"
    assert problems.process_issues(str(tmp_path))[0]["message"] != "mutated"
    (tmp_path / "CLAUDE.md").write_text("myvec search\n")
    assert _codes(tmp_path) == []
    monkeypatch.setattr(problems, "_adoption_for", real)


def test_adoption_cache_is_bounded(tmp_path):
    for n in range(problems._PROCESS_CACHE_MAX + 5):
        d = tmp_path / f"p{n}"
        d.mkdir()
        problems.process_issues(str(d))
    assert len(problems._ADOPT_CACHE) <= problems._PROCESS_CACHE_MAX


def test_missing_root_gives_empty(tmp_path):
    assert problems.process_issues(str(tmp_path / "nope")) == []


def test_adoption_failure_does_not_hide_state_issue(tmp_path, monkeypatch):
    from myvec.scaffold import PROCESS_VERSION
    _state(tmp_path, PROCESS_VERSION - 1)
    monkeypatch.setattr(problems, "_adoption_issues", lambda r: 1 / 0)
    assert _codes(tmp_path) == ["process_outdated"]


def test_fast_path_without_claude_does_not_import_scaffold(tmp_path):
    code = ("import sys; from myvec import problems; "
            f"r = problems.process_issues({str(tmp_path)!r}); "
            "assert r == [], r; assert 'myvec.scaffold' not in sys.modules")
    out = subprocess.run([__import__("sys").executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "MYVEC_HOME": str(tmp_path / "home")})
    assert out.returncode == 0, out.stderr


def test_adoption_with_agents_does_not_import_scaffold(tmp_path):
    _agents(tmp_path)
    code = ("import sys; from myvec import problems; "
            f"r = problems.process_issues({str(tmp_path)!r}); "
            "assert [i['code'] for i in r] == ['search_rule_missing'], r; assert 'myvec.scaffold' not in sys.modules")
    out = subprocess.run([__import__("sys").executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "MYVEC_HOME": str(tmp_path / "home")})
    assert out.returncode == 0, out.stderr


# ------------------------------------------------------------------ search_rule_outdated (E8-17, ADR-046)
_V3 = Path(__file__).parent / "fixtures" / "scaffold" / "search_rule" / "v3.md"


def _rule(root, data: bytes):
    (Path(root) / ".claude" / "rules").mkdir(parents=True, exist_ok=True)
    (Path(root) / ".claude" / "rules" / "search.md").write_bytes(data)


def test_search_rule_outdated_in_dictionary_and_imports_no_scaffold():
    assert "search_rule_outdated" in problems.codes()
    assert problems._P["search_rule_outdated"][4] == problems.ADOPT_CMD
    src = Path(problems.__file__).read_text(encoding="utf-8")
    assert "scaffold" not in src.split("def _adoption_for")[1].split("def _adoption_issues")[0]


def test_search_rule_outdated_appears_and_disappears(tmp_path):
    _agents(tmp_path)
    _rule(tmp_path, _V3.read_bytes())
    (i,) = problems.process_issues(str(tmp_path))
    assert i["code"] == "search_rule_outdated" and i["severity"] == "warning" and i["path"] == ".claude/rules/search.md"
    from myvec import scaffold
    _rule(tmp_path, scaffold._read_template(scaffold.SEARCH_RULE).encode("utf8"))   # updated
    assert _codes(tmp_path) == []


@pytest.mark.parametrize("edit", [lambda b: b + b"\n", lambda b: b.replace(b"\n", b"\r\n"), lambda b: b[:-2] + b"x\n"])
def test_search_rule_outdated_not_for_an_edited_file(tmp_path, edit):
    _agents(tmp_path)
    _rule(tmp_path, edit(_V3.read_bytes()))
    assert _codes(tmp_path) == []


def test_search_rule_outdated_not_with_scaffold_state(tmp_path):
    _agents(tmp_path)
    _rule(tmp_path, _V3.read_bytes())
    (tmp_path / ".myvec").mkdir()
    (tmp_path / ".myvec" / "scaffold.json").write_text("{}")
    assert "search_rule_outdated" not in _codes(tmp_path)


def test_search_rule_outdated_not_for_a_symlink(tmp_path):
    _agents(tmp_path)
    (tmp_path / "real.md").write_bytes(_V3.read_bytes())
    (tmp_path / ".claude" / "rules").mkdir()
    (tmp_path / ".claude" / "rules" / "search.md").symlink_to(tmp_path / "real.md")
    assert _codes(tmp_path) == []


def test_search_rule_outdated_cache_follows_a_content_change(tmp_path):
    _agents(tmp_path)
    _rule(tmp_path, _V3.read_bytes())
    assert _codes(tmp_path) == ["search_rule_outdated"]
    _rule(tmp_path, _V3.read_bytes() + b"x")          # size changes -> signature changes
    assert _codes(tmp_path) == []
    _rule(tmp_path, _V3.read_bytes())
    assert _codes(tmp_path) == ["search_rule_outdated"]


def test_search_rule_outdated_oversize_file_is_ignored(tmp_path):
    _agents(tmp_path)
    _rule(tmp_path, b"x" * (problems._ADOPT_MAX_BYTES + 10))
    assert _codes(tmp_path) == []


def test_search_rule_outdated_not_when_claude_or_rules_is_a_symlink(tmp_path):
    real = tmp_path / "real"
    (real / "rules").mkdir(parents=True)
    (real / "rules" / "search.md").write_bytes(_V3.read_bytes())
    root = tmp_path / "p"
    root.mkdir()
    (root / ".claude").symlink_to(real)
    assert "search_rule_outdated" not in _codes(root)
    root2 = tmp_path / "q"
    (root2 / ".claude").mkdir(parents=True)
    (root2 / ".claude" / "rules").symlink_to(real / "rules")
    assert "search_rule_outdated" not in _codes(root2)
