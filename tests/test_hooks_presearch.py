"""PreToolUse pre-search hint (E8-11, ADR-040): classifier, throttling, silence, speed."""
import io
import json
import os
import subprocess
import sys

import pytest

from myvec import hooks, presearch


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".myvector.toml").write_text('name = "acme"\n')
    (root / "a.py").write_text("x\n")
    return root


def call(monkeypatch, capsys, proj, cmd=None, tool="Bash", sid="s1", agent=None, now=1e6, **ti):
    ti = ti or {"command": cmd}
    payload = {"tool_name": tool, "tool_input": ti, "cwd": str(proj), "session_id": sid}
    if agent:
        payload["agent_id"] = agent
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks.pre_search(now=now) == 0
    return capsys.readouterr().out


HINT = ['grep -rn "a\\|b" src/', 'rg "foo|bar"', 'rg "two words"', "grep -rn foo .", "rg foo src",
        'find . | xargs grep -l "a|b"', 'timeout 5 rg "a|b"', 'cd x && rg "a|b"', "rg -e one -e two 2>/dev/null"]
QUIET = ["grep -n foo a.py src/b.py", "rg foo /tmp", "find . -name x", 'rg "a|b', "rg -c foo", "grep -rn foo . | wc -l",
         "rg foo a.py", "cat x | rg foo", "rg foo logs", "ls -la", "myvec search foo"]


@pytest.mark.parametrize("cmd", HINT)
def test_hint_on_tree_search(proj, cmd):
    assert presearch.exploratory("Bash", {"command": cmd}, str(proj), str(proj)) is not None


@pytest.mark.parametrize("cmd", QUIET)
def test_quiet_on_other(proj, cmd):
    assert presearch.exploratory("Bash", {"command": cmd}, str(proj), str(proj)) is None


def test_grep_tool(proj):
    f = presearch.exploratory
    assert f("Grep", {"pattern": "a|b", "path": str(proj / "src")}, str(proj), str(proj)) == ["a", "b"]
    assert f("Grep", {"pattern": "a|b", "path": str(proj / "a.py")}, str(proj), str(proj)) is None
    assert f("Glob", {"pattern": "a|b"}, str(proj), str(proj)) is None


def test_output_is_json_context_only(monkeypatch, capsys, proj):
    d = json.loads(call(monkeypatch, capsys, proj, 'rg "foo|bar" src'))
    h = d["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and "permissionDecision" not in h and "updatedInput" not in d
    assert 'myvec search "foo bar"' in h["additionalContext"]
    assert h["additionalContext"].isascii() and len(h["additionalContext"]) <= 300


def test_words_sanitised():
    ws = presearch.words(["$(rm -rf)", "`id`", "ok_word", "sk-" + "a" * 50, "two words", "ok_word"])
    assert ws == ["ok_word", "two", "words"]
    assert presearch.hint([]).count("<question>") == 1


def test_throttle_and_search_quiet(monkeypatch, capsys, proj):
    c = 'rg "a|b" src'
    assert call(monkeypatch, capsys, proj, c, now=1e6)
    assert call(monkeypatch, capsys, proj, c, now=1e6 + presearch.HINT_EVERY_S - 1) == ""
    assert call(monkeypatch, capsys, proj, c, now=1e6 + presearch.HINT_EVERY_S + 1)
    assert call(monkeypatch, capsys, proj, "myvec search x", now=2e6) == ""
    assert call(monkeypatch, capsys, proj, c, now=2e6 + presearch.QUIET_AFTER_SEARCH_S - 1) == ""
    assert call(monkeypatch, capsys, proj, c, agent="sub1", now=2e6 + 1)   # own counter
    assert call(monkeypatch, capsys, proj, c, now=2e6 + presearch.QUIET_AFTER_SEARCH_S + 1)


def test_session_end_drops_state(monkeypatch, capsys, proj):
    from myvec import config
    call(monkeypatch, capsys, proj, 'rg "a|b" src')
    d = config.home() / "state" / "presearch"
    assert list(d.glob("s1-*"))
    hooks._drop_presearch_state({"session_id": "s1"})
    assert not list(d.glob("s1-*"))


def test_silent_outside_project_and_on_garbage(monkeypatch, capsys, tmp_path, proj):
    assert call(monkeypatch, capsys, tmp_path, 'rg "a|b" src') == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("{bad"))
    assert hooks.main(["pre-search"]) == 0 and capsys.readouterr().out == ""


