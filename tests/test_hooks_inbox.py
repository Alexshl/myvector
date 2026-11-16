"""Сповіщення про inbox у хуках (E3-02, ADR-022)."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

from myvec import config, hooks, inbox

SECRET = "AKIAIOSFODNN7EXAMPLE"
LONG = "ВЕЛИКИЙ-ТЕКСТ-КОМЕНТАРЯ " * 20


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "docs/todo").mkdir(parents=True)
    (root / ".myvector.toml").write_text('name = "acme"\n')
    return root


def run(monkeypatch, capsys, payload, which="session-event"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main([which]) == 0
    return capsys.readouterr().out


def prompt(root, sid="s1", **kw):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(root), **kw}


def start(root, sid="s1", **kw):
    return {"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(root), "source": "startup", **kw}


def add(root, tid="E3-01", text="hi", status=None):
    time.sleep(0.01)
    return inbox.write(root, "docs/todo", tid, text, status)


def state_files():
    d = config.home() / "state" / "inbox_seen"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_start_prints_ids_kinds_status_never_text(monkeypatch, capsys, proj):
    add(proj, "E3-01", LONG)
    add(proj, "E2-04", "x", "done")
    out = run(monkeypatch, capsys, start(proj), "session-start")
    assert "[myvec] inbox: 2 new in docs/todo/inbox" in out
    assert "E3-01 comment" in out and "E2-04 status->done" in out
    assert "ВЕЛИКИЙ" not in out and len(out.strip().splitlines()[-1]) <= 400


def test_secretlike_text_not_printed(monkeypatch, capsys, proj):
    p = proj / "docs/todo/inbox"
    p.mkdir()
    (p / "a.md").write_text(f"---\nticket: E1-01\nkind: status\nrequested_status: {SECRET}\n---\n{SECRET}\n")
    out = run(monkeypatch, capsys, start(proj), "session-start")
    assert SECRET not in out and "E1-01 status" in out and "->" not in out


def test_prompt_sequence(monkeypatch, capsys, proj):
    assert "inbox" not in run(monkeypatch, capsys, start(proj), "session-start")
    assert run(monkeypatch, capsys, prompt(proj)) == ""
    a = add(proj, "E1-01")
    assert "E1-01 comment" in run(monkeypatch, capsys, prompt(proj))
    assert run(monkeypatch, capsys, prompt(proj)) == ""
    (proj / "docs/todo/inbox" / a["file"]).unlink()
    assert run(monkeypatch, capsys, prompt(proj)) == ""
    add(proj, "E1-02")
    out = run(monkeypatch, capsys, prompt(proj))
    assert "1 new" in out and "E1-02" in out and "E1-01" not in out


def test_start_always_prints_even_compact(monkeypatch, capsys, proj):
    add(proj)
    run(monkeypatch, capsys, start(proj), "session-start")
    out = run(monkeypatch, capsys, start(proj, source="compact"), "session-start")
    assert "inbox: 1 new" in out


def test_empty_inbox_no_output_no_state(monkeypatch, capsys, proj):
    assert run(monkeypatch, capsys, prompt(proj)) == ""
    assert state_files() == []


def test_no_marker(monkeypatch, capsys, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert run(monkeypatch, capsys, prompt(d)) == ""


def test_agent_id_silent(monkeypatch, capsys, proj):
    add(proj)
    assert run(monkeypatch, capsys, prompt(proj, agent_id="a1")) == ""


def test_linked_worktree_silent_submodule_and_nogit_notified(monkeypatch, capsys, proj, tmp_path):
    add(proj)
    assert "inbox" in run(monkeypatch, capsys, prompt(proj, sid="nogit"))
    # submodule: .git file, gitdir without commondir
    sub = tmp_path / "sub.git"
    sub.mkdir()
    (sub / "HEAD").write_text("ref: refs/heads/main\n")
    (proj / ".git").write_text(f"gitdir: {sub}\n")
    assert "inbox" in run(monkeypatch, capsys, prompt(proj, sid="subm"))
    # linked worktree: commondir present
    (sub / "commondir").write_text("../..\n")
    assert run(monkeypatch, capsys, prompt(proj, sid="wt")) == ""
    assert "inbox" not in run(monkeypatch, capsys, start(proj, sid="wt2"), "session-start")


def test_state_only_in_home_bad_sid_and_end(monkeypatch, capsys, proj):
    add(proj)
    run(monkeypatch, capsys, start(proj, sid="ok-1"), "session-start")
    assert state_files() == ["ok-1"]
    assert (config.home() / "state/inbox_seen/ok-1").stat().st_mode & 0o777 == 0o600
    assert "inbox" in run(monkeypatch, capsys, start(proj, sid="../evil"), "session-start")
    assert run(monkeypatch, capsys, prompt(proj, sid="../evil")) == ""
    assert state_files() == ["ok-1"]
    run(monkeypatch, capsys, {"hook_event_name": "SessionEnd", "session_id": "ok-1", "cwd": str(proj)})
    assert state_files() == []


def test_old_state_pruned_on_start(monkeypatch, capsys, proj):
    d = config.home() / "state" / "inbox_seen"
    d.mkdir(parents=True)
    old = d / "old"
    old.write_text("")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    run(monkeypatch, capsys, start(proj), "session-start")
    assert state_files() == []


def test_unwritable_state_still_notifies_on_start(monkeypatch, capsys, proj):
    add(proj)
    (config.home() / "state").write_text("a file where the dir should be")
    out = run(monkeypatch, capsys, start(proj), "session-start")
    assert "inbox: 1 new" in out and "Traceback" not in out
    assert run(monkeypatch, capsys, prompt(proj)) == ""


def test_broken_marker_exit_zero_and_journal_written(monkeypatch, capsys, proj):
    add(proj)
    (proj / ".myvector.toml").write_text('name = "/abs"\n')
    assert run(monkeypatch, capsys, prompt(proj)) == ""
    recs = [json.loads(x) for x in config.session_events_path().read_text().splitlines()]
    assert recs[0]["ev"] == "prompt"


def test_newline_in_filename_not_repeated(monkeypatch, capsys, proj):
    d = proj / "docs/todo/inbox"
    d.mkdir()
    (d / "a\nb.md").write_text("---\nticket: E1-01\nkind: comment\n---\nx\n")
    assert run(monkeypatch, capsys, prompt(proj)) == ""


def test_journal_written_before_notice(monkeypatch, capsys, proj):
    add(proj)
    monkeypatch.setattr(hooks, "_inbox_notice", lambda o, e: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(prompt(proj))))
    assert hooks.main(["session-event"]) == 0
    recs = [json.loads(x) for x in config.session_events_path().read_text().splitlines()]
    assert recs[0]["ev"] == "prompt"


def test_inbox_symlink_ignored(monkeypatch, capsys, proj, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (proj / "docs/todo/inbox").symlink_to(other)
    assert run(monkeypatch, capsys, prompt(proj)) == ""


def test_unreadable_file_skipped(monkeypatch, capsys, proj):
    add(proj, "E1-01")
    bad = add(proj, "E1-02")
    p = proj / "docs/todo/inbox" / bad["file"]
    p.chmod(0)
    try:
        out = run(monkeypatch, capsys, prompt(proj))
    finally:
        p.chmod(0o600)
    assert "E1-01 comment" in out and "E1-02" not in out


def test_no_inbox_dir_skips_imports(proj):
    code = ("import sys;from myvec import hooks;hooks.main(['session-event']);"
            "print([m for m in ('myvec.inbox','myvec.tickets','myvec.projects') if m in sys.modules])")
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps(prompt(proj)), capture_output=True,
                       text=True, env=dict(os.environ), timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "[]"


def test_prompt_path_light_and_fast(proj, tmp_path):
    inbox.write(proj, "docs/todo", "E3-01", "x")
    code = ("import sys,time;t=time.time();from myvec import hooks;r=hooks.main(['session-event']);"
            "bad=[m for m in ('typer','httpx','sqlite_vec') if m in sys.modules];print(r,bad,time.time()-t)")
    payload = json.dumps(prompt(proj))
    r = subprocess.run([sys.executable, "-c", code], input=payload, capture_output=True, text=True,
                       env=dict(os.environ), timeout=30)
    assert r.returncode == 0, r.stderr
    assert "inbox: 1 new" in r.stdout
    last = r.stdout.strip().splitlines()[-1].split(" ", 1)
    assert last[0] == "0" and "[]" in last[1]
    assert float(last[1].split()[-1]) < 1.0


def test_ctx_branch_comes_from_the_linked_worktree_not_the_main_checkout(tmp_path):
    from conftest import git
    from myvec import ctx as ctxmod

    root = tmp_path / "main_repo"
    root.mkdir()
    (root / ".myvector.toml").write_text('name = "acme"\n')
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("x")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "i")
    wt = root / ".worktrees" / "E1-01"
    git(root, "worktree", "add", "-q", str(wt), "-b", "t/E1-01-x")
    assert ctxmod.current(None, root).branch == "main"
    assert ctxmod.current(None, wt).branch == "t/E1-01-x"
    sub = wt / "sub"
    sub.mkdir()
    assert ctxmod.current(None, sub).branch == "t/E1-01-x"


def lesson_add(root, origin="project-a", text="секретне правило"):
    time.sleep(0.01)
    return inbox.write(root, "docs/todo", "lesson", text, kind="lesson", source="cli", origin=origin)


def test_lesson_line_shows_from_and_tail_never_text(monkeypatch, capsys, proj):
    lesson_add(proj, text="СЕКРЕТНЕ-ПРАВИЛО")
    out = run(monkeypatch, capsys, start(proj), "session-start")
    line = out.strip().splitlines()[-1]
    assert "lesson<-project-a" in line and "orchestrator.md" in line and "Lessons back-channel" in line
    assert "СЕКРЕТНЕ" not in out and len(line) <= 400


def test_no_lesson_tail_without_lessons(monkeypatch, capsys, proj):
    add(proj, "E1-01")
    assert "Lessons back-channel" not in run(monkeypatch, capsys, start(proj), "session-start")


def test_lesson_from_sanitised(monkeypatch, capsys, proj):
    d = proj / "docs/todo/inbox"
    d.mkdir()
    (d / "a.md").write_text("---\nticket: lesson\nkind: lesson\nfrom: 'a b;rm'\n---\nx\n")
    out = run(monkeypatch, capsys, start(proj), "session-start")
    assert "lesson<-" in out and ";" not in out.split("inbox:")[1].split("(")[1].split(")")[0]


def test_lesson_line_limit_with_many(monkeypatch, capsys, proj):
    for i in range(8):
        lesson_add(proj, origin="p" * 60 + str(i))
    line = run(monkeypatch, capsys, start(proj), "session-start").strip().splitlines()[-1]
    assert len(line) <= 400 and "lesson<-" in line


def test_lesson_tail_survives_long_ids_and_late_lesson(monkeypatch, capsys, proj):
    for i in range(10):
        add(proj, "E" + "9" * 55 + str(i))
    lesson_add(proj, origin="p" * 60)
    line = run(monkeypatch, capsys, start(proj), "session-start").strip().splitlines()[-1]
    assert len(line) <= 400 and line.endswith("delete the file.") and "Lessons back-channel" in line
