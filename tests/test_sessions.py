import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from typer.testing import CliRunner

from myvec import config, hooks
from myvec import db as dbmod
from myvec import sessions as sess
from myvec.commands import app

runner = CliRunner()
T0 = 1_800_000_000.0
AWS = "AKIAIOSFODNN7EXAMPLE"


# ---------------------------------------------------------------- helpers

def run_hook(monkeypatch, payload, which="session-event", raw=None) -> int:
    data = raw if raw is not None else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(data))
    return hooks.main([which])


def journal() -> list[dict]:
    p = config.session_events_path()
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def make_db():
    return dbmod.open_ready(config.load_config())


def ev(ev_, ts, sid="s1", **kw):
    return {"ts": ts, "ev": ev_, "sid": sid, **kw}


def write_journal(events):
    p = config.session_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def row(db, sid="s1"):
    r = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return dict(r) if r else None


def fold_events(db, events, now=T0 + 1000):
    write_journal(events)
    return sess.fold(db, now=now)


# ---------------------------------------------------------------- hook: payload -> journal

def test_session_start_event_has_session_cwd_and_source(monkeypatch, tmp_path):
    assert run_hook(monkeypatch, {"hook_event_name": "SessionStart", "session_id": "abc", "cwd": str(tmp_path),
                                  "source": "resume", "model": "m1", "permission_mode": "default",
                                  "transcript_path": "/t/x.jsonl"}, "session-start") == 0
    (r,) = journal()
    assert r["ev"] == "start" and r["sid"] == "abc" and r["cwd"] == str(tmp_path)
    assert r["src"] == "resume" and r["model"] == "m1" and r["pm"] == "default" and r["tp"] == "/t/x.jsonl"


def test_branch_from_plain_git_repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "cwd": str(sub)})
    (r,) = journal()
    assert r["branch"] == "feature/x" and r["wt"] == str(tmp_path)


def test_branch_and_worktree_from_gitdir_file(monkeypatch, tmp_path):
    real = tmp_path / "main" / ".git" / "worktrees" / "w1"
    real.mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/t/E2-01-hooks\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real}\n")
    run_hook(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": str(wt)})
    (r,) = journal()
    assert r["branch"] == "t/E2-01-hooks" and r["wt"] == str(wt)


def test_detached_head_gives_short_sha(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("0123456789abcdef0123\n")
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "cwd": str(tmp_path)})
    assert journal()[0]["branch"] == "01234567"


def test_user_prompt_is_never_stored(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": "/x",
                           "prompt": "my very private prompt text"})
    (r,) = journal()
    assert r["ev"] == "prompt"
    assert "private prompt" not in config.session_events_path().read_text()


def test_stop_event_keeps_tail_of_message_and_background_flag(monkeypatch):
    msg = "A" * 700 + "THE-END"
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "cwd": "/x", "last_assistant_message": msg})
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "cwd": "/x", "background_tasks": [{"id": 1}]})
    a, b = journal()
    assert a["ev"] == "stop" and len(a["msg"]) == hooks.MAX_MSG and a["msg"].endswith("THE-END") and "bg" not in a
    assert b["bg"] == 1


def test_ask_user_question_text_joined(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "s",
                           "cwd": "/x", "tool_input": {"questions": [{"question": "Which db?"}, {"question": "Which port?"}]}})
    (r,) = journal()
    assert r["ev"] == "ask" and r["q"] == "Which db? / Which port?"


def test_ask_question_clipped_to_limit(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "s",
                           "tool_input": {"questions": [{"question": "q" * 5000}]}})
    (r,) = journal()
    assert len(r["q"]) <= hooks.MAX_Q
    assert len((config.session_events_path().read_bytes())) <= hooks.MAX_LINE + 1


def test_post_tool_ask_user_question_is_answered(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "session_id": "s"})
    assert journal()[0]["ev"] == "answered"


@pytest.mark.parametrize("ntype", ["permission_prompt", "idle_prompt", "elicitation_dialog", "auth_success"])
def test_notification_types_recorded(monkeypatch, ntype):
    run_hook(monkeypatch, {"hook_event_name": "Notification", "session_id": "s", "cwd": "/x",
                           "notification_type": ntype, "message": "Claude needs you"})
    (r,) = journal()
    assert r["ev"] == "notify" and r["ntype"] == ntype and r["msg"] == "Claude needs you"


def test_session_end_reason_recorded(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "SessionEnd", "session_id": "s", "reason": "logout"})
    r = journal()[0]
    assert r["ev"] == "end" and r["reason"] == "logout"


def test_subagent_events_are_flagged(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "agent_id": "ag1", "agent_type": "x"})
    assert journal()[0]["agent"] == 1


def test_tool_pulse_and_post_edit_write_tool_event(monkeypatch, tmp_path):
    run_hook(monkeypatch, {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s", "cwd": "/x"},
             "session-pulse")
    run_hook(monkeypatch, {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "s",
                           "tool_input": {"file_path": str(tmp_path / "f.py")}}, "post-edit")
    assert [r["ev"] for r in journal()] == ["tool", "tool"]


def test_session_start_hook_also_logs_event(monkeypatch, tmp_path, capsys):
    run_hook(monkeypatch, {"session_id": "s9", "cwd": str(tmp_path)}, "session-start")
    assert journal()[0]["ev"] == "start"
    assert capsys.readouterr().out == ""


def test_unknown_event_and_missing_session_id_write_nothing(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "Whatever", "session_id": "s"})
    run_hook(monkeypatch, {"hook_event_name": "Stop"})
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": ""})
    assert journal() == []


