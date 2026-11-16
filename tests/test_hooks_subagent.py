"""SubagentStart hook: a fixed search hint for subagents (E8-01)."""
import io
import json
import subprocess
import sys

import pytest

from myvec import hooks


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".myvector.toml").write_text('name = "acme"\n')
    return root


def run(monkeypatch, capsys, payload, raw=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw if raw is not None else json.dumps(payload)))
    assert hooks.main(["subagent-start"]) == 0
    return capsys.readouterr().out


def test_marker_dir_gives_valid_json_with_hint(monkeypatch, capsys, proj):
    out = run(monkeypatch, capsys, {"hook_event_name": "SubagentStart", "cwd": str(proj)})
    d = json.loads(out)
    assert d["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert d["hookSpecificOutput"]["additionalContext"] == hooks.SEARCH_HINT


def test_nested_dir_under_marker_gives_hint(monkeypatch, capsys, proj):
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    out = run(monkeypatch, capsys, {"cwd": str(sub)})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == hooks.SEARCH_HINT


def test_no_marker_is_silent(monkeypatch, capsys, tmp_path):
    assert run(monkeypatch, capsys, {"cwd": str(tmp_path)}) == ""


def test_missing_cwd_falls_back_to_process_cwd(monkeypatch, capsys, proj):
    monkeypatch.chdir(proj)
    assert json.loads(run(monkeypatch, capsys, {}))["hookSpecificOutput"]["additionalContext"] == hooks.SEARCH_HINT


@pytest.mark.parametrize("raw", ["{not json", "", "[]", "null", '{"cwd": 5}'])
def test_broken_stdin_exit_0_no_crash(monkeypatch, capsys, tmp_path, raw):
    monkeypatch.chdir(tmp_path)
    assert run(monkeypatch, capsys, None, raw=raw) == ""


def test_exception_inside_hook_is_swallowed(monkeypatch, capsys):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(hooks, "subagent_start", boom)
    assert hooks.main(["subagent-start"]) == 0
    assert capsys.readouterr().out == ""


def test_hint_is_short_ascii_and_private_free():
    h = hooks.SEARCH_HINT
    assert h.isascii()
    for bad in ("/home", "/Users", "MyVector"):
        assert bad not in h


def test_hint_length_limit():
    assert len(hooks.SEARCH_HINT) <= 300
    assert hooks.SEARCH_HINT.isascii()


def test_hint_mentions_kind_docs():
    assert "--kind docs" in hooks.SEARCH_HINT


# ---------------------------------------------------------------- install()

def _cmds(data, event):
    return [h["command"] for g in data["hooks"].get(event, []) for h in g["hooks"]]


def test_install_adds_subagent_start_once_and_is_idempotent(tmp_path):
    p = tmp_path / "settings.json"
    changed, _ = hooks.install(p, "/bin/myvec")
    assert changed
    data = json.loads(p.read_text())
    assert _cmds(data, "SubagentStart") == ["/bin/myvec hook subagent-start"]
    assert data["hooks"]["SubagentStart"][0]["hooks"][0]["timeout"] == 5
    assert _cmds(data, "SessionStart") == ["/bin/myvec hook session-start"]
    assert hooks.install(p, "/bin/myvec") == (False, None)
    assert _cmds(json.loads(p.read_text()), "SubagentStart") == ["/bin/myvec hook subagent-start"]


def test_install_rewrites_stale_subagent_path_without_duplicates(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SubagentStart": [
        {"hooks": [{"type": "command", "command": "/Users/old/.local/bin/myvec hook subagent-start"}]}]}}))
    hooks.install(p, "/bin/myvec")
    data = json.loads(p.read_text())
    assert _cmds(data, "SubagentStart") == ["/bin/myvec hook subagent-start"]


def test_install_keeps_foreign_hooks(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SubagentStart": [
        {"hooks": [{"type": "command", "command": "/opt/other/tool run"}]}]}}))
    hooks.install(p, "/bin/myvec")
    cmds = _cmds(json.loads(p.read_text()), "SubagentStart")
    assert "/opt/other/tool run" in cmds and "/bin/myvec hook subagent-start" in cmds


def test_install_stale_session_start_not_confused_with_subagent_start(tmp_path):
    """Marks 'hook session-start' and 'hook subagent-start' must not match each other."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "/Users/old/bin/myvec hook session-start"}]}],
        "SubagentStart": [{"hooks": [{"type": "command", "command": "/Users/old/bin/myvec hook subagent-start"}]}],
    }}))
    hooks.install(p, "/bin/myvec")
    data = json.loads(p.read_text())
    assert _cmds(data, "SessionStart") == ["/bin/myvec hook session-start"]
    assert _cmds(data, "SubagentStart") == ["/bin/myvec hook subagent-start"]


def test_install_stale_subagent_mark_under_wrong_event_is_removed(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "/Users/old/bin/myvec hook subagent-start"}]}]}}))
    hooks.install(p, "/bin/myvec")
    data = json.loads(p.read_text())
    assert "/Users/old/bin/myvec hook subagent-start" not in _cmds(data, "SessionStart")
    assert _cmds(data, "SessionStart") == ["/bin/myvec hook session-start"]


# ---------------------------------------------------------------- speed and imports

def test_subagent_start_is_fast_and_light(proj):
    code = (
        "import io,sys,json,time\n"
        f"sys.stdin=io.StringIO(json.dumps({{'cwd': {str(proj)!r}}}))\n"
        "from myvec import hooks\n"
        "t=time.perf_counter(); rc=hooks.main(['subagent-start']); dt=time.perf_counter()-t\n"
        "bad=[m for m in ('myvec.context','sqlite_vec','httpx','typer') if m in sys.modules]\n"
        "sys.stderr.write(json.dumps({'rc':rc,'dt':dt,'bad':bad}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    res = json.loads(r.stderr.strip().splitlines()[-1])
    assert res["rc"] == 0 and res["bad"] == []
    assert res["dt"] < 0.1
    assert json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] == hooks.SEARCH_HINT
