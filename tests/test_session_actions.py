import json
import time

import pytest
from starlette.testclient import TestClient

from fakecm import FakeCm
from myvec import cmctl, config, daemon, sessionact
from myvec import sessions as sess

PORT = 7878
TOKEN = "test-token"
H = {"X-CSRF-Token": TOKEN}
SID = "11111111-2222-3333-4444-555555555555"
SID2 = "22222222-2222-3333-4444-555555555555"
MARK = "MARKER-ANSWER-7731"
T0 = time.time() - 1000


@pytest.fixture
def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(cmctl, "process_session_ids", lambda *a: set())   # never the real `ps`
    f = FakeCm(tmp_path / "bin")
    config.ensure_home()
    (config.home() / "config.toml").write_text(f'cm_path = "{f.path}"\ncm_timeout_s = 5\n')
    cmctl._caps_cache.clear()
    cmctl._ls_cache.clear()
    sessionact._locks.clear()
    return f


@pytest.fixture
def state(cm):
    st = daemon.DaemonState(PORT, TOKEN)
    db = st.db()
    sess.ensure_table(db)
    ev = [{"ts": T0, "ev": "start", "sid": SID, "cwd": "/tmp/p"},
          {"ts": T0 + 10, "ev": "stop", "sid": SID},
          {"ts": T0 + 500, "ev": "start", "sid": SID2, "cwd": "/tmp/p"},
          {"ts": T0 + 510, "ev": "stop", "sid": SID2}]
    p = config.session_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e) + "\n" for e in ev))
    sess.fold(db, now=T0 + 600)
    yield st
    st.close()


@pytest.fixture
def client(state):
    return TestClient(daemon.create_app(state), base_url=f"http://127.0.0.1:{PORT}")


ALIVE = {"managed": True, "alive": True, "holders": []}
DEAD = {"managed": True, "alive": False, "holders": []}
FREE = {"managed": False, "alive": False, "holders": []}
HELD = {"managed": False, "alive": False, "holders": [{"pid": 5, "remote_control": False, "ours": False}]}
CAPS = {"available": True}


# ------------------------------------------------------------ decide (pure)

@pytest.mark.parametrize("who,mode", [(ALIVE, "send"), (DEAD, "restart_send"), (HELD, "held"), (FREE, "resume_send"),
                                      ({**FREE, "holders": [{"ours": True}]}, "resume_send"), (None, "unavailable")])
def test_decide(who, mode):
    assert sessionact.decide(who, {}, CAPS) == mode


def test_decide_unavailable_without_caps():
    assert sessionact.decide(ALIVE, {}, {"available": False}) == "unavailable"


def test_approx_who():
    assert sessionact.approx_who("a", {}, False, {}) is None
    assert sessionact.approx_who("a", {}, True, {"a": {"alive": True}})["alive"] is True
    # held only when a process carries exactly this session id (never by cwd, never by "not ended")
    assert sessionact.decide(sessionact.approx_who("a", {"state": "waiting"}, True, {}, {"a"}), {}, CAPS) == "held"
    assert sessionact.decide(sessionact.approx_who("a", {"state": "waiting"}, True, {}, {"b"}), {}, CAPS) == "resume_send"
    assert sessionact.decide(sessionact.approx_who("a", {"state": "waiting", "cwd": "/x"}, True, {}), {}, CAPS) == "resume_send"
    assert sessionact.decide(sessionact.approx_who("a", {"state": "ended"}, True, {}, {"a"}), {}, CAPS) == "resume_send"


def test_valid_text():
    assert sessionact.valid_text("") is None and sessionact.valid_text("a\nb\tc") is None
    assert sessionact.valid_text("x" * 16000) is None
    assert sessionact.valid_text("x" * 16001) and sessionact.valid_text("a\x00b") and sessionact.valid_text("a\x1bb")
    assert sessionact.valid_text(5)


# ------------------------------------------------------------ execute (mocked cm)

def run(cm, who, text=MARK, **cfg):
    cm.set(who=who, **cfg)
    return sessionact.execute(SID, {}, text)