def test_journal_file_mode_0600(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s"})
    assert oct(config.session_events_path().stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- hook: robustness

@pytest.mark.parametrize("raw", ["", "not json {{{", "[1,2,3]", "null", "\x00\xff garbage", "   \n"])
def test_hook_exits_zero_on_bad_stdin_without_stdout(monkeypatch, capsys, raw, tmp_path):
    monkeypatch.chdir(tmp_path)   # session-start falls back to cwd; keep it outside any project
    for which in ("session-event", "session-pulse", "session-start", "post-edit"):
        assert run_hook(monkeypatch, None, which, raw=raw) == 0
    assert capsys.readouterr().out == ""
    assert journal() == []


def test_hook_exits_zero_when_state_dir_unwritable(monkeypatch, capsys):
    state = config.session_events_path().parent
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("a file where the state dir should be")
    assert run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s"}) == 0
    assert capsys.readouterr().out == ""


def test_hook_exits_zero_when_append_raises(monkeypatch):
    def boom(rec):
        raise OSError("disk full")

    monkeypatch.setattr(hooks, "_append_session_event", boom)
    assert run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s"}) == 0


def _sub(payload, which="session-event", extra="", env_extra=None):
    code = (f"import sys,time;t=time.time();from myvec import hooks;r=hooks.main(['{which}']);"
            "bad=[m for m in ('typer','httpx','sqlite_vec') if m in sys.modules];"
            f"print(r,bad,time.time()-t);{extra}")
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, "-c", code], input=payload, capture_output=True, text=True, env=env,
                          timeout=30)


def test_hook_does_not_import_heavy_modules_and_is_fast():
    r = _sub(json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/x", "last_assistant_message": "m"}))
    assert r.returncode == 0, r.stderr
    code, bad, took = r.stdout.split(" ", 2)[0], r.stdout[r.stdout.index("["):r.stdout.index("]") + 1], r.stdout.split()[-1]
    assert code == "0" and bad == "[]"
    assert float(took) < 1.0   # soft bound against flakes; real figure is ~50 ms
    assert len(journal()) == 1


def test_hook_via_python_m_myvec_subprocess():
    r = subprocess.run([sys.executable, "-m", "myvec", "hook", "session-event"], capture_output=True, text=True,
                       input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "sx", "reason": "other"}),
                       env=dict(os.environ), timeout=30)
    assert r.returncode == 0 and r.stdout == ""
    assert journal()[0]["sid"] == "sx"
    r = subprocess.run([sys.executable, "-m", "myvec", "hook", "session-event"], capture_output=True, text=True,
                       input="garbage", env=dict(os.environ), timeout=30)
    assert r.returncode == 0 and r.stdout == ""


# ---------------------------------------------------------------- secrets

def test_question_with_token_never_reaches_journal(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "s",
                           "tool_input": {"questions": [{"question": f"Use key {AWS} ok?"}]}})
    text = config.session_events_path().read_text()
    assert AWS not in text
    assert "q" not in journal()[0]   # hard secret: field dropped


def test_literal_secret_value_masked_in_question_and_message(monkeypatch):
    run_hook(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "s",
                           "tool_input": {"questions": [{"question": "Set password=hunter2hunter2 now?"}]}})
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "done, DB_PASSWORD=hunter2hunter2"})
    text = config.session_events_path().read_text()
    assert "hunter2hunter2" not in text and "•••" in text


def test_fold_masks_again_what_a_foreign_journal_line_contains():
    db = make_db()
    fold_events(db, [ev("ask", T0, q=f"token {AWS}?"), ev("stop", T0 + 1, msg="password=hunter2hunter2 done")])
    r = row(db)
    assert r["question"] is None   # state changed by stop -> cleared anyway
    assert "hunter2hunter2" not in (r["last_message"] or "") and "•••" in r["last_message"]
    fold_events(db, [ev("ask", T0 + 2, q=f"use {AWS}")])
    assert row(db)["question"] is None
    fold_events(db, [ev("prompt", T0 + 3), ev("ask", T0 + 4, q="password=hunter2hunter2 ok?")])
    assert "hunter2hunter2" not in row(db)["question"]


# ---------------------------------------------------------------- state machine (apply)

def apply_all(events, row_=None):
    for e in events:
        row_ = sess.apply(row_, e)
    return row_


def test_start_gives_working_state():
    r = apply_all([ev("start", T0, src="startup", cwd="/p", branch="main", wt="/p")])
    assert r["state"] == "working" and r["branch"] == "main" and r["worktree"] == "/p" and r["source"] == "startup"


def test_prompt_then_stop_gives_waiting_with_last_message():
    r = apply_all([ev("start", T0), ev("prompt", T0 + 1), ev("stop", T0 + 5, msg="all done")])
    assert r["state"] == "waiting" and r["last_message"] == "all done"


def test_stop_with_background_tasks_stays_working():
    r = apply_all([ev("prompt", T0), ev("stop", T0 + 1, bg=1, msg="x")])
    assert r["state"] == "working" and r["last_message"] is None


def test_ask_user_question_waiting_with_text_then_answered_working():
    r = apply_all([ev("prompt", T0), ev("ask", T0 + 1, q="Which?")])
    assert r["state"] == "waiting" and r["question"] == "Which?" and r["question_at"] == T0 + 1
    r = sess.apply(r, ev("answered", T0 + 2))
    assert r["state"] == "working" and r["question"] is None and r["question_at"] is None


@pytest.mark.parametrize("nt", ["idle_prompt", "permission_prompt", "elicitation_dialog",
                                "elicitation_url_dialog", "agent_needs_input", None])
def test_waiting_notifications(nt):
    kw = {"ntype": nt} if nt else {}
    r = apply_all([ev("prompt", T0), ev("notify", T0 + 1, **kw)])
    assert r["state"] == "waiting" and r["notification_type"] == nt


def test_other_notification_only_updates_last_seen():
    r = apply_all([ev("prompt", T0), ev("notify", T0 + 9, ntype="auth_success")])
    assert r["state"] == "working" and r["last_seen"] == T0 + 9


def test_heartbeat_never_leaves_permission_wait_even_with_parallel_tools():
    r = apply_all([ev("prompt", T0), ev("notify", T0 + 1, ntype="permission_prompt"), ev("tool", T0 + 2)])
    assert r["state"] == "waiting" and r["notification_type"] == "permission_prompt" and r["last_event_at"] == T0 + 2
    r = apply_all([ev("prompt", T0), ev("notify", T0 + 1, ntype="permission_prompt"), ev("tool", T0 + 2),
                   ev("prompt", T0 + 3)])
    assert r["state"] == "working"


