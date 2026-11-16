"""SessionStart: рядок «процес застарів» (E0-10, ADR-036)."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from myvec import hooks, problems
from myvec.scaffold import PROCESS_VERSION, state

SRC = str(Path(__file__).parent.parent / "src")


@pytest.fixture(autouse=True)
def no_sync(monkeypatch):
    # the full session_start reaches spawn_sync: no detached `myvec sync` from tests
    monkeypatch.setattr(hooks, "spawn_sync", lambda cwd: None)


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".myvector.toml").write_text('name = "acme"\n')
    return root


def put_state(root, version, fmt=1):
    p = root / ".myvec" / "scaffold.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"format": fmt, "version": version, "answers": {"secret_path": "/home/u/x"}, "files": {}}))


def start(root, **kw):
    return {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(root), "source": "startup", **kw}


def run(monkeypatch, capsys, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main(["session-start"]) == 0
    return capsys.readouterr().out


def notice(out):
    return [ln for ln in out.splitlines() if ln.startswith("[myvec] process:")]


def test_outdated_prints_line_with_numbers_and_phrase(monkeypatch, capsys, proj):
    put_state(proj, PROCESS_VERSION - 1)
    lines = notice(run(monkeypatch, capsys, start(proj)))
    assert len(lines) == 1
    assert f"v{PROCESS_VERSION - 1}" in lines[0] and f"v{PROCESS_VERSION}" in lines[0]
    assert f'"{problems.UPDATE_PHRASE}"' in lines[0]


def test_hint_uses_constants_from_problems(monkeypatch, capsys, proj):
    monkeypatch.setattr(problems, "UPDATE_PHRASE", "ZZZ-фраза")
    put_state(proj, PROCESS_VERSION - 1)
    assert '"ZZZ-фраза"' in notice(run(monkeypatch, capsys, start(proj)))[0]
    assert problems.UPDATE_WRITE == problems.UPDATE_CMD + " --write"


def test_problem_hint_uses_update_constants():
    d = problems.describe("process_outdated")
    assert d["cmd"] == problems.UPDATE_CMD
    assert problems.UPDATE_PHRASE in d["fix"] and problems.UPDATE_WRITE in d["fix"]
    assert not d["legacy"]


@pytest.mark.parametrize("version,fmt", [(PROCESS_VERSION, 1), (PROCESS_VERSION + 1, 1), (PROCESS_VERSION - 1, 99)])
def test_current_or_newer_state_is_silent(monkeypatch, capsys, proj, version, fmt):
    put_state(proj, version, fmt)
    assert notice(run(monkeypatch, capsys, start(proj))) == []


def test_invalid_state_is_silent_and_exit_zero(monkeypatch, capsys, proj):
    (proj / ".myvec").mkdir()
    (proj / ".myvec" / "scaffold.json").write_text("{not json")
    assert notice(run(monkeypatch, capsys, start(proj))) == []
    (proj / ".myvec" / "scaffold.json").write_text('{"format":1,"version":"2"}')
    assert notice(run(monkeypatch, capsys, start(proj))) == []


def test_no_marker_file_is_silent(monkeypatch, capsys, proj):
    assert notice(run(monkeypatch, capsys, start(proj))) == []


def test_agent_id_is_silent(monkeypatch, capsys, proj):
    put_state(proj, PROCESS_VERSION - 1)
    assert notice(run(monkeypatch, capsys, start(proj, agent_id="a1"))) == []


def test_linked_worktree_silent_submodule_and_plain_notified(monkeypatch, capsys, proj, tmp_path):
    put_state(proj, PROCESS_VERSION - 1)
    sub = tmp_path / "sub.git"
    sub.mkdir()
    (sub / "HEAD").write_text("ref: refs/heads/main\n")
    (proj / ".git").write_text(f"gitdir: {sub}\n")
    assert notice(run(monkeypatch, capsys, start(proj)))          # submodule: no commondir
    (sub / "commondir").write_text("../..\n")
    assert notice(run(monkeypatch, capsys, start(proj))) == []     # linked worktree


def test_state_read_exception_is_silent(monkeypatch, capsys, proj):
    put_state(proj, PROCESS_VERSION - 1)

    def boom(root):
        raise RuntimeError("boom")
    monkeypatch.setattr(state, "read", boom)
    assert notice(run(monkeypatch, capsys, start(proj))) == []


def test_line_has_no_paths_or_answers(monkeypatch, capsys, proj):
    put_state(proj, PROCESS_VERSION - 1)
    line = notice(run(monkeypatch, capsys, start(proj)))[0]
    assert str(proj) not in line and "/home" not in line and "secret_path" not in line and "answers" not in line


def test_context_and_inbox_lines_stay(monkeypatch, capsys, proj):
    from myvec import inbox

    (proj / "docs/todo").mkdir(parents=True)
    inbox.write(proj, "docs/todo", "E1-01", "hi", None)
    put_state(proj, PROCESS_VERSION - 1)
    out = run(monkeypatch, capsys, start(proj))
    lines = out.splitlines()
    pi = next(i for i, ln in enumerate(lines) if ln.startswith("[myvec] process:"))
    ii = next(i for i, ln in enumerate(lines) if "inbox" in ln)
    assert pi < ii


def _sub(code, root=None, home=None):
    env = {**os.environ, "PYTHONPATH": SRC, "MYVEC_HOME": str(home)}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)


def test_scaffold_not_imported_without_state_file(proj, tmp_path):
    payload = json.dumps(start(proj))
    code = ("import io, sys, json\n"
            f"sys.stdin = io.StringIO({payload!r})\n"
            "from myvec import hooks\n"
            "assert hooks.main(['session-start']) == 0\n"
            "assert 'myvec.scaffold' not in sys.modules, 'scaffold imported'\n")
    r = _sub(code, home=tmp_path / "home")
    assert r.returncode == 0, r.stderr


def test_scaffold_imported_when_state_file_exists(proj, tmp_path):
    put_state(proj, PROCESS_VERSION - 1)
    payload = json.dumps(start(proj))
    code = ("import io, sys\n"
            f"sys.stdin = io.StringIO({payload!r})\n"
            "from myvec import hooks\n"
            "assert hooks.main(['session-start']) == 0\n"
            "assert 'myvec.scaffold' in sys.modules\n")
    r = _sub(code, home=tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert "[myvec] process:" in r.stdout


def test_importing_problems_is_cheap(tmp_path):
    code = ("import sys\nimport myvec.problems\n"
            "bad = [m for m in ('typer', 'sqlite_vec', 'httpx', 'myvec.scaffold') if m in sys.modules]\n"
            "assert not bad, bad\n")
    r = _sub(code, home=tmp_path / "home")
    assert r.returncode == 0, r.stderr