def test_send_branch(cm):
    r = run(cm, ALIVE)
    assert r["ok"] and r["http"] == 200 and r["mode"] == "send" and r["action"] == "send"
    assert cm.verbs() == ["who", "send"]
    assert cm.real()[-1]["stdin"] == MARK and MARK not in " ".join(cm.real()[-1]["argv"])
    assert "--wait" not in cm.real()[-1]["argv"]


def test_send_empty_text_is_noop(cm):
    r = run(cm, ALIVE, text="")
    assert r["ok"] and r["action"] == "noop" and cm.verbs() == ["who"]


def test_restart_send_branch(cm):
    r = run(cm, DEAD)
    assert r["ok"] and r["mode"] == "restart_send"
    assert cm.verbs() == ["who", "restart", "send"]
    assert cm.real()[-1]["argv"] == ["send", SID, "-", "--wait", "30"]
    assert cm.real()[1]["argv"] == ["restart", SID]


def test_resume_send_branch(cm):
    r = run(cm, FREE)
    assert r["ok"] and r["mode"] == "resume_send"
    assert cm.verbs() == ["who", "resume", "send"]
    assert cm.real()[-1]["argv"][-2:] == ["--wait", "30"] and "--fork" not in cm.real()[1]["argv"]


def test_restart_and_resume_without_text_do_not_send(cm):
    assert run(cm, DEAD, text="")["action"] == "restart" and cm.verbs() == ["who", "restart"]
    cm.set(who=FREE)
    assert sessionact.execute(SID, {}, "")["action"] == "resume" and "send" not in cm.verbs()


def test_held_never_sends(cm):
    r = run(cm, HELD)
    assert not r["ok"] and r["http"] == 409 and r["mode"] == "held"
    assert r["fork_cmd"] == f"cm resume {SID} --fork"
    assert cm.verbs() == ["who"]


def test_old_cm_unavailable(cm):
    r = run(cm, ALIVE, old=True)
    assert r["http"] == 503 and "cm update" in r["message"] and cm.verbs() == []


def test_missing_cm_unavailable(cm, tmp_path):
    (config.home() / "config.toml").write_text(f'cm_path = "{tmp_path}/none"\n')
    r = sessionact.execute(SID, {}, "x")
    assert r["http"] == 503 and r["mode"] == "unavailable"


def test_who_failure_is_502_with_clear_message(cm):
    r = run(cm, {}, who_code=1)
    assert r["http"] == 502 and "cm who" in r["message"] and cm.verbs() == ["who"]


def test_restored_but_busy_message(cm):
    r = run(cm, DEAD, codes={"send": 4})
    assert r["http"] == 409 and r["code"] == 4 and "відновлено" in r["message"] and cm.verbs() == ["who", "restart", "send"]


def test_locks_are_released_from_the_registry(cm):
    run(cm, ALIVE)
    import gc
    gc.collect()
    assert len(sessionact._locks) == 0


def test_uuid_fullmatch():
    assert sessionact.UUID_RE.fullmatch(SID) and not sessionact.UUID_RE.fullmatch(SID + "\n")


def test_code4_busy(cm):
    r = run(cm, ALIVE, codes={"send": 4})
    assert not r["ok"] and r["http"] == 409 and r["code"] == 4 and r["message"].startswith("busy")


def test_code3_dead_rederives_mode_once(cm):
    r = run(cm, [ALIVE, DEAD], codes={"send": [3, 0]})
    assert r["ok"] and r["mode"] == "restart_send"
    assert cm.verbs() == ["who", "send", "who", "restart", "send"]


def test_code3_twice_is_dead(cm):
    r = run(cm, ALIVE, codes={"send": 3})
    assert not r["ok"] and r["http"] == 409 and r["code"] == 3 and r["message"].startswith("dead")
    assert cm.verbs().count("who") == 2


def test_code1_error_and_failed_restart(cm):
    assert run(cm, ALIVE, codes={"send": 1})["http"] == 502
    cm.set(who=DEAD, codes={"restart": 1})
    r = sessionact.execute(SID, {}, MARK)
    assert r["http"] == 502 and cm.verbs()[-2:] == ["who", "restart"]