def test_old_event_replay_does_not_roll_state_back():
    r = apply_all([ev("prompt", T0), ev("stop", T0 + 5), ev("prompt", T0 + 1)])
    assert r["state"] == "waiting"


def test_late_stamped_heartbeat_does_not_hide_ask_waiting():
    r = apply_all([ev("prompt", T0), ev("tool", T0 + 11), ev("ask", T0 + 10, q="Which db?")])
    assert r["state"] == "waiting" and r["question"] == "Which db?"


def test_secret_straddling_the_message_cut_is_not_journaled(monkeypatch):
    msg = "x" * (hooks.MAX_MSG - 8) + " password=hunter2hunter2 tail"
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": msg})
    assert "hunter2" not in config.session_events_path().read_text()
    tok = "y" * 480 + " " + AWS + " end"
    run_hook(monkeypatch, {"hook_event_name": "Notification", "session_id": "s", "message": tok[:hooks.MAX_MSG - 10] + tok})
    assert "AKIA" not in config.session_events_path().read_text()


def test_big_journal_drops_heartbeats_keeps_state_events(monkeypatch):
    p = config.session_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    monkeypatch.setattr(hooks, "MAX_JOURNAL", 10)
    p.write_text("x" * 50 + "\n")
    run_hook(monkeypatch, {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s"}, "session-pulse")
    run_hook(monkeypatch, {"hook_event_name": "Stop", "session_id": "s"})
    lines = p.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["ev"] == "stop"


def test_install_removes_stale_async_from_sync_hooks(tmp_path):
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/x/myvec hook session-event", "async": True}]}]}}))
    hooks.install(sp, "/x/myvec")
    stop = json.loads(sp.read_text())["hooks"]["Stop"][0]["hooks"][0]
    assert "async" not in stop


def test_tool_event_does_not_clear_other_waiting_states():
    r = apply_all([ev("prompt", T0), ev("stop", T0 + 1), ev("tool", T0 + 2)])
    assert r["state"] == "waiting"
    r = apply_all([ev("prompt", T0), ev("notify", T0 + 1, ntype="idle_prompt"), ev("tool", T0 + 2)])
    assert r["state"] == "waiting"


def test_end_marks_ended_with_reason():
    r = apply_all([ev("start", T0), ev("end", T0 + 3, reason="logout")])
    assert r["state"] == "ended" and r["ended_at"] == T0 + 3 and r["end_reason"] == "logout"


def test_heartbeat_does_not_reopen_ended_session():
    r = apply_all([ev("start", T0), ev("end", T0 + 3, reason="other"), ev("tool", T0 + 4)])
    assert r["state"] == "ended" and r["ended_at"] == T0 + 3


def test_resume_reopens_ended_session():
    r = apply_all([ev("start", T0), ev("end", T0 + 3, reason="other"), ev("start", T0 + 10, src="resume")])
    assert r["state"] == "working" and r["ended_at"] is None and r["end_reason"] is None
    assert r["started_at"] == T0 + 10 and r["source"] == "resume"


def test_prompt_after_end_also_reopens():
    r = apply_all([ev("start", T0), ev("end", T0 + 3), ev("prompt", T0 + 5)])
    assert r["state"] == "working" and r["ended_at"] is None


def test_compact_start_does_not_change_state():
    r = apply_all([ev("prompt", T0), ev("stop", T0 + 1), ev("start", T0 + 2, src="compact")])
    assert r["state"] == "waiting" and r["last_seen"] == T0 + 2


@pytest.mark.parametrize("sub", [ev("prompt", T0 + 5, agent=1), ev("stop", T0 + 5, agent=1, msg="sub"),
                                 ev("ask", T0 + 5, agent=1, q="sub?"), ev("end", T0 + 5, agent=1, reason="x"),
                                 ev("notify", T0 + 5, agent=1, ntype="idle_prompt")])
def test_subagent_events_do_not_change_state(sub):
    base = apply_all([ev("prompt", T0)])
    after = sess.apply(base, sub)
    assert after["state"] == "working" and after["last_message"] is None and after["question"] is None
    assert after["last_seen"] == T0 + 5
    waiting = apply_all([ev("prompt", T0), ev("stop", T0 + 1)])
    assert sess.apply(waiting, sub)["state"] == "waiting"


def test_idle_after_silence_computed_on_read_not_stored():
    r = apply_all([ev("prompt", T0)])
    assert sess.effective_state(r, T0 + 15 * 60, 15) == "working"       # exactly 15 min: not yet
    assert sess.effective_state(r, T0 + 15 * 60 + 1, 15) == "idle"
    assert sess.effective_state(r, T0 + 3600, 120) == "working"          # configurable
    assert r["state"] == "working"


def test_stop_then_15_min_silence_stays_waiting_not_idle():
    r = apply_all([ev("prompt", T0), ev("stop", T0 + 1)])
    assert sess.effective_state(r, T0 + 10 * 86400, 15) == "waiting"


def test_waiting_question_never_decays_to_idle():
    r = apply_all([ev("ask", T0, q="?")])
    assert sess.effective_state(r, T0 + 86400, 15) == "waiting"


def test_ended_stays_ended_at_read():
    r = apply_all([ev("start", T0), ev("end", T0 + 1)])
    assert sess.effective_state(r, T0 + 99999, 15) == "ended"


def test_heartbeats_keep_working_session_from_idling():
    r = apply_all([ev("prompt", T0), ev("tool", T0 + 20 * 60)])
    assert sess.effective_state(r, T0 + 25 * 60, 15) == "working"


# ---------------------------------------------------------------- fold

def test_fold_journal_into_sessions_table_and_reports_changes():
    db = make_db()
    ch = fold_events(db, [ev("start", T0, cwd="/p", branch="main", src="startup"), ev("stop", T0 + 5, msg="hi")])
    r = row(db)
    assert r["state"] == "waiting" and r["last_message"] == "hi" and r["branch"] == "main" and r["cwd"] == "/p"
    assert ch == [{"session_id": "s1", "state": "waiting", "project": None}]
    assert not config.session_events_path().exists()
    assert not list(config.session_events_path().parent.glob("*.folding.*"))


def test_fold_reports_only_state_changes():
    db = make_db()
    fold_events(db, [ev("prompt", T0)])
    assert fold_events(db, [ev("tool", T0 + 1)]) == []
    assert [c["state"] for c in fold_events(db, [ev("stop", T0 + 2)])] == ["waiting"]


def test_fold_sorts_events_by_ts():
    db = make_db()
    fold_events(db, [ev("stop", T0 + 5), ev("prompt", T0 + 1)])
    assert row(db)["state"] == "waiting"


def test_fold_is_idempotent_on_empty_journal():
    db = make_db()
    fold_events(db, [ev("prompt", T0)])
    before = row(db)
    assert sess.fold(db, now=T0 + 10) == []
    assert row(db) == before


def test_fold_skips_garbage_lines():
    db = make_db()
    p = config.session_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("garbage\n" + json.dumps(ev("prompt", T0)) + "\n[1]\n" + json.dumps({"ev": "x"}) + "\n")
    sess.fold(db, now=T0)
    assert row(db)["state"] == "working"


def test_fold_resolves_project_by_deepest_registered_root(ws):
    db = make_db()
    db.execute("INSERT INTO projects(path, root) VALUES('acme', ?)", (str(ws),))
    db.execute("INSERT INTO projects(path, root) VALUES('acme/site', ?)", (str(ws / "site"),))
    db.commit()
    fold_events(db, [ev("prompt", T0, sid="a", cwd=str(ws / "site" / "src")), ev("prompt", T0, sid="b", cwd=str(ws)),
                     ev("prompt", T0, sid="c", cwd="/elsewhere"),
                     ev("prompt", T0, sid="d", cwd="/elsewhere", wt=str(ws / "libs"))])
    assert row(db, "a")["project_path"] == "acme/site"
    assert row(db, "b")["project_path"] == "acme"
    assert row(db, "c")["project_path"] is None
    assert row(db, "d")["project_path"] == "acme"


def test_failed_fold_keeps_claimed_file_and_next_fold_adopts_it(monkeypatch):
    db = make_db()
    write_journal([ev("prompt", T0), ev("stop", T0 + 1, msg="m")])
    real = sess.apply

    def boom(r, e):
        raise RuntimeError("write failed")

    monkeypatch.setattr(sess, "apply", boom)
    with pytest.raises(RuntimeError):
        sess.fold(db, now=T0)
    monkeypatch.setattr(sess, "apply", real)
    leftovers = list(config.session_events_path().parent.glob("session_events.jsonl.folding.*"))
    assert len(leftovers) == 1 and row(db) is None
    assert sess.pending()
    ch = sess.fold(db, now=T0 + 5)
    assert row(db)["state"] == "waiting" and ch[0]["state"] == "waiting"
    assert not list(config.session_events_path().parent.glob("session_events.jsonl.folding.*"))


def test_claimed_file_of_dead_pid_is_adopted(monkeypatch):
    db = make_db()
    d = config.session_events_path().parent
    d.mkdir(parents=True, exist_ok=True)
    dead = d / "session_events.jsonl.folding.99999999.deadbeef"
    dead.write_text(json.dumps(ev("prompt", T0)) + "\n")
    monkeypatch.setattr(sess, "_pid_alive", lambda pid: False)
    sess.fold(db, now=T0)
    assert row(db)["state"] == "working" and not dead.exists()


def test_claimed_file_of_live_foreign_pid_is_left_alone(monkeypatch):
    db = make_db()
    d = config.session_events_path().parent
    d.mkdir(parents=True, exist_ok=True)
    other = d / f"session_events.jsonl.folding.{os.getpid() + 1}.cafe"
    other.write_text(json.dumps(ev("prompt", T0)) + "\n")
    monkeypatch.setattr(sess, "_pid_alive", lambda pid: True)
    sess.fold(db, now=T0)
    assert row(db) is None and other.exists()


def test_hook_appending_during_fold_loses_no_events(monkeypatch):
    db = make_db()
    write_journal([ev("prompt", T0, sid="seed")])
    real_read = sess._read_events
    done = threading.Event()

    def read_and_append(files):
        # a hook fires after the journal was claimed but before the fold finished
        run_hook_threadsafe({"hook_event_name": "Stop", "session_id": "late", "cwd": "/x"})
        done.set()
        return real_read(files)

    def run_hook_threadsafe(payload):
        rec = hooks._session_record(payload, "stop")
        hooks._append_session_event(rec)

    monkeypatch.setattr(sess, "_read_events", read_and_append)
    sess.fold(db, now=time.time())
    monkeypatch.setattr(sess, "_read_events", real_read)
    assert done.is_set() and row(db, "seed") is not None and row(db, "late") is None
    assert [r["sid"] for r in journal()] == ["late"]
    sess.fold(db, now=time.time())
    assert row(db, "late")["state"] == "waiting"


def test_many_concurrent_hook_appends_stay_whole_lines():
    recs = [hooks._session_record({"session_id": f"s{i}", "cwd": "/x", "last_assistant_message": "m" * 300}, "stop")
            for i in range(40)]
    ts = [threading.Thread(target=hooks._append_session_event, args=(r,)) for r in recs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r["sid"] for r in journal()) == sorted(f"s{i}" for i in range(40))


def test_pending_reflects_journal_presence():
    assert not sess.pending()
    write_journal([ev("prompt", T0)])
    assert sess.pending()


# ---------------------------------------------------------------- retention

def test_retention_deletes_ended_older_than_30_days_only():
    db = make_db()
    now = T0 + 100 * 86400
    fold_events(db, [ev("start", T0, sid="old"), ev("end", T0 + 1, sid="old"),
                     ev("start", now - 29 * 86400, sid="recent"), ev("end", now - 29 * 86400 + 1, sid="recent"),
                     ev("prompt", T0, sid="oldworking"), ev("stop", T0 + 1, sid="oldwaiting")], now=now)
    assert row(db, "old") is None
    assert row(db, "recent") is not None
    assert row(db, "oldworking") is not None and row(db, "oldwaiting") is not None


def test_retention_days_configurable_and_zero_disables():
    db = make_db()
    write_journal([ev("start", T0), ev("end", T0 + 1)])
    sess.fold(db, now=T0 + 3 * 86400, retention_days=0)
    assert row(db) is not None
    sess.fold(db, now=T0 + 3 * 86400, retention_days=2)
    assert row(db) is None


# ---------------------------------------------------------------- list / config / schema

def test_list_sessions_hides_old_ended_and_computes_state():
    db = make_db()
    now = T0 + 10 * 86400
    fold_events(db, [ev("start", T0, sid="old"), ev("end", T0 + 1, sid="old"),
                     ev("prompt", now - 3600, sid="quiet"), ev("prompt", now - 10, sid="busy"),
                     ev("ask", now - 5, sid="wait", q="?")], now=now)
    ids = {r["session_id"]: r for r in sess.list_sessions(db, now, 15)}
    assert set(ids) == {"quiet", "busy", "wait"}
    assert ids["quiet"]["effective_state"] == "idle" and ids["busy"]["effective_state"] == "working"
    assert ids["wait"]["effective_state"] == "waiting"
    assert "old" in {r["session_id"] for r in sess.list_sessions(db, now, 15, include_old_ended=True)}


def test_config_defaults_and_override():
    cfg = config.load_config()
    assert cfg.session_idle_minutes == 15 and cfg.session_retention_days == 30
    assert "session_idle_minutes = 15" in config.default_config_text()
    p = config.home() / "config.toml"
    p.write_text(p.read_text().replace("session_idle_minutes = 15", "session_idle_minutes = 5"))
    assert config.load_config().session_idle_minutes == 5


def test_v5_database_gets_sessions_table_without_loss(ws):
    from conftest import do_sync

    _, db = do_sync(ws)
    n = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    db.execute("DROP TABLE sessions")
    db.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    db.commit()
    db.close()
    db = make_db()
    assert dbmod.get_meta(db, "schema_version") == str(dbmod.SCHEMA_VERSION)
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == n
    idx = {r[1] for r in db.execute("PRAGMA index_list(sessions)")}
    assert any("state" in i for i in idx) and any("project" in i for i in idx)


def test_sessions_survive_in_new_db_columns():
    db = make_db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    assert set(sess.COLUMNS) <= cols


# ---------------------------------------------------------------- daemon

def test_daemon_tick_folds_and_publishes_session_changed(ws):
    from conftest import do_sync
    from myvec import daemon

    do_sync(ws)
    st = daemon.DaemonState(7878, "t")
    try:
        st.refresh()
        write_journal([ev("prompt", time.time() - 1, cwd=str(ws / "site")), ev("stop", time.time(), msg="x")])
        t0 = time.time()
        st._tick_sessions()
        assert time.time() - t0 < 2
        evs = [e for e in st.bus.recent(500) if e["type"] == "session_changed"]
        assert evs and evs[-1]["session_id"] == "s1" and evs[-1]["state"] == "waiting"
        assert row(st.db(), "s1")["state"] == "waiting"
        n = len(evs)
        st._tick_sessions()
        assert len([e for e in st.bus.recent(500) if e["type"] == "session_changed"]) == n
    finally:
        st.executor.shutdown(wait=True)
        st.close()


def test_daemon_tick_survives_fold_failure(ws, monkeypatch):
    from conftest import do_sync
    from myvec import daemon

    do_sync(ws)
    st = daemon.DaemonState(7878, "t")
    try:
        st.refresh()
        write_journal([ev("prompt", time.time())])
        real = sess.fold
        monkeypatch.setattr(sess, "fold", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        st._tick_sessions()   # must not raise
        monkeypatch.setattr(sess, "fold", real)
    finally:
        st.executor.shutdown(wait=True)
        st.close()


# ---------------------------------------------------------------- install-hooks

def _groups(data, event):
    return data["hooks"][event]


def test_install_hooks_adds_all_groups_idempotently_and_keeps_foreign(tmp_path):
    s = tmp_path / "settings.json"
    foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/other-stop"}]}],
                         "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]},
               "theme": "dark"}
    s.write_text(json.dumps(foreign))
    r = runner.invoke(app, ["install-hooks", "--settings", str(s)])
    assert r.exit_code == 0, r.output
    d = json.loads(s.read_text())
    assert d["theme"] == "dark"
    cmds = lambda ev: [h["command"] for g in d["hooks"][ev] for h in g["hooks"]]   # noqa: E731
    assert "/usr/bin/other-stop" in cmds("Stop") and any("hook session-event" in c for c in cmds("Stop"))
    assert "guard" in cmds("PreToolUse")
    for e in ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd", "PreToolUse", "PostToolUse"):
        assert e in d["hooks"]
    pre = [g for g in d["hooks"]["PreToolUse"] if g.get("matcher") == "AskUserQuestion"]
    post_matchers = [g.get("matcher") for g in d["hooks"]["PostToolUse"]]
    assert len(pre) == 1 and {"AskUserQuestion", "*", "Edit|Write|MultiEdit"} <= set(post_matchers)
    pulse = next(h for g in d["hooks"]["PostToolUse"] if g.get("matcher") == "*" for h in g["hooks"])
    assert pulse["async"] is True and "hook session-pulse" in pulse["command"]
    ev_hook = next(h for g in d["hooks"]["Notification"] for h in g["hooks"])
    assert ev_hook["timeout"] == 10 and "async" not in ev_hook
    first = s.read_text()
    baks = set(tmp_path.glob("settings.json.bak-*"))
    r2 = runner.invoke(app, ["install-hooks", "--settings", str(s)])
    assert "already installed" in r2.stdout
    assert s.read_text() == first and set(tmp_path.glob("settings.json.bak-*")) == baks


def test_install_hooks_new_hooks_added_to_older_install(tmp_path):
    s = tmp_path / "settings.json"
    old = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/old/myvec hook session-start"}]}],
                     "PostToolUse": [{"matcher": "Edit|Write|MultiEdit",
                                      "hooks": [{"type": "command", "command": "/old/myvec hook post-edit"}]}]}}
    s.write_text(json.dumps(old))
    changed, backup = hooks.install(s, "/new/myvec")
    assert changed and backup is not None
    d = json.loads(s.read_text())
    flat = [h["command"] for gs in d["hooks"].values() for g in gs for h in g["hooks"]]
    assert not any("/old/" in c for c in flat)
    assert sum("hook session-event" in c for c in flat) == 6 and sum("hook session-pulse" in c for c in flat) == 1


