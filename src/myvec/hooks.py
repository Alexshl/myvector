"""Claude Code hook entrypoints + settings.json merging.

Import-light on purpose (stdlib only): hooks must return in < 100 ms and never fail loudly.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

SESSION_SYNC_AGE = 60.0
EDIT_SYNC_AGE = 30.0
MARK_START = "hook session-start"
MARK_EDIT = "hook post-edit"
MARK_EVENT = "hook session-event"
MARK_PULSE = "hook session-pulse"
MARK_SUBAGENT = "hook subagent-start"
MARK_PRESEARCH = "hook pre-search"
MARKS = (MARK_START, MARK_EDIT, MARK_EVENT, MARK_PULSE, MARK_SUBAGENT, MARK_PRESEARCH)
SEARCH_HINT = (
    "[myvec] Indexed project: search with `myvec search \"<question or identifier>\"` first, identifiers too; "
    "`--kind docs` for README/docs/ADR/tickets, `--by-project` for \"did we do this elsewhere\"; "
    "grep/rg only for exact lines in files already found or a full count."
)
MAX_Q = 1000
MAX_MSG = 500
MAX_OPT = 120
MAX_OPTS = 8
MAX_JOURNAL = 20 * 1024 * 1024
MAX_LINE = 4000   # a single O_APPEND write this small is atomic; concurrent hooks never interleave


def _log(msg: str) -> None:
    try:
        from myvec import config, logutil

        logutil.append(config.logs_dir() / "hook.log", msg)
    except Exception:
        pass


def _read_stdin_json() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = sys.stdin.read()
        obj = json.loads(data) if data.strip() else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _last_sync(project_path: str) -> float:
    from myvec import config

    p = config.db_path()
    if not p.exists():
        return 0.0
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            r = db.execute("SELECT max(last_sync) FROM repos WHERE project_path=? OR project_path LIKE ?",
                           (project_path, project_path + "/%")).fetchone()
            return float(r[0] or 0.0)
        finally:
            db.close()
    except Exception:
        return 0.0


def spawn_sync(cwd: str) -> None:
    """Detached `myvec sync --quiet` (log to logs/hook-sync.log)."""
    from myvec import config, logutil

    out = logutil.open_append(config.logs_dir() / "hook-sync.log")
    subprocess.Popen([sys.executable, "-m", "myvec", "sync", "--quiet"], cwd=cwd, stdin=subprocess.DEVNULL,
                     stdout=out, stderr=out, start_new_session=True, close_fds=True)
    out.close()


def _find_marker_dir(start: str) -> Path | None:
    p = Path(start).resolve()
    for d in [p, *p.parents]:
        if (d / ".myvector.toml").is_file():
            return d
    return None


# ---------------------------------------------------------------- session journal (ADR-007)

def _git_info(cwd: str) -> tuple[str | None, str | None]:
    """(branch, checkout root) by reading .git / HEAD, no git process. A linked worktree has a `.git` file."""
    try:
        p = Path(cwd)
        for d in [p, *list(p.parents)[:30]]:
            g = d / ".git"
            if g.is_dir():
                gitdir = g
            elif g.is_file():
                txt = g.read_text(encoding="utf-8", errors="replace").strip()
                if not txt.startswith("gitdir:"):
                    continue
                gitdir = Path(txt[7:].strip())
                if not gitdir.is_absolute():
                    gitdir = d / gitdir
            else:
                continue
            head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref: refs/heads/"):
                return head[16:], str(d)
            return (head[:8] or None), str(d)
    except OSError:
        pass
    return None, None


def _clip(v, n: int, tail: bool = False) -> str | None:
    if not isinstance(v, str) or not v:
        return None
    return v[-n:] if tail else v[:n]


def _safe_text(v: str | None) -> str | None:
    """Secrets never reach the journal either: hard secrets are dropped, literal values masked (stdlib filters)."""
    if not v:
        return None
    from myvec import filters

    return None if filters.has_hard_secret(v) else filters.mask_secrets(v)[0]


def _question_shape(qs: list) -> dict:
    """qn = number of questions, qm = 1 for multiSelect, qo = option labels (only one non-multiSelect question,
    up to 8, masked; a label holding a hard secret becomes null so the UI offers no button for it)."""
    qs = [x for x in qs if isinstance(x, dict)]
    if not qs:
        return {}
    multi = any(x.get("multiSelect") is True for x in qs)
    out: dict = {"qn": len(qs)}
    if multi:
        out["qm"] = 1
    opts = qs[0].get("options") if len(qs) == 1 and not multi else None
    if isinstance(opts, list) and opts:
        raw = [(o.get("label") if isinstance(o, dict) else o) for o in opts[:MAX_OPTS]]
        labels = [_clip(_safe_text(x), MAX_OPT) if isinstance(x, str) else None for x in raw]
        out["qo"] = [x if x else None for x in labels]
    return out


def _session_record(obj: dict, ev: str) -> dict | None:
    sid = obj.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) else None
    rec: dict = {"ts": round(time.time(), 3), "ev": ev, "sid": sid, "cwd": cwd}
    if obj.get("agent_id"):
        rec["agent"] = 1
    if ev != "tool":
        # the user's prompt is deliberately never read
        q = None
        qs = ((obj.get("tool_input") or {}).get("questions")) if ev == "ask" else None
        qextra: dict = {}
        if isinstance(qs, list):
            q = _clip(_safe_text(" / ".join(str(x.get("question", "")) for x in qs if isinstance(x, dict))), MAX_Q)
            qextra = _question_shape(qs)
        branch, wt = _git_info(cwd) if cwd else (None, None)
        src = obj.get("source")
        # mask / secret-check the FULL text first, truncate after (a secret must not straddle the cut)
        msg = (_clip(_safe_text(obj.get("last_assistant_message")), MAX_MSG, tail=True) if ev == "stop"
               else _clip(_safe_text(obj.get("message")), MAX_MSG) if ev == "notify" else None)
        rec.update(tp=_clip(obj.get("transcript_path"), 500), src=src if isinstance(src, str) else None,
                   ntype=_clip(obj.get("notification_type"), 60), reason=_clip(obj.get("reason"), 60), q=q, msg=msg,
                   bg=1 if (obj.get("background_tasks") or obj.get("session_crons")) else None,
                   pm=_clip(obj.get("permission_mode"), 40), model=_clip(obj.get("model"), 100),
                   title=_clip(obj.get("session_title"), 200), branch=branch, wt=wt, **qextra)
    return {k: v for k, v in rec.items() if v is not None}


def _append_session_event(rec: dict) -> None:
    from myvec import config

    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    while len(line.encode("utf-8")) > MAX_LINE:
        long = [k for k in ("q", "msg") if len(rec.get(k, "")) > 20]
        if long:
            for k in long:
                rec[k] = rec[k][: len(rec[k]) // 2]
        elif "qo" in rec:   # labels go last: no list is better than a cut one
            rec.pop("qo")
        else:
            for k in ("tp", "title", "cwd", "wt", "branch", "model"):
                rec.pop(k, None)
            rec = {k: v for k, v in rec.items() if len(str(v)) < 300}
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    p = config.session_events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if rec.get("ev") == "tool":   # daemon down: cap journal growth, keep state-changing events
        try:
            if p.stat().st_size > MAX_JOURNAL:
                return
        except OSError:
            pass
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _log_session_event(obj: dict, ev: str) -> None:
    try:
        rec = _session_record(obj, ev)
        if rec is not None:
            _append_session_event(rec)
    except Exception:  # noqa: BLE001 - never block the session
        _log("session event error: " + traceback.format_exc())


def _event_name(obj: dict) -> str | None:
    h, tool = obj.get("hook_event_name"), obj.get("tool_name")
    auq = tool == "AskUserQuestion"
    return {"SessionStart": "start", "UserPromptSubmit": "prompt", "Stop": "stop", "Notification": "notify",
            "SessionEnd": "end", "PreToolUse": "ask" if auq else "tool",
            "PostToolUse": "answered" if auq else "tool"}.get(h)


# ---------------------------------------------------------------- inbox notice (ADR-022)

SID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
INBOX_LIST = 10
INBOX_MAX_LINE = 400
STATE_MAX_AGE = 7 * 86400


def _is_linked_worktree(root: str) -> bool:
    """`<root>/.git` is a file whose gitdir has `commondir` (linked worktree); a submodule has none."""
    try:
        dot = Path(root) / ".git"
        if not dot.is_file():
            return False
        first = dot.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
        if not first.startswith("gitdir:"):
            return False
        gitdir = Path(first[7:].strip())
        if not gitdir.is_absolute():
            gitdir = Path(root) / gitdir
        return (gitdir / "commondir").is_file()
    except (OSError, IndexError):
        return False


def _inbox_state_dir() -> Path:
    from myvec import config

    return config.home() / "state" / "inbox_seen"


def _inbox_state_path(sid) -> Path | None:
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None
    return _inbox_state_dir() / sid


def _read_seen(path: Path) -> set[str] | None:
    try:
        return set(path.read_text(encoding="utf-8").split("\n")) - {""}
    except OSError:
        return None


def _write_seen(path: Path, names: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "\n".join(sorted(names)).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _prune_inbox_state() -> None:
    d = _inbox_state_dir()
    if not d.is_dir():
        return
    cutoff = time.time() - STATE_MAX_AGE
    with os.scandir(d) as it:
        for e in it:
            try:
                if e.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(e.path)
            except OSError:
                pass


def _may_have_inbox(mdir: Path) -> bool:
    """Cheap pre-check before any import: default inbox dir has a *.md file, or the marker sets a custom tickets dir."""
    try:
        d = mdir / "docs" / "todo" / "inbox"
        if d.is_dir():
            with os.scandir(d) as it:
                if any(e.name.endswith(".md") for e in it):
                    return True
        return "tickets" in (mdir / ".myvector.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _inbox_line(items: list[dict], where: str) -> str:
    from myvec import tickets

    parts = []
    for it in items[:INBOX_LIST]:
        tid = re.sub(r"[^A-Za-z0-9._-]", "", str(it["ticket"]))[:64]
        if it["kind"] == "lesson":
            frm = re.sub(r"[^A-Za-z0-9._-]", "", str(it.get("from") or ""))[:64]
            parts.append(f"lesson<-{frm}")
            continue
        part = f"{tid} {it['kind']}"
        if it["kind"] == "status" and it.get("requested_status") in tickets.STATUS_KEYS:
            part += f"->{it['requested_status']}"
        parts.append(part)
    tail = (" Lessons: decide per /myvector orchestrator.md (Lessons back-channel), delete the file."
            if any(it["kind"] == "lesson" for it in items) else "")

    def build(n: int) -> str:
        hidden = len(items) - n
        more = f", +{hidden}" if hidden > 0 else ""
        return (f"[myvec] inbox: {len(items)} new in {where} ({', '.join(parts[:n])}{more}). Orchestrator: move each "
                "into the ticket's ## Comments / Log, delete the file, change status yourself (tickets canon, Inbox)."
                + tail)

    n = len(parts)
    while n > 1 and len(build(n)) > INBOX_MAX_LINE:
        n -= 1
    return build(n)[:INBOX_MAX_LINE]


def _inbox_notice(obj: dict, event: str) -> str | None:
    """One line about new inbox files (ids, kinds, requested status; never comment text) or None.
    event: start | prompt. Any error -> None."""
    try:
        if obj.get("agent_id"):
            return None
        cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) and obj.get("cwd") else os.getcwd()
        mdir = _find_marker_dir(cwd)
        if mdir is None:
            return None
        if event == "start":
            try:
                _prune_inbox_state()
            except OSError:
                pass
        if not _may_have_inbox(mdir):
            return None
        _, checkout = _git_info(cwd)
        if checkout and _is_linked_worktree(checkout):
            return None
        from myvec import projects

        m = projects.try_read_marker(mdir)
        if m is None:
            return None
        from myvec import inbox

        items = inbox.pending(mdir, m.tickets_dir)
        items = [it for it in items if "\n" not in it["file"]]
        names = {it["file"] for it in items}
        sp = _inbox_state_path(obj.get("session_id"))
        if event == "start":
            if sp is not None and (names or sp.exists()):
                try:
                    _write_seen(sp, names)
                except OSError:
                    _log("inbox state write error: " + traceback.format_exc())
            shown = items
        else:
            if sp is None:
                return None
            seen = _read_seen(sp)
            if not names and seen is None:
                return None
            shown = [it for it in items if it["file"] not in (seen or set())]
            if seen is None or seen != names:
                _write_seen(sp, names)
        if not shown:
            return None
        return _inbox_line(shown, f"{m.tickets_dir}/inbox")
    except Exception:  # noqa: BLE001 - never block the session
        _log("inbox notice error: " + traceback.format_exc())
        return None


def _process_notice(obj: dict, root: str) -> str | None:
    """One line when the project's process (convention) is older than the installed myvec (E0-10, ADR-036); only
    numbers and a fixed text. `myvec.scaffold` is imported only if `.myvec/scaffold.json` exists. Any error -> None."""
    try:
        if obj.get("agent_id"):
            return None
        if not os.path.lexists(os.path.join(root, ".myvec", "scaffold.json")):
            return None
        cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) and obj.get("cwd") else os.getcwd()
        _, checkout = _git_info(cwd)
        if checkout and _is_linked_worktree(checkout):
            return None
        from myvec import problems
        from myvec.scaffold import state

        r = state.read(root)
        if r.get("status") != "outdated" or not isinstance(r.get("version"), int) \
                or not isinstance(r.get("current"), int):
            return None
        return (f"[myvec] process: this project's convention is v{r['version']}, installed myvec has "
                f"v{r['current']}. Suggest to the owner: say \"{problems.UPDATE_PHRASE}\" "
                "(/myvector, update process).")
    except Exception:  # noqa: BLE001 - never block the session
        _log("process notice error: " + traceback.format_exc())
        return None


def _print_notice(obj: dict, event: str) -> None:
    text = _inbox_notice(obj, event)
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def _drop_inbox_state(obj: dict) -> None:
    try:
        sp = _inbox_state_path(obj.get("session_id"))
        if sp is not None:
            sp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        _log("inbox state error: " + traceback.format_exc())


def _drop_presearch_state(obj: dict) -> None:
    try:
        sid = obj.get("session_id")
        d = _presearch_dir()
        if not isinstance(sid, str) or not SID_RE.match(sid) or not d.is_dir():
            return
        for p in d.glob(sid + "-*"):
            p.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        _log("presearch state error: " + type(e).__name__)


def session_event() -> int:
    obj = _read_stdin_json()
    ev = _event_name(obj)
    if ev:
        _log_session_event(obj, ev)
        if ev == "prompt":
            _print_notice(obj, "prompt")
        elif ev == "end":
            _drop_inbox_state(obj)
            _drop_presearch_state(obj)
    return 0


def session_pulse() -> int:
    """Async PostToolUse `*` heartbeat: only the fact that the session is alive."""
    _log_session_event(_read_stdin_json(), "tool")
    return 0


def session_start() -> int:
    obj = _read_stdin_json()
    _log_session_event(obj, "start")
    cwd = obj.get("cwd") or os.getcwd()
    root = _find_marker_dir(cwd)
    if root is None:
        return 0
    from myvec import context

    text = context.build(cwd)
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    pn = _process_notice(obj, root)
    if pn:
        sys.stdout.write(pn + "\n")
        sys.stdout.flush()
    _print_notice(obj, "start")
    from myvec import projects

    m = projects.try_read_marker(root)  # an invalid marker is silently not a project
    if m is None:
        return 0
    from myvec import activity

    if activity.touch(m.path) == "off":  # access extends the activity window; mode off = no automatic sync
        return 0
    if time.time() - _last_sync(m.path) > SESSION_SYNC_AGE:
        from myvec import lock

        if not lock.is_locked():
            spawn_sync(cwd)
    return 0


def post_edit() -> int:
    obj = _read_stdin_json()
    _log_session_event(obj, "tool")
    fp = (obj.get("tool_input") or {}).get("file_path")
    if not fp:
        return 0
    root = _find_marker_dir(str(Path(fp).parent))
    if root is None:
        return 0
    from myvec import projects

    m = projects.try_read_marker(root)
    if m is None:
        return 0
    from myvec import activity

    if activity.touch(m.path) == "off":
        return 0
    if time.time() - _last_sync(m.path) < EDIT_SYNC_AGE:
        return 0
    from myvec import lock

    if lock.is_locked():
        return 0
    spawn_sync(str(root))
    return 0


def subagent_start() -> int:
    """SubagentStart: a fixed hint into the subagent context (only via JSON additionalContext); no DB, no data."""
    obj = _read_stdin_json()
    cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) and obj.get("cwd") else os.getcwd()
    if _find_marker_dir(cwd) is None:
        return 0
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                                        "additionalContext": SEARCH_HINT}}) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- pre-search hint (E8-11, ADR-040)

_PRESEARCH_PREFILTER = re.compile(r"(?<![A-Za-z0-9_])(?:[ef]?grep|rg|ag|ack|myvec)(?![A-Za-z0-9_])")


def _presearch_dir() -> Path:
    from myvec import config

    return config.home() / "state" / "presearch"


def _presearch_path(obj: dict) -> Path | None:
    sid = obj.get("session_id")
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None
    agent = obj.get("agent_id")
    agent = agent if isinstance(agent, str) and SID_RE.match(agent) else "main"
    return _presearch_dir() / f"{sid}-{agent}"


def _read_presearch(path: Path) -> dict[str, float]:
    out = {"searched": 0.0, "hinted": 0.0}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k in out:
                out[k] = float(v)
    except (OSError, ValueError):
        pass
    return out


def _write_presearch(path: Path, st: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "".join(f"{k}={v:.0f}\n" for k, v in st.items()).encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _prune_presearch() -> None:
    """At most hourly (gate: mtime of the `.pruned` stamp, not of the dir - every state write bumps the dir)."""
    d = _presearch_dir()
    now = time.time()
    stamp = d / ".pruned"
    try:
        if not d.is_dir() or now - stamp.stat().st_mtime < 3600:
            return
    except FileNotFoundError:
        if not d.is_dir():
            return
    except OSError:
        return
    try:
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        return
    with os.scandir(d) as it:
        for e in it:
            if e.name == ".pruned":
                continue
            try:
                if e.stat(follow_symlinks=False).st_mtime < now - STATE_MAX_AGE:
                    os.unlink(e.path)
            except OSError:
                pass


def pre_search(now: float | None = None) -> int:
    """PreToolUse Bash|Grep: one-sentence `myvec search` hint before a grep over the project tree. Never blocks;
    the command text is only classified in memory (state holds two timestamps; errors log only the exception type)."""
    try:
        obj = _read_stdin_json()
        tool = obj.get("tool_name")
        ti = obj.get("tool_input")
        if tool not in ("Bash", "Grep") or not isinstance(ti, dict):
            return 0
        if tool == "Bash":
            cmd = ti.get("command")
            if not isinstance(cmd, str) or not _PRESEARCH_PREFILTER.search(cmd):
                return 0
        cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) and obj.get("cwd") else os.getcwd()
        root = _find_marker_dir(cwd)
        if root is None:
            return 0
        from myvec import presearch

        now = time.time() if now is None else now
        path = _presearch_path(obj)
        if presearch.is_myvec_search(tool, ti):
            if path is not None:
                st = _read_presearch(path)
                st["searched"] = now
                _write_presearch(path, st)
            return 0
        ws = presearch.exploratory(tool, ti, cwd, str(root))
        if ws is None or path is None:
            return 0
        st = _read_presearch(path)
        if now - st["searched"] < presearch.QUIET_AFTER_SEARCH_S or now - st["hinted"] < presearch.HINT_EVERY_S:
            return 0
        st["hinted"] = now
        _prune_presearch()
        _write_presearch(path, st)
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                            "additionalContext": presearch.hint(ws)}}) + "\n")
        sys.stdout.flush()
    except Exception as e:  # noqa: BLE001 - hooks never fail loudly; no message (it may hold command text)
        _log("pre-search error: " + type(e).__name__)
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "pre-search":
            return pre_search()
        if argv and argv[0] == "subagent-start":
            return subagent_start()
        if argv and argv[0] == "session-start":
            return session_start()
        if argv and argv[0] == "post-edit":
            return post_edit()
        if argv and argv[0] == "session-event":
            return session_event()
        if argv and argv[0] == "session-pulse":
            return session_pulse()
    except BaseException:  # noqa: BLE001 - hooks never fail loudly
        _log("hook error: " + traceback.format_exc())
    return 0


# ---------------------------------------------------------------- install-hooks

_STALE_RE = re.compile(r"myvec['\"]? hook ")  # any myvec hook command, whatever the path


def install(settings_path: Path, myvec_cmd: str | None = None) -> tuple[bool, Path | None]:
    """Idempotently merge our hooks. Returns (changed, backup_path)."""
    myvec_cmd = shlex.quote(myvec_cmd or shutil.which("myvec") or "myvec")  # the hook command runs through a shell
    data: dict = {}
    existed = settings_path.exists()
    if existed:
        raw = settings_path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{settings_path} is not a JSON object")
    before = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    ev_cmd = f"{myvec_cmd} {MARK_EVENT}"
    sync_extra = {"timeout": 10}   # session-event hooks are synchronous: the order of events matters
    wanted = [
        ("SessionStart", None, f"{myvec_cmd} {MARK_START}", MARK_START, {}),
        ("PostToolUse", "Edit|Write|MultiEdit", f"{myvec_cmd} {MARK_EDIT}", MARK_EDIT, {}),
        ("UserPromptSubmit", None, ev_cmd, MARK_EVENT, sync_extra),
        ("Stop", None, ev_cmd, MARK_EVENT, sync_extra),
        ("Notification", None, ev_cmd, MARK_EVENT, sync_extra),
        ("SessionEnd", None, ev_cmd, MARK_EVENT, sync_extra),
        ("PreToolUse", "AskUserQuestion", ev_cmd, MARK_EVENT, sync_extra),
        ("PostToolUse", "AskUserQuestion", ev_cmd, MARK_EVENT, sync_extra),
        ("PostToolUse", "*", f"{myvec_cmd} {MARK_PULSE}", MARK_PULSE, {"timeout": 5, "async": True}),
        ("SubagentStart", None, f"{myvec_cmd} {MARK_SUBAGENT}", MARK_SUBAGENT, {"timeout": 5}),
        ("PreToolUse", "Bash|Grep", f"{myvec_cmd} {MARK_PRESEARCH}", MARK_PRESEARCH, {"timeout": 5}),
    ]
    by_key = {(event, mark): (matcher, command, extra) for event, matcher, command, mark, extra in wanted}
    kept: set[tuple[str, str]] = set()
    emptied: set[int] = set()
    for ev, groups in hooks.items():  # stale entries (e.g. another machine's path) go; the first valid one per hook is updated in place
        if not isinstance(groups, list):
            continue
        for g in groups:
            hs = g.get("hooks", [])
            for h in list(hs):
                cmd = h.get("command", "") if h.get("type") == "command" else ""
                if not _STALE_RE.search(cmd):
                    continue
                key = next(((ev, m) for m in MARKS if cmd.endswith(m) or f"{m} " in cmd), None)
                if key in by_key and key not in kept:
                    kept.add(key)
                    h["command"] = by_key[key][1]
                    h.update(by_key[key][2])
                    if "async" not in by_key[key][2]:
                        h.pop("async", None)   # must be synchronous: event order matters
                    if by_key[key][0] and g.get("matcher") != by_key[key][0]:
                        g["matcher"] = by_key[key][0]
                else:
                    hs.remove(h)
                    emptied.add(id(g))
        hooks[ev] = [g for g in groups if id(g) not in emptied or g.get("hooks")]
    for (event, mark), (matcher, command, extra) in by_key.items():
        if (event, mark) not in kept:
            g = {"hooks": [{"type": "command", "command": command, **extra}]}
            if matcher:
                g["matcher"] = matcher
            hooks.setdefault(event, []).append(g)
    if json.dumps(data, sort_keys=True) == before and existed:
        return False, None
    backup = None
    if existed:
        backup = settings_path.with_name(settings_path.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(settings_path, backup)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)
    return True, backup