def test_error_is_logged_without_command_text(monkeypatch, capsys, proj):
    from myvec import config
    def boom(*a, **k):
        raise RuntimeError("SENTINEL-CMD-TEXT")
    monkeypatch.setattr(presearch, "exploratory", boom)
    assert call(monkeypatch, capsys, proj, 'rg "SENTINEL-CMD-TEXT|b" src') == ""
    log = (config.logs_dir() / "hook.log").read_text()
    assert "RuntimeError" in log and "SENTINEL" not in log


def test_install_adds_group_once(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}]}}))
    assert hooks.install(s, "/x/myvec")[0]
    assert not hooks.install(s, "/x/myvec")[0]
    pre = json.loads(s.read_text())["hooks"]["PreToolUse"]
    mine = [g for g in pre if any("hook pre-search" in h["command"] for h in g["hooks"])]
    assert len(mine) == 1 and mine[0]["matcher"] == "Bash|Grep"
    assert any(g["matcher"] == "AskUserQuestion" for g in pre)
    assert any(h["command"] == "other" for g in pre for h in g["hooks"])
    hooks.install(s, "/y/myvec")
    pre = json.loads(s.read_text())["hooks"]["PreToolUse"]
    assert sum("hook pre-search" in h["command"] for g in pre for h in g["hooks"]) == 1


def _timed(proj, tool_input, extra_bad):
    code = (
        "import io,sys,json,time\n"
        f"sys.stdin=io.StringIO(json.dumps({{'tool_name':'Bash','tool_input':{tool_input!r},'cwd':{str(proj)!r},'session_id':'sp'}}))\n"
        "from myvec import hooks\n"
        "t=time.perf_counter(); rc=hooks.main(['pre-search']); dt=time.perf_counter()-t\n"
        f"bad=[m for m in {extra_bad!r} if m in sys.modules]\n"
        "sys.stderr.write(json.dumps({'rc':rc,'dt':dt,'bad':bad}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=dict(os.environ))
    return json.loads(r.stderr.strip().splitlines()[-1]), r.stdout


def test_fast_paths(proj):
    heavy = ["sqlite_vec", "httpx", "typer", "myvec.context", "myvec.tokens"]
    res, out = _timed(proj, {"command": "ls -la"}, heavy + ["myvec.config", "tomllib"])
    assert res["rc"] == 0 and res["bad"] == [] and res["dt"] < 0.03 and out == ""
    res, out = _timed(proj, {"command": 'rg "a|b" src'}, heavy)
    assert res["rc"] == 0 and res["bad"] == [] and res["dt"] < 0.1
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_prune_removes_old_state_despite_fresh_dir_mtime(monkeypatch, capsys, proj):
    """Regression: writing state bumps the dir mtime, so the prune gate must not depend on it."""
    import time
    from myvec import config
    d = config.home() / "state" / "presearch"
    d.mkdir(parents=True)
    old = d / "old-main"
    old.write_text("searched=1\nhinted=1\n")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    out = call(monkeypatch, capsys, proj, 'rg "a|b" src', now=time.time())
    assert out
    assert not old.exists()
    assert list(d.glob("s1-*"))


@pytest.mark.parametrize("cmd", ["rg -p foo src", "ag -S foo src"])
def test_flags_without_value_keep_pattern(monkeypatch, capsys, proj, cmd):
    out = call(monkeypatch, capsys, proj, cmd)
    assert 'myvec search \\"src\\"' not in out