# ---------------------------------------------------------------- CLI

def _populate_db_with_sessions():
    db = make_db()
    now = time.time()
    fold_events(db, [ev("prompt", now - 3600, sid="quiet-session-id", cwd="/x", branch="main"),
                     ev("ask", now - 5, sid="waiting-session", q="Which database?", branch="t/E2-01-x"),
                     ev("start", now - 3 * 86400, sid="ancient"), ev("end", now - 3 * 86400 + 1, sid="ancient")],
                now=now)
    db.close()


def test_sessions_json_shape_and_states():
    _populate_db_with_sessions()
    r = runner.invoke(app, ["sessions", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    by = {d["session_id"]: d for d in data}
    assert set(by) == {"quiet-session-id", "waiting-session"}
    for d in data:
        assert set(sess.COLUMNS) <= set(d) and {"effective_state", "age_s"} <= set(d)
    assert by["quiet-session-id"]["effective_state"] == "idle" and by["quiet-session-id"]["state"] == "working"
    assert by["waiting-session"]["effective_state"] == "waiting" and by["waiting-session"]["question"] == "Which database?"
    assert by["waiting-session"]["age_s"] >= 5
    assert [d["session_id"] for d in data][0] == "waiting-session"   # newest activity first


def test_sessions_all_includes_old_ended():
    _populate_db_with_sessions()
    data = json.loads(runner.invoke(app, ["sessions", "--json", "--all"]).stdout)
    assert "ancient" in {d["session_id"] for d in data}


def test_sessions_table_output():
    _populate_db_with_sessions()
    out = runner.invoke(app, ["sessions"]).stdout
    assert "waiting-" in out and "waiting" in out and "idle" in out and "Which database?" in out
    assert "t/E2-01-x" in out


def test_sessions_folds_journal_when_no_daemon(monkeypatch, ws):
    from conftest import do_sync
    from myvec import daemon_client

    do_sync(ws)
    monkeypatch.setattr(daemon_client, "read_daemon", lambda *a, **k: None)
    write_journal([ev("prompt", time.time() - 2, sid="fresh-1"), ev("stop", time.time() - 1, sid="fresh-1", msg="hey")])
    data = json.loads(runner.invoke(app, ["sessions", "--json"]).stdout)
    assert data[0]["session_id"] == "fresh-1" and data[0]["effective_state"] == "waiting"
    assert not config.session_events_path().exists()


def test_sessions_does_not_fold_when_daemon_runs(monkeypatch, ws):
    from conftest import do_sync
    from myvec import daemon_client

    do_sync(ws)
    monkeypatch.setattr(daemon_client, "read_daemon", lambda *a, **k: {"port": 1})
    write_journal([ev("prompt", time.time(), sid="fresh-2")])
    assert json.loads(runner.invoke(app, ["sessions", "--json"]).stdout) == []
    assert config.session_events_path().exists()


def test_sessions_without_database():
    r = runner.invoke(app, ["sessions", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout) == []
    assert "no sessions" in runner.invoke(app, ["sessions"]).stdout


# ---------------------------------------------------------------- ticket binding (E2-02, ADR-009)

def ticket_env(tmp_path, ids=("E1", "E1-02", "E2-02")):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    db = make_db()
    db.execute("INSERT OR REPLACE INTO projects(path, root) VALUES('proj', ?)", (str(root),))
    for t in ids:
        db.execute("INSERT OR REPLACE INTO tickets(project_path, id, title, status) VALUES('proj', ?, ?, 'todo')",
                   (t, f"title {t}"))
    return db, root


def bound(db, events):
    fold_events(db, events)
    return {r["session_id"]: r for r in sess.list_sessions(db, now=T0 + 1000)}


def test_branch_binds_longest_known_id(tmp_path):
    db, root = ticket_env(tmp_path)
    rows = bound(db, [ev("start", T0, "a", cwd=str(root), branch="t/E1-02-rrf-weights"),
                      ev("start", T0, "b", cwd=str(root), branch="t/foo"),
                      ev("start", T0, "c", cwd=str(root), branch="main")])
    assert (rows["a"]["ticket_id"], rows["a"]["ticket_source"], rows["a"]["ticket_project"]) == ("E1-02", "branch", "proj")
    assert rows["a"]["ticket_title"] == "title E1-02" and rows["a"]["ticket_status"] == "todo"
    assert rows["b"]["ticket_id"] is None and rows["c"]["ticket_source"] is None


def test_worktree_dir_binds_and_external_worktree_maps_to_main_project(tmp_path):
    db, root = ticket_env(tmp_path)
    wt = root / ".worktrees" / "E2-02"
    wt.mkdir(parents=True)
    ext = tmp_path / "ext-wt"
    ext.mkdir()
    gd = root / ".git" / "worktrees" / "n"
    gd.mkdir(parents=True)
    (gd / "commondir").write_text("../..\n")
    (ext / ".git").write_text(f"gitdir: {gd}\n")
    assert sess.main_root_of(str(ext)) == str(root)
    assert sess.main_root_of(str(root)) is None and sess.main_root_of(None) is None
    rows = bound(db, [ev("start", T0, "a", cwd=str(wt), wt=str(wt), branch="work"),
                      ev("start", T0, "b", cwd=str(ext), wt=str(ext), branch="t/E1-02-x")])
    assert (rows["a"]["ticket_id"], rows["a"]["ticket_source"]) == ("E2-02", "worktree")
    assert rows["b"]["project_path"] == "proj" and rows["b"]["ticket_id"] == "E1-02"


def test_claim_overrides_and_release_restores(tmp_path):
    from myvec import ticketops
    db, root = ticket_env(tmp_path)
    fold_events(db, [ev("start", T0, "a", cwd=str(root), branch="t/E1-02-x")])
    ticketops.record_claim("a", "E2-02", root)
    r = sess.list_sessions(db, now=T0 + 1000)[0]   # no new hook event
    assert (r["ticket_id"], r["ticket_source"]) == ("E2-02", "claim")
    ticketops.record_claim("a", None, root)
    r = sess.list_sessions(db, now=T0 + 1000)[0]
    assert (r["ticket_id"], r["ticket_source"]) == ("E1-02", "branch")
    assert sess.list_sessions(db, now=T0 + 1000, ticket=("proj", "E1-02"))
    assert not sess.list_sessions(db, now=T0 + 1000, ticket=("proj", "E2-02"))


def test_by_ticket_and_get_ticket_sessions(tmp_path):
    from myvec import ticketdb
    db, root = ticket_env(tmp_path)
    fold_events(db, [ev("start", T0, "old", cwd=str(root), branch="t/E1-02-x"),
                     ev("start", T0 + 500, "new", cwd=str(root), branch="t/E1-02-y"),
                     ev("stop", T0 + 600, "new", cwd=str(root))])
    g = sess.by_ticket(db, "proj", now=T0 + 1000)
    assert [s["session_id"] for s in g["E1-02"]] == ["new", "old"]
    assert g["E1-02"][0]["effective_state"] == "waiting"
    db.execute("INSERT OR REPLACE INTO ticket_dirs(project_path, dir, n_tickets) VALUES('proj', 'docs/todo', 3)")
    t = ticketdb.get_ticket(db, "proj", "E1-02")
    assert isinstance(t["sessions"], list)
    assert ticketdb.get_ticket(db, "proj", "E2-02")["sessions"] == []
    tree = ticketdb.project_tickets(db, "proj")
    assert all("sessions" in n for n in tree["nodes"])


def test_cli_sessions_ticket_column(tmp_path, monkeypatch):
    db, root = ticket_env(tmp_path)
    fold_events(db, [ev("start", time.time(), "abcdef123", cwd=str(root), branch="t/E1-02-x")])
    db.commit()
    monkeypatch.chdir(root)
    out = runner.invoke(app, ["sessions"]).output
    assert "E1-02" in out
    j = json.loads(runner.invoke(app, ["sessions", "--json"]).output)
    assert j[0]["ticket_source"] == "branch" and j[0]["ticket_project"] == "proj"
    assert "abcdef12" in runner.invoke(app, ["sessions", "--ticket", "E1-02"]).output
    assert "abcdef12" not in runner.invoke(app, ["sessions", "--ticket", "E2-02"]).output


def test_claim_marker_in_cli_and_other_project_claim(tmp_path, monkeypatch):
    from myvec import ticketops
    db, root = ticket_env(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    db.execute("INSERT INTO projects(path, root) VALUES('other', ?)", (str(other),))
    db.execute("INSERT INTO tickets(project_path, id, title, status) VALUES('other', 'X-01', 'x', 'todo')")
    fold_events(db, [ev("start", time.time(), "abcdef123", cwd=str(root), branch="main")])
    db.commit()
    ticketops.record_claim("abcdef123", "E2-02", root)
    monkeypatch.chdir(root)
    assert "E2-02*" in runner.invoke(app, ["sessions"]).output
    ticketops.record_claim("abcdef123", "X-01", other)   # claim made in another project
    r = sess.list_sessions(db)[0]
    assert (r["ticket_project"], r["ticket_id"], r["ticket_title"]) == ("other", "X-01", "x")
    assert sess.by_ticket(db, "other")["X-01"][0]["session_id"] == "abcdef123"
    assert "X-01" not in sess.by_ticket(db, "proj")
    ticketops.record_claim("abcdef123", "E1", tmp_path / "nowhere")   # unregistered root -> session's project
    r = sess.list_sessions(db)[0]
    assert (r["ticket_project"], r["ticket_id"]) == ("proj", "E1")


def test_cli_ticket_filter_outside_project_and_in_external_worktree(tmp_path, monkeypatch):
    db, root = ticket_env(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "i"], check=True)
    ext = tmp_path / "ext"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "t/E1-02-x", str(ext)], check=True)
    fold_events(db, [ev("start", time.time(), "abcdef123", cwd=str(ext), wt=str(ext), branch="t/E1-02-x")])
    db.commit()
    monkeypatch.chdir(ext)
    assert "abcdef12" in runner.invoke(app, ["sessions", "--ticket", "E1-02"]).output
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    res = runner.invoke(app, ["sessions", "--ticket", "E1-02"])
    assert res.exit_code == 1


def test_branch_beats_worktree_and_old_ended_excluded(tmp_path):
    db, root = ticket_env(tmp_path)
    wt = root / ".worktrees" / "E2-02"
    wt.mkdir(parents=True)
    fold_events(db, [ev("start", T0, "a", cwd=str(wt), wt=str(wt), branch="t/E1-02-x"),
                     ev("start", T0, "old", cwd=str(root), branch="t/E1-02-x"),
                     ev("end", T0 + 1, "old", reason="other")], now=T0 + 10)
    rows = {r["session_id"]: r for r in sess.list_sessions(db, now=T0 + 100)}
    assert (rows["a"]["ticket_id"], rows["a"]["ticket_source"]) == ("E1-02", "branch")
    late = T0 + 3 * 86400
    ids = [s["session_id"] for s in sess.by_ticket(db, "proj", now=late).get("E1-02", [])]
    assert "old" not in ids and "a" in ids


def test_main_root_of_edge_cases(tmp_path):
    root = tmp_path / "main"
    gd = root / ".git" / "worktrees" / "n"
    gd.mkdir(parents=True)
    (gd / "commondir").write_text("../..")
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / ".git").write_text(os.path.relpath(gd, rel).join(["gitdir: ", "\n"]))
    assert sess.main_root_of(str(rel)) == str(root.resolve())
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("garbage")
    assert sess.main_root_of(str(broken)) is None
    (broken / ".git").write_bytes(b"")
    assert sess.main_root_of(str(broken)) is None
    sub = tmp_path / "sub"
    sub.mkdir()
    sd = tmp_path / "modules" / "sub"
    sd.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {sd}\n")   # submodule: no commondir
    assert sess.main_root_of(str(sub)) is None


