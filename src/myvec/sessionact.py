"""The "resume session" action (ADR-010): the server decides what `cm` calls to make; the answer text is never logged."""
from __future__ import annotations

import re
import threading
import weakref

from myvec import cmctl

MAX_TEXT = 16000
WAIT_S = 30
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()   # a lock lives while an action holds it
_locks_guard = threading.Lock()


def on_answer(project: str | None, ticket_id: str | None, session_id: str, text: str) -> None:
    """Seam for E3 (inbox, ADR-001): called after the answer was delivered. No-op for now."""


def decide(who: dict | None, row: dict, caps: dict) -> str:
    """send | restart_send | held | resume_send | unavailable."""
    if not caps.get("available") or who is None:
        return "unavailable"
    if who.get("managed"):
        return "send" if who.get("alive") else "restart_send"
    if any(not h.get("ours") for h in who.get("holders") or []):
        return "held"
    return "resume_send"


def has_yes_label(qo) -> bool:
    """`cm` reads any screen with an option starting with "Yes" as a permission prompt and refuses it (exit 4)."""
    return isinstance(qo, dict) and any(isinstance(x, str) and x.startswith("Yes") for x in qo.get("labels") or [])


def answer_kind(row: dict, caps: dict, mode: str) -> str | None:
    """'auq' when the session waits on a single plain AskUserQuestion that `cm answer` can answer (ADR-014)."""
    qo = row.get("question_options")
    if has_yes_label(qo):
        return None
    if (mode == "send" and caps.get("answer") and row.get("state") == "waiting" and row.get("question_at")
            and isinstance(qo, dict) and qo.get("n") == 1 and not qo.get("multi")):
        return "auq"
    return None


def approx_who(sid: str, row: dict, ls_ok: bool, ls_map: dict, procs=()) -> dict | None:
    """Cheap stand-in for `cm who` in lists: one `cm ls`; held only when a process has exactly this session id in its args."""
    if not ls_ok:
        return None
    info = ls_map.get(sid)
    if info:
        return {"managed": True, "alive": info["alive"], "holders": []}
    held = row.get("state") != "ended" and sid.lower() in (procs or ())
    return {"managed": False, "alive": False, "holders": [{"ours": False}] if held else []}


def valid_text(text) -> str | None:
    """Error message for a bad answer text, None when fine (empty = just resume)."""
    if not isinstance(text, str):
        return "text must be a string"
    if len(text) > MAX_TEXT:
        return f"text longer than {MAX_TEXT} characters"
    if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text):
        return "text contains control characters"
    return None


def fork_cmd(sid: str) -> str:
    return f"cm resume {sid} --fork"


def _res(ok: bool, http: int, action: str, mode: str, message: str, code=None, sid: str | None = None) -> dict:
    d = {"ok": ok, "http": http, "action": action, "mode": mode, "code": code, "message": message}
    if mode == "held" and sid:
        d["fork_cmd"] = fork_cmd(sid)
    return d


def _send_result(res: tuple, action: str, mode: str, sid: str) -> dict:
    code, _, err = res
    if code == 0:
        return _res(True, 200, action, mode, "надіслано", 0)
    if code == 4:
        restored = " (сесію відновлено, але" if action in ("restart_send", "resume_send") else ""
        msg = "busy: сесію відновлено, але на екрані діалог: відповідай у терміналі" if restored else \
            "busy: на екрані діалог, відповідай у терміналі"
        return _res(False, 409, action, mode, msg, 4)
    if code == 3:
        return _res(False, 409, action, mode, "dead: сесія не керована або мертва", 3)
    if code is None:
        return _res(False, 503, action, mode, f"cm недоступний: {err}", None)
    return _res(False, 502, action, mode, f"cm помилка: {err}", code)