def test_timeout_returns_error(cm):
    (config.home() / "config.toml").write_text(f'cm_path = "{cm.path}"\ncm_timeout_s = 0.5\n')
    r = run(cm, ALIVE, sleep=5)
    assert not r["ok"] and r["http"] == 503 and "timeout" in r["message"]


def test_message_never_contains_text(cm):
    r = run(cm, ALIVE, echo=True, codes={"send": 1})
    assert MARK not in json.dumps(r)


def test_on_answer_is_noop():
    assert sessionact.on_answer("p", "T-1", SID, "x") is None


# ------------------------------------------------------------ API

URL = f"/api/sessions/{SID}/resume"


def test_post_requires_csrf(client, cm):
    assert client.post(URL, json={"text": "x"}).status_code == 403
    assert client.post(URL, json={"text": "x"}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert cm.real() == []


def test_post_bad_and_unknown_id(client, cm):
    assert client.post("/api/sessions/not-a-uuid/resume", json={}, headers=H).status_code == 400
    assert client.post("/api/sessions/..%2Fx/resume", json={}, headers=H).status_code in (400, 404)
    unknown = "99999999-2222-3333-4444-555555555555"
    assert client.post(f"/api/sessions/{unknown}/resume", json={}, headers=H).status_code == 404
    assert cm.real() == []


def test_post_bad_text(client, cm):
    for bad in ("x" * 16001, "a\x00b", 5):
        assert client.post(URL, json={"text": bad}, headers=H).status_code == 400
    assert cm.real() == []


def test_post_send_ok_and_bus_has_no_text(client, state, cm):
    cm.set(who=ALIVE)
    r = client.post(URL, json={"text": MARK}, headers=H)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["mode"] == "send" and "http" not in r.json()
    assert MARK not in r.text
    evs = client.get("/api/events/recent?limit=500").json()["events"]
    ev = [e for e in evs if e["type"] == "session_action"][-1]
    assert ev["session_id"] == SID and ev["action"] == "send" and ev["exit_code"] == 0 and ev["ok"] is True
    assert ev["text_len"] == len(MARK) and MARK not in json.dumps(evs)
    assert MARK not in (config.logs_dir() / "events.jsonl").read_text()


def test_post_held_409_with_fork(client, cm):
    cm.set(who=HELD)
    r = client.post(URL, json={"text": "x"}, headers=H)
    assert r.status_code == 409 and r.json()["fork_cmd"] == f"cm resume {SID} --fork"
    assert "send" not in cm.verbs()


def test_post_busy_409(client, cm):
    cm.set(who=ALIVE, codes={"send": 4})
    r = client.post(URL, json={"text": "x"}, headers=H)
    assert r.status_code == 409 and "busy" in r.json()["message"]


def test_post_old_cm_503(client, cm):
    cm.set(old=True)
    r = client.post(URL, json={"text": "x"}, headers=H)
    assert r.status_code == 503 and "cm update" in r.json()["message"]


def test_post_parallel_same_session_409(state, cm):
    import threading
    cm.set(who=ALIVE, sleep=1.5)
    out = []

    def go():
        c = TestClient(daemon.create_app(state), base_url=f"http://127.0.0.1:{PORT}")
        out.append(c.post(URL, json={"text": "x"}, headers=H).status_code)
    ts = [threading.Thread(target=go) for _ in range(2)]
    ts[0].start()
    time.sleep(0.6)
    ts[1].start()
    for t in ts:
        t.join()
    assert sorted(out) == [200, 409]


def test_on_answer_called_after_delivery(client, cm, monkeypatch):
    got = []
    monkeypatch.setattr(sessionact, "on_answer", lambda *a: got.append(a))
    cm.set(who=ALIVE)
    client.post(URL, json={"text": "hi"}, headers=H)
    assert got and got[0][2:] == (SID, "hi")
    got.clear()
    cm.set(who=ALIVE, codes={"send": 1})
    client.post(URL, json={"text": "hi"}, headers=H)
    assert not got


def test_get_sessions(client, cm):
    cm.set(ls=[{"session_id": SID, "alive": True, "window": "w", "control": "rc"}])
    d = client.get("/api/sessions?all=1").json()
    assert d["node"] and "." not in d["node"] and d["cm"]["available"] is True
    modes = {s["session_id"]: s["mode"] for s in d["sessions"]}
    assert modes == {SID: "send", SID2: "resume_send"}   # no process carries SID2: not held
    assert all("wait_s" in s and "effective_state" in s for s in d["sessions"])


def test_get_sessions_old_cm(client, cm):
    cm.set(old=True)
    d = client.get("/api/sessions").json()
    assert d["cm"]["available"] is False and {s["mode"] for s in d["sessions"]} == {"unavailable"}


def test_waiting_sorted_longest_first(client, cm):
    cm.set(ls=[])
    d = client.get("/api/sessions/waiting").json()
    ids = [s["session_id"] for s in d["sessions"]]
    assert ids == [SID, SID2]   # SID waits since T0+10, SID2 since T0+510
    ws = [s["wait_s"] for s in d["sessions"]]
    assert ws == sorted(ws, reverse=True) and ws[0] > ws[1] > 0


def test_get_endpoints_need_no_csrf(client, cm):
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/sessions/waiting", headers={"Host": "evil.com"}).status_code == 403


def test_get_sessions_held_by_exact_session_id(client, cm, monkeypatch):
    cm.set(ls=[])
    monkeypatch.setattr(cmctl, "process_session_ids", lambda *a: {SID2})
    modes = {s["session_id"]: s["mode"] for s in client.get("/api/sessions").json()["sessions"]}
    assert modes == {SID: "resume_send", SID2: "held"}


def test_process_session_ids_parses_ps(monkeypatch):
    import subprocess

    class P:
        stdout = f"claude --resume {SID.upper()}\n/usr/bin/other --cwd /a/b\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P)
    cmctl._proc_cache = (float("-inf"), set())
    assert cmctl.process_session_ids() == {SID}
    cmctl._proc_cache = (float("-inf"), set())


def test_ticket_card_sessions_have_mode(state, cm, client):
    cm.set(ls=[{"session_id": SID, "alive": True}])
    rows = [{"session_id": SID, "state": "waiting"}, {"session_id": SID2, "state": "waiting"}]
    # exercised through the shared helper path used by /ticket
    caps = cmctl.capabilities()
    ls_ok, m = cmctl.ls()
    assert [sessionact.decide(sessionact.approx_who(r["session_id"], r, ls_ok, m, set()), r, caps) for r in rows] == ["send", "resume_send"]


# ------------------------------------------------------------ E2-06: answer_kind / cm answer (ADR-014)

AUQ_ROW = {"state": "waiting", "question_at": 1.0, "question_options": {"n": 1, "multi": False, "labels": ["Червоний", "Синій", None]}}
ACAPS = {"available": True, "answer": True}


def test_answer_kind():
    assert sessionact.answer_kind(AUQ_ROW, ACAPS, "send") == "auq"
    assert sessionact.answer_kind(AUQ_ROW, ACAPS, "restart_send") is None
    assert sessionact.answer_kind(AUQ_ROW, CAPS, "send") is None
    assert sessionact.answer_kind({**AUQ_ROW, "state": "working"}, ACAPS, "send") is None
    for qo in ({"n": 2, "multi": False, "labels": []}, {"n": 1, "multi": True, "labels": []}, None):
        assert sessionact.answer_kind({**AUQ_ROW, "question_options": qo}, ACAPS, "send") is None


def test_execute_option_and_text_use_cm_answer(cm):
    cm.set(who=ALIVE)
    r = sessionact.execute(SID, AUQ_ROW, "", option=2)
    assert r["ok"] and r["action"] == "answer"
    assert cm.real()[-1]["argv"] == ["answer", SID, "--option", "2", "--expect-label", "Синій"]
    assert sessionact.execute(SID, AUQ_ROW, "Своя відповідь")["ok"]
    last = cm.real()[-1]
    assert last["argv"] == ["answer", SID, "--text", "-"] and last["stdin"] == "Своя відповідь"
    assert "send" not in cm.verbs()
    assert sessionact.execute(SID, AUQ_ROW, "")["action"] == "noop"


def test_execute_answer_refusals(cm):
    cm.set(who=ALIVE)
    assert sessionact.execute(SID, {}, "", option=1)["http"] == 409          # no AUQ
    assert sessionact.execute(SID, AUQ_ROW, "", option=3)["http"] == 400     # masked label: no button
    assert sessionact.execute(SID, AUQ_ROW, "a\nb")["http"] == 400            # multi-line refused
    assert "answer" not in cm.verbs()
    cm.set(who=ALIVE, codes={"answer": 4})
    r = sessionact.execute(SID, AUQ_ROW, "", option=1)
    assert r["http"] == 409 and r["code"] == 4 and "терміналі" in r["message"]
    cm.set(who=ALIVE, codes={"answer": 1})
    assert sessionact.execute(SID, AUQ_ROW, "", option=1)["http"] == 502


def test_no_cm_answer_falls_back_to_send(cm):
    cm.set(who=ALIVE, no_answer=True)
    cmctl._caps_cache.clear()
    assert cmctl.capabilities()["answer"] is False
    r = sessionact.execute(SID, {**AUQ_ROW}, "hi")
    assert r["action"] == "send" and cm.verbs()[-1] == "send"


def test_api_option_validation(client, state, cm):
    cm.set(who=ALIVE)
    ev = {"ts": T0 + 520, "ev": "ask", "sid": SID, "q": "Q?", "qn": 1, "qo": ["A", "B"]}
    p = config.session_events_path()
    p.write_text(json.dumps(ev) + "\n")
    sess.fold(state.db(), now=T0 + 600)
    url = f"/api/sessions/{SID}/resume"
    assert client.post(url, json={"option": 1, "text": "x", "question_at": T0 + 520}, headers=H).status_code == 400
    q = {"question_at": T0 + 520, "label": "A"}
    assert client.post(url, json={"option": 0, **q}, headers=H).status_code == 400
    assert client.post(url, json={"option": "1", **q}, headers=H).status_code == 400
    assert client.post(url, json={"option": 3, **q}, headers=H).status_code == 400
    # stale question: the card showed another question / label than the session has now
    for body in ({"option": 1, "label": "A", "question_at": T0 + 1}, {"option": 1, "label": "Z", "question_at": T0 + 520},
                 {"text": "x", "question_at": T0 + 1}):
        r = client.post(url, json=body, headers=H)
        assert r.status_code == 409 and "питання змінилося" in r.json()["error"], body
    assert client.post(url, json={"option": 1}, headers=H).status_code == 400      # no question_at
    assert "answer" not in cm.verbs()
    r = client.post(url, json={"option": 2, "label": "B", "question_at": T0 + 520}, headers=H)
    assert r.status_code == 200 and r.json()["action"] == "answer"
    assert cm.real()[-1]["argv"][:4] == ["answer", SID, "--option", "2"]


def test_answer_exit_5_yes_labels_and_masked_prefix(cm):
    cm.set(who=ALIVE, codes={"answer": 5})
    r = sessionact.execute(SID, AUQ_ROW, "", option=1)
    assert r["http"] == 502 and "могла пройти" in r["message"]
    yes = {**AUQ_ROW, "question_options": {"n": 1, "multi": False, "labels": ["Yes, do it", "No"]}}
    assert sessionact.answer_kind(yes, ACAPS, "send") is None
    cm.set(who=ALIVE)
    masked = {**AUQ_ROW, "question_options": {"n": 1, "multi": False, "labels": ["Ключ ••• кінець"]}}
    assert sessionact.execute(SID, masked, "", option=1)["ok"]
    assert cm.real()[-1]["argv"][-2:] == ["--expect-label", "Ключ"]