def test_null_project_row_is_resolved_on_read(tmp_path):
    db, root = ticket_env(tmp_path)
    fold_events(db, [ev("start", T0, "a", cwd=str(root), branch="t/E1-02-x")])
    db.execute("UPDATE sessions SET project_path=NULL")
    r = sess.list_sessions(db, now=T0 + 100)[0]
    assert (r["project_path"], r["ticket_id"]) == ("proj", "E1-02")


def test_wait_s_and_n_waiting(tmp_path):
    from myvec import ticketdb
    db, root = ticket_env(tmp_path)
    fold_events(db, [ev("start", T0, "w", cwd=str(root), branch="t/E1-02-x"), ev("stop", T0 + 100, "w", cwd=str(root)),
                     ev("start", T0 + 200, "r", cwd=str(root), branch="t/E1-02-y")])
    g = sess.by_ticket(db, "proj", now=T0 + 400)["E1-02"]
    by = {s["session_id"]: s for s in g}
    assert by["w"]["wait_s"] == 300 and by["r"]["wait_s"] is None and by["w"]["cwd"] == str(root)
    db.execute("INSERT OR REPLACE INTO ticket_dirs(project_path, dir, n_tickets) VALUES('proj', 'docs/todo', 3)")
    nodes = ticketdb.project_tickets(db, "proj", 15.0)["nodes"]
    assert all("wait_s" in x for n in nodes for x in n["sessions"])
    rows = ticketdb.projects_summary(db, include_empty=True)
    assert [r["n_waiting"] for r in rows if r["project_path"] == "proj"] == [1]
    assert sess.wait_s({"effective_state": "working"}) is None