def _answer_result(res: tuple, mode: str, sid: str) -> dict:
    code, _, err = res
    if code == 0:
        return _res(True, 200, "answer", mode, "надіслано", 0)
    if code == 4:
        return _res(False, 409, "answer", mode,
                    "busy: на екрані не віджет питання (або інше питання): відповідай у терміналі", 4)
    if code == 3:
        return _res(False, 409, "answer", mode, "dead: сесія не керована або мертва", 3)
    if code == 5:
        return _res(False, 502, "answer", mode, "відповідь могла пройти — перевір термінал", 5)
    if code is None:
        return _res(False, 503, "answer", mode, f"cm недоступний: {err}", None)
    return _res(False, 502, "answer", mode, f"cm помилка: {err}", code)


def execute(sid: str, row: dict, text: str, option: int | None = None) -> dict:
    """Run the action. Never raises; returns {ok, http, action, mode, code, message, fork_cmd?}. One action per session."""
    with _locks_guard:
        lock = _locks.get(sid)
        if lock is None:
            lock = _locks[sid] = threading.Lock()
    if not lock.acquire(blocking=False):
        return _res(False, 409, "none", "busy", "дія над цією сесією вже виконується")
    try:
        return _execute(sid, row, text, option)
    finally:
        lock.release()


def _execute(sid: str, row: dict, text: str, option: int | None = None) -> dict:
    caps = cmctl.capabilities()
    mode = "unavailable"
    for attempt in range(2):   # a "dead" answer from send re-derives the mode once
        if not caps.get("available"):
            return _res(False, 503, "none", "unavailable", caps.get("hint") or "cm недоступний")
        who = cmctl.who(sid)
        if who is None:
            return _res(False, 502, "none", "unavailable", "`cm who` завершився помилкою або не відповів")
        mode = decide(who, row, caps)
        if mode == "held":
            return _res(False, 409, "none", mode, "сесію тримає інший процес (термінал): відкрий форк", sid=sid)
        kind = answer_kind(row, caps, mode)
        if option is not None and kind != "auq":
            return _res(False, 409, "none", mode, "сесія не чекає на питання з варіантами")
        if kind == "auq":
            labels = row["question_options"].get("labels") or []
            if option is not None:
                if not 1 <= option <= len(labels) or not labels[option - 1]:
                    return _res(False, 400, "none", mode, "варіант недоступний: відповідай у терміналі")
                return _answer_result(cmctl.answer(sid, option=option, expect=labels[option - 1]), mode, sid)
            if not text:
                return _res(True, 200, "noop", mode, "сесія чекає на питання, нічого надсилати")
            if "\n" in text.strip():
                return _res(False, 400, "none", mode,
                            "багаторядкова відповідь на питання не підтримується: перенос рядка відправив би її передчасно; "
                            "напиши в один рядок")
            r = _answer_result(cmctl.answer(sid, text=text.strip()), mode, sid)
            if r["code"] == 3 and attempt == 0:
                continue
            return r
        if mode == "send":
            if not text:
                return _res(True, 200, "noop", mode, "сесія жива, нічого надсилати")
            r = _send_result(cmctl.send(sid, text), "send", mode, sid)
            if r["code"] == 3 and attempt == 0:
                continue
            return r
        if mode == "restart_send":
            res = cmctl.restart(sid)
            if res[0] != 0:
                return _send_result(res, "restart", mode, sid)
            if not text:
                return _res(True, 200, "restart", mode, "сесію перезапущено", 0)
            return _send_result(cmctl.send(sid, text, wait=WAIT_S), "restart_send", mode, sid)
        res = cmctl.resume(sid)   # resume_send
        if res[0] != 0:
            return _send_result(res, "resume", mode, sid)
        if not text:
            return _res(True, 200, "resume", mode, "сесію відновлено", 0)
        return _send_result(cmctl.send(sid, text, wait=WAIT_S), "resume_send", mode, sid)
    return _res(False, 409, "send", mode, "dead: сесія не керована або мертва", 3)
