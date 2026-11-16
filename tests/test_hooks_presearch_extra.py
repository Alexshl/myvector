"""E8-11 extra coverage: counting, log/tmp trees, garbage payloads, state without command text."""
import io
import json
import sys

import pytest

from myvec import config, hooks, presearch


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    for d in ("src", "logs", "tmp", "src/tmp"):
        (root / d).mkdir(parents=True)
    (root / ".myvector.toml").write_text('name = "acme"\n')
    (root / "a.py").write_text("x\n")
    return root


def run(monkeypatch, capsys, payload, now=1e6):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert hooks.pre_search(now=now) == 0
    return capsys.readouterr().out


def words(proj, cmd):
    return presearch.exploratory("Bash", {"command": cmd}, str(proj), str(proj))


@pytest.mark.parametrize("cmd", ["grep -rc foo .", "grep -rn foo . | wc -l", "rg --count foo src",
                                 "rg --count-matches foo", "grep -r foo . | wc -l"])
def test_counting_is_silent(proj, cmd):
    assert words(proj, cmd) is None


@pytest.mark.parametrize("cmd", ["rg foo logs", "grep -rn foo tmp", "rg foo /tmp", "rg foo src/tmp",
                                 "grep -rn foo logs/ tmp/"])
def test_log_and_tmp_trees_are_silent(proj, cmd):
    assert words(proj, cmd) is None


def test_single_identifier_hints(proj):
    assert words(proj, "rg build_index src") == ["build_index"]
    assert words(proj, "grep -rn MYVEC_HOME .") == ["MYVEC_HOME"]


def test_specific_files_are_silent(proj):
    assert words(proj, "grep -n foo a.py") is None
    assert words(proj, "rg foo a.py src/missing.py") is None


@pytest.mark.parametrize("payload", ["", "null", "[]", "42", '{"tool_name": "Bash"}',
                                     '{"tool_name": "Bash", "tool_input": "rg x"}',
                                     '{"tool_name": "Bash", "tool_input": {"command": 5}}',
                                     '{"tool_name": "Grep", "tool_input": {"pattern": null}}',
                                     '{"tool_name": 7, "tool_input": {}}', "\xff\x00 not json"])
def test_garbage_input_exit_0_and_silent(monkeypatch, capsys, payload):
    assert run(monkeypatch, capsys, payload) == ""


def test_bad_session_id_does_not_write_state(monkeypatch, capsys, proj):
    out = run(monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": "rg foo src"},
                                    "cwd": str(proj), "session_id": "../../etc/x"})
    assert out == ""
    assert not (config.home() / "state" / "presearch").exists() or not list((config.home() / "state" / "presearch").iterdir())


def test_state_files_hold_no_command_text(monkeypatch, capsys, proj):
    run(monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": 'rg "SECRETWORD|other" src'},
                              "cwd": str(proj), "session_id": "sx"})
    d = config.home() / "state" / "presearch"
    files = list(d.iterdir())
    assert files
    assert all("SECRETWORD" not in f.read_text() for f in files)
    for p in config.logs_dir().glob("*"):
        if p.is_file():
            assert "SECRETWORD" not in p.read_text(errors="ignore")