# ------------------------------------------------------------ E2-06: AskUserQuestion options (ADR-014)

def _ask(monkeypatch, questions, sid="s"):
    run_hook(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": sid,
                           "cwd": "/x", "tool_input": {"questions": questions}})
    return journal()[-1]


def test_ask_single_question_records_option_labels(monkeypatch):
    r = _ask(monkeypatch, [{"question": "Колір?", "options": [{"label": "Червоний"}, {"label": "Синій"}]}])
    assert r["qn"] == 1 and r["qo"] == ["Червоний", "Синій"] and "qm" not in r


def test_ask_multi_question_or_multiselect_has_no_options(monkeypatch):
    opts = [{"label": "a"}, {"label": "b"}]
    r = _ask(monkeypatch, [{"question": "1", "options": opts}, {"question": "2", "options": opts}])
    assert r["qn"] == 2 and "qo" not in r
    r = _ask(monkeypatch, [{"question": "1", "multiSelect": True, "options": opts}])
    assert r["qn"] == 1 and r["qm"] == 1 and "qo" not in r


def test_ask_labels_masked_clipped_and_capped(monkeypatch):
    opts = [{"label": "x" * 300}] + [{"label": f"o{i}"} for i in range(12)]
    opts[1] = {"label": "-----BEGIN RSA PRIVATE KEY-----\nabc"}
    r = _ask(monkeypatch, [{"question": "q", "options": opts}])
    assert len(r["qo"]) == hooks.MAX_OPTS and len(r["qo"][0]) == hooks.MAX_OPT
    assert "qo" in r and r["qo"][1] is None


