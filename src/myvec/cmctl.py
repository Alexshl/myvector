"""Thin wrapper over the claude-manager CLI `cm` (ADR-010).

Only `subprocess.run([abs_cm, ...], shell=False)` with a timeout and no inherited stdin. The answer text goes to
`cm send <id> -` through stdin, never argv (invisible in `ps`). Output returned to callers is a masked, trimmed tail.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from myvec import config, filters

TAIL = 300
UUID_FIND = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LS_TTL = 10.0
UPDATE_HINT = "оновіть claude-manager: `cm update`"

_lock = threading.Lock()
CAPS_TTL = 60.0
_caps_cache: dict[tuple[str, float], tuple[float, dict]] = {}
_ls_cache: dict[str, tuple[float, tuple[bool, dict]]] = {}


def cm_path() -> str:
    return os.path.expanduser(config.load_config().cm_path)


def _tail(text: str, secret: str | None = None) -> str:
    if secret:
        text = text.replace(secret, "")
    text = filters.mask_secrets(text.strip())[0]
    return text[-TAIL:]


def run(argv: list[str], input: str | None = None, timeout: float | None = None) -> tuple[int | None, str, str]:
    """(exit code | None, stdout tail, stderr tail). None = cm missing / timed out / not runnable."""
    path = cm_path()
    if timeout is None:
        timeout = float(config.load_config().cm_timeout_s)
    kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        p = subprocess.run([path, *argv], capture_output=True, text=True, timeout=timeout, shell=False, **kw)
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except (OSError, ValueError) as e:
        return None, "", type(e).__name__
    return p.returncode, _tail(p.stdout, input), _tail(p.stderr, input)


def capabilities() -> dict:
    """{available, send_wait, answer, hint}; cached by (cm_path, mtime). `answer` = `cm answer` exists (ADR-014)."""
    path = cm_path()
    try:
        key = (path, os.stat(path).st_mtime)
    except OSError:
        return {"available": False, "send_wait": False, "answer": False, "hint": f"cm не знайдено: {path}"}
    with _lock:
        hit = _caps_cache.get(key)
        if hit and time.monotonic() - hit[0] < CAPS_TTL:
            return hit[1]
    code_s, out_s, _ = run(["send", "--help"], timeout=15)
    code_w, _, _ = run(["who", "--help"], timeout=15)
    ok = code_s == 0 and code_w == 0
    caps = {"available": ok, "send_wait": ok and "--wait" in out_s, "answer": False,
            "hint": "" if ok else UPDATE_HINT}
    if ok and not caps["send_wait"]:
        caps.update(available=False, hint=UPDATE_HINT)
    if caps["available"]:
        caps["answer"] = run(["answer", "--help"], timeout=15)[0] == 0
    with _lock:
        _caps_cache.clear()
        _caps_cache[key] = (time.monotonic(), caps)
    return caps


def _json(out: str):
    try:
        return json.loads(out)
    except ValueError:
        return None


def ls(now: float | None = None) -> tuple[bool, dict]:
    """(ok, {session_id: {alive, window, control}}); cached 10 s; failure -> (False, {})."""
    path = cm_path()
    now = time.time() if now is None else now
    with _lock:
        hit = _ls_cache.get(path)
        if hit and now - hit[0] < LS_TTL:
            return hit[1]
    res = _ls_raw() if capabilities()["available"] else (False, {})
    with _lock:
        _ls_cache[path] = (now, res)
    return res


def _ls_raw() -> tuple[bool, dict]:
    try:
        p = subprocess.run([cm_path(), "ls", "--json"], capture_output=True, text=True, shell=False,
                           stdin=subprocess.DEVNULL, timeout=min(30.0, float(config.load_config().cm_timeout_s)))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False, {}
    rows = _json(p.stdout) if p.returncode == 0 else None
    if isinstance(rows, dict):
        rows = rows.get("sessions", [])
    if not isinstance(rows, list):
        return False, {}
    return True, {r["session_id"]: {"alive": bool(r.get("alive", r.get("state") == "alive")),
                                    "window": r.get("window"), "control": r.get("control")}
                  for r in rows if isinstance(r, dict) and r.get("session_id")}


def who(sid: str) -> dict | None:
    """`cm who <id> --json` parsed; None when cm fails. stdout must be whole, so it is read untrimmed."""
    try:
        p = subprocess.run([cm_path(), "who", sid, "--json"], capture_output=True, text=True, shell=False,
                           stdin=subprocess.DEVNULL, timeout=min(30.0, float(config.load_config().cm_timeout_s)))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    d = _json(p.stdout) if p.returncode == 0 else None
    return d if isinstance(d, dict) else None


def send(sid: str, text: str, wait: int | None = None) -> tuple[int | None, str, str]:
    argv = ["send", sid, "-"] + (["--wait", str(wait)] if wait else [])
    return run(argv, input=text)


def answer(sid: str, option: int | None = None, text: str | None = None,
           expect: str | None = None) -> tuple[int | None, str, str]:
    """`cm answer`: `--option N [--expect-label L]` or `--text -` (text via stdin, never argv). For a masked label
    (`•••`) only the unmasked prefix before the mask is passed: the rest cannot match the real screen."""
    if text is not None:
        return run(["answer", sid, "--text", "-"], input=text)
    argv = ["answer", sid, "--option", str(int(option))]
    prefix = (expect or "").split("•••")[0].strip()
    if prefix:
        argv += ["--expect-label", prefix]
    return run(argv)


def restart(sid: str) -> tuple[int | None, str, str]:
    return run(["restart", sid])


def resume(sid: str) -> tuple[int | None, str, str]:
    return run(["resume", sid])


_proc_cache: tuple[float, set[str]] = (float("-inf"), set())


def process_session_ids(now: float | None = None) -> set[str]:
    """UUIDs that appear in the argument lists of running processes (`claude --resume <id>`...), cached 10 s.
    Held-by-terminal is judged by the exact session id, never by cwd."""
    global _proc_cache
    now = time.monotonic() if now is None else now
    if now - _proc_cache[0] < LS_TTL:
        return _proc_cache[1]
    try:
        out = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True, shell=False,
                             stdin=subprocess.DEVNULL, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError, ValueError):
        out = ""
    ids = set(UUID_FIND.findall(out.lower()))
    _proc_cache = (now, ids)
    return ids