def test_ask_long_record_drops_options_last():
    rec = {"ts": 1.0, "ev": "ask", "sid": "s", "q": "q" * 1000, "cwd": "/" + "c" * 3600, "qn": 1, "qo": ["y" * 120] * 8}
    hooks._append_session_event(rec)
    (r,) = journal()
    assert "qo" not in r and r["qn"] == 1
    assert len(config.session_events_path().read_bytes()) <= hooks.MAX_LINE + 1


def test_fold_stores_options_and_clears_on_state_change():
    db = make_db()
    sess.ensure_table(db)
    p = config.session_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": 100, "ev": "ask", "sid": "a", "cwd": "/x", "q": "Q", "qn": 1, "qo": ["A", None]}) + "\n")
    sess.fold(db, now=200)
    d = [x for x in sess.list_sessions(db, 200) if x["session_id"] == "a"][0]
    assert d["question_options"] == {"n": 1, "multi": False, "labels": ["A", None]}
    p.write_text(json.dumps({"ts": 150, "ev": "answered", "sid": "a"}) + "\n")
    sess.fold(db, now=200)
    d = [x for x in sess.list_sessions(db, 200) if x["session_id"] == "a"][0]
    assert d["question_options"] is None


def test_old_v7_sessions_table_gets_question_options(tmp_path):
    db = make_db()
    db.execute("ALTER TABLE sessions DROP COLUMN question_options")
    db.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    db.commit()
    db.close()
    db = make_db()
    assert "question_options" in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}


def test_short_cwd_and_project_label(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(sess.Path, "home", classmethod(lambda cls: home))
    assert sess.short_cwd(str(home / "projects" / "x")) == "~/projects/x"
    assert sess.short_cwd(str(home)) == "~" and sess.short_cwd("/") == "/" and sess.short_cwd("") is None
    assert sess.short_cwd("/opt/y") == "/opt/y"
    db = make_db()
    now = T0 + 100
    fold_events(db, [ev("prompt", now - 5, sid="free", cwd=str(home / "projects" / "free")),
                     ev("prompt", now - 5, sid="root", cwd="/")], now=now)
    got = {r["session_id"]: r for r in sess.list_sessions(db, now, 15)}
    assert got["free"]["project_path"] is None and got["free"]["project_label"] == "~/projects/free"
    assert got["root"]["project_label"] == "/"
