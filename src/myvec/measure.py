"""`myvec measure`: run a command and measure its process tree (ADR-003, ADR-011).

Imported by the fast path in `cli.py` before typer: stdlib only (psutil lazily, sqlite-vec never). CPU time comes
from `os.wait4` (exact for the child and every descendant it waited for); peaks of cores and RSS come from psutil
sampling. The run is stored in `test_runs`; when the database is busy (300 ms) or missing, events go to
`state/test_runs.jsonl` and `fold` moves them into the table later. The wrapper never changes the exit code
because of the database.

Peak memory is the sum over the tree of USS (`memory_full_info`, shared pages counted once) where psutil can read
it, else RSS; without any sample the child's `ru_maxrss` is used.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from myvec import config, filters
from myvec import db as dbmod

USAGE = ("usage: myvec measure [--ticket ID] [--agent NAME] [--label TEXT] [--interval SECONDS] -- <command> [args...]\n"
         "Runs the command, prints a one-line summary (wall, CPU, cores, RSS, exit code) to stderr and records the run.")
HIDDEN = "[команда приховано]"
BUSY_TIMEOUT_MS = 300
LOST_AFTER_S = 60.0
MEASURE_ENV = "MYVEC_MEASURE_RUN"   # set for the child: the passive detector (E6-03) skips such runs
COLUMNS = ("run_uid", "source", "state", "host", "pid", "project_path", "cwd", "branch", "ticket_id", "ticket_source",
           "agent", "label", "session_id", "command", "started_at", "ended_at", "wall_s", "cpu_user_s", "cpu_sys_s",
           "cpu_s", "avg_cores", "peak_cores", "peak_rss_bytes", "exit_code", "signal", "sample_interval_s", "n_samples",
           "interrupted")
_BRANCH_TICKET = re.compile(r"^t/([A-Za-z][A-Za-z0-9]*-\d+)(?:-|$)")


# ---------------------------------------------------------------- formatting (uk-UA)

def _num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "–"
    if sec < 60:
        return f"{_num(sec)} с"
    total = int(round(sec))
    if total < 3600:
        return f"{total // 60} хв {total % 60} с"
    return f"{total // 3600} год {total % 3600 // 60} хв"


def fmt_cores(x: float | None) -> str:
    return "–" if x is None else _num(x)


def fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "–"
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{_num(mb / 1024)} ГБ"
    return f"{int(round(mb))} МБ"


def safe_command(argv: list[str]) -> str:
    """Command line as stored and printed: secrets masked, a hard secret hides the whole line."""
    text = shlex.join(argv)
    if filters.has_hard_secret(text):
        return HIDDEN
    return filters.mask_secrets(text)[0]


def safe_text(text: str | None) -> str | None:
    """Free-form label/agent as stored: masked, hidden when it holds a hard secret."""
    if not text:
        return text
    return HIDDEN if filters.has_hard_secret(text) else filters.mask_secrets(text)[0]


def summary_line(command: str, r: dict) -> str:
    code = r.get("exit_code")
    tail = " · перервано" if r.get("interrupted") else ""
    return (f"measure: {command} · {fmt_duration(r.get('wall_s'))} · CPU {fmt_duration(r.get('cpu_s'))} · "
            f"{fmt_cores(r.get('avg_cores'))}/{fmt_cores(r.get('peak_cores'))} ядра · {fmt_bytes(r.get('peak_rss_bytes'))} · "
            f"exit {'–' if code is None else code}{tail}")


# ---------------------------------------------------------------- attribution

def _toplevel(cwd: str) -> str | None:
    p = Path(cwd)
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return str(d)
    return None


def attribute(db: sqlite3.Connection | None, cwd: str, ticket: str | None, session_id: str | None) -> dict:
    """project, branch and ticket of a run; the ticket is fixed at launch (ADR-011)."""
    from myvec import gitutil, sessions

    top = _toplevel(cwd)
    branch = None
    try:
        branch = gitutil.head_branch(top or cwd)
    except Exception:  # noqa: BLE001
        pass
    main_root = sessions.main_root_of(top)
    project = None
    ids_by_project: dict = {}
    if db is not None:
        try:
            project = sessions.project_of(db, cwd, top, main_root)
            for r in db.execute("SELECT project_path, id FROM tickets"):
                ids_by_project.setdefault(r[0], set()).add(r[1])
        except sqlite3.Error:
            pass
    out = {"project_path": project, "branch": branch, "ticket_id": None, "ticket_source": None}
    if ticket:
        out.update(ticket_id=ticket, ticket_source="flag")
        return out
    claim = None
    if session_id and config.home().joinpath("state", "ticket_claims.jsonl").exists():
        try:
            from myvec import ticketops

            claim = ticketops.read_claims().get(session_id)
        except Exception:  # noqa: BLE001
            claim = None
    row = {"project_path": project, "branch": branch, "worktree": top if main_root else None}
    res = sessions.resolve_ticket(row, claim, ids_by_project, lambda root: sessions.project_of(db, root, sessions.main_root_of(root))
                                  if db is not None else None)
    if res:
        out.update(ticket_id=res[1], ticket_source=res[2])
        if res[2] == "claim" and res[0]:
            out["project_path"] = res[0]
        return out
    m = _BRANCH_TICKET.match(branch or "")   # tickets of the project not indexed (yet): fall back to the id shape
    if m:
        out.update(ticket_id=m.group(1), ticket_source="branch")
    elif top and main_root and os.path.basename(os.path.dirname(top)) == ".worktrees":
        out.update(ticket_id=os.path.basename(top), ticket_source="worktree")
    return out


# ---------------------------------------------------------------- storage

def _upsert(db: sqlite3.Connection, row: dict) -> None:
    """Insert or update by run_uid; a finished row is never reverted to `running`."""
    cur = db.execute("SELECT state FROM test_runs WHERE run_uid=?", (row["run_uid"],)).fetchone()
    if cur is None:
        db.execute(f"INSERT INTO test_runs({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))})",
                   [row.get(c) for c in COLUMNS])
    elif not (cur[0] in ("done", "lost") and row.get("state") == "running"):
        cols = [c for c in COLUMNS if c != "run_uid"]
        db.execute(f"UPDATE test_runs SET {','.join(c + '=?' for c in cols)} WHERE run_uid=?",
                   [row.get(c) for c in cols] + [row["run_uid"]])


def _open_db(create_schema: bool = True) -> sqlite3.Connection | None:
    """Plain connection (no vec) with a short busy timeout; None when there is no database yet."""
    path = config.db_path()
    if not path.exists():
        return None
    db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if create_schema:
        db.executescript(dbmod.TEST_RUNS_SCHEMA)
    return db


JOURNAL_LINE_MAX = 4000   # one O_APPEND write below PIPE_BUF-ish size stays atomic between writers


def _journal_line(row: dict) -> bytes:
    row = dict(row)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    for key in ("command", "cwd", "label", "agent"):
        if len(data) <= JOURNAL_LINE_MAX:
            break
        v = row.get(key)
        if isinstance(v, str):
            row[key] = v[: max(40, len(v) - (len(data) - JOURNAL_LINE_MAX) - 1)]
            data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    return data


def _journal(row: dict) -> None:
    p = config.test_runs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    from myvec import activity   # stdlib only; the same flock scheme as the activity queue (E0-04)

    with activity._queue_lock(p, exclusive=False):   # shared while open+write: the folder renames under LOCK_EX
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, _journal_line(row))
        finally:
            os.close(fd)


def _store(db: sqlite3.Connection | None, row: dict) -> sqlite3.Connection | None:
    """Write straight to the table, otherwise to the journal. Returns the connection to reuse (None after failure)."""
    try:
        if db is None:
            raise sqlite3.OperationalError("no database")
        _upsert(db, row)
        return db
    except (sqlite3.Error, OSError):
        try:
            _journal(row)
        except OSError:
            pass
        return None


def pending() -> bool:
    p = config.test_runs_path()
    if p.exists():
        return True
    try:
        return any(True for _ in p.parent.glob(p.name + ".folding.*"))
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _claim(src: Path) -> list[Path]:
    """Atomically take the journal (same scheme as sessions._claim); adopt leftovers of dead or failed folders."""
    claimed: list[Path] = []
    tmp = src.with_name(f"{src.name}.folding.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    from myvec import activity

    try:
        with activity._queue_lock(src, exclusive=True):
            os.replace(src, tmp)
        claimed.append(tmp)
    except OSError:
        pass
    try:
        for old in sorted(src.parent.glob(src.name + ".folding.*")):
            if old in claimed:
                continue
            try:
                pid = int(old.name.split(".folding.", 1)[1].split(".")[0])
            except ValueError:
                continue
            if pid != os.getpid() and _pid_alive(pid):
                continue
            mine = old.with_name(f"{src.name}.folding.{os.getpid()}.{uuid.uuid4().hex[:8]}")
            try:
                os.replace(old, mine)
                claimed.append(mine)
            except OSError:
                continue
    except OSError:
        pass
    return claimed


def _read_rows(files: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for f in files:
        try:
            fh = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("run_uid"), str):
                    rows.append(r)
    rows.sort(key=lambda r: (0 if r.get("state") == "running" else 1, r.get("ended_at") or r.get("started_at") or 0))
    return rows


def _pin_passive(db: sqlite3.Connection, r: dict) -> None:
    """Passive rows: attribution once, on the first insert; later events keep what the first one fixed (ADR-011)."""
    cur = db.execute("SELECT branch, ticket_id, ticket_source, project_path FROM test_runs WHERE run_uid=?",
                     (r["run_uid"],)).fetchone()
    if cur is not None:
        r.update(branch=cur[0], ticket_id=cur[1], ticket_source=cur[2], project_path=cur[3])
        return
    if not r.get("cwd"):
        return
    try:
        attr = attribute(db, r["cwd"], None, r.get("session_id"))
    except Exception:  # noqa: BLE001
        return
    if r.get("project_path"):   # the detector resolved it from the cwd of the run: keep
        attr.pop("project_path", None)
    r.update(attr)


def fold(db: sqlite3.Connection, now: float | None = None) -> int:
    """Apply the fallback journal to `test_runs` (no duplicates: upsert by run_uid) and mark `running` runs of this
    host whose process is gone and which started over a minute ago as `lost`. Returns the number of rows applied."""
    from myvec import sessions

    now = time.time() if now is None else now
    db.executescript(dbmod.TEST_RUNS_SCHEMA)
    claimed = _claim(config.test_runs_path())
    n = 0
    if claimed:
        rows = _read_rows(claimed)
        db.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                if not r.get("project_path") and r.get("cwd"):
                    r["project_path"] = sessions.project_of(db, r["cwd"], sessions.main_root_of(r["cwd"]))
                if r.get("source") == "passive":
                    _pin_passive(db, r)
                _upsert(db, r)
                n += 1
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        for f in claimed:
            try:
                f.unlink()
            except OSError:
                pass
    host = socket.gethostname()
    for r in db.execute("SELECT run_uid, pid FROM test_runs WHERE state='running' AND host=? AND started_at<?",
                        (host, now - LOST_AFTER_S)).fetchall():
        if not r[1] or not _pid_alive(int(r[1])):
            db.execute("UPDATE test_runs SET state='lost', ended_at=? WHERE run_uid=?", (now, r[0]))
    return n


# ---------------------------------------------------------------- sampling

class Sampler(threading.Thread):
    """Samples the process tree under `pid` every `interval` s: peak summed RSS and peak cores (CPU-time deltas)."""

    def __init__(self, pid: int, interval: float):
        super().__init__(daemon=True)
        import psutil

        self.psutil = psutil
        self.pid = pid
        self.interval = interval
        self.stop = threading.Event()
        self.peak_rss = 0
        self.peak_cores = 0.0
        self.n = 0
        self.cum_cpu: dict = {}   # (pid, create_time) -> last seen cpu seconds: tree CPU incl. short-lived members
        self._prev: dict = {}
        self._t: float | None = None

    def sample(self) -> None:
        ps = self.psutil
        try:
            root = ps.Process(self.pid)
            procs = [root] + root.children(recursive=True)
        except ps.Error:
            return
        now = time.monotonic()
        rss = 0
        cur: dict = {}
        for p in procs:
            try:
                key = (p.pid, p.create_time())
                t = p.cpu_times()
                cur[key] = t.user + t.system
                try:
                    rss += p.memory_full_info().uss
                except (ps.AccessDenied, AttributeError, NotImplementedError):
                    rss += p.memory_info().rss
            except ps.Error:
                continue
        self.peak_rss = max(self.peak_rss, rss)
        for k, v in cur.items():
            self.cum_cpu[k] = max(self.cum_cpu.get(k, 0.0), v)
        if self._t is not None and now > self._t:
            # a pid seen for the first time is a baseline: its earlier history is not part of this interval
            delta = sum(max(0.0, v - self._prev.get(k, v)) for k, v in cur.items())
            self.peak_cores = max(self.peak_cores, delta / (now - self._t))
        self._prev, self._t = cur, now
        self.n += 1

    def run(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - sampling must never break the run
                pass
            if self.stop.wait(self.interval):
                return


def _tty_foreground() -> bool:
    """Is our process group the foreground group of the terminal (stdin or /dev/tty)? Then a Ctrl+C was delivered to
    the whole group, the child included."""
    for opener in (lambda: 0, lambda: os.open("/dev/tty", os.O_RDONLY)):
        try:
            fd = opener()
            try:
                if os.isatty(fd):
                    return os.tcgetpgrp(fd) == os.getpgrp()
            finally:
                if fd != 0:
                    os.close(fd)
        except OSError:
            continue
    return False


def _have_psutil() -> bool:
    try:
        import psutil  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------- run

def _decode(status: int) -> tuple[int, int | None]:
    """(exit code for the shell, terminating signal)."""
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        return 128 + sig, sig
    return os.WEXITSTATUS(status), None


def execute(argv: list[str], *, ticket: str | None = None, agent: str | None = None, label: str | None = None,
            interval: float = 1.0) -> tuple[int, dict]:
    """Run `argv` and measure it. Returns (exit code, result row); the row is already stored."""
    cwd = os.getcwd()
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    db = None
    try:
        db = _open_db()
        if db is not None and pending():
            try:
                fold(db)
            except Exception:  # noqa: BLE001
                pass
    except (sqlite3.Error, OSError):
        db = None
    try:
        attr = attribute(db, cwd, ticket, session_id)
    except Exception:  # noqa: BLE001
        attr = {"project_path": None, "branch": None, "ticket_id": ticket, "ticket_source": "flag" if ticket else None}
    command = safe_command(argv)
    row = {"run_uid": uuid.uuid4().hex, "source": "wrapper", "state": "running", "host": socket.gethostname(),
           "cwd": cwd, "agent": safe_text(agent), "label": safe_text(label), "session_id": session_id, "command": command,
           "sample_interval_s": interval, **attr}

    box: dict = {"proc": None, "pending": [], "interrupted": None, "done": False}

    def send(signum):
        proc = box["proc"]
        if proc is None or box["done"]:
            return
        try:
            proc.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass

    def forward(signum, _frame):
        box["interrupted"] = box["interrupted"] or signum
        if box["proc"] is None:
            box["pending"].append(signum)   # delivered right after Popen returns
        elif signum == signal.SIGINT and _tty_foreground():
            pass   # a tty Ctrl+C already reached the child through the foreground process group: do not double it
        else:
            send(signum)

    old: dict = {}
    try:   # signal() works in the main thread only; handlers go in before Popen so no signal is lost
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old[s] = signal.signal(s, forward)
    except ValueError:
        pass
    sampler = None
    try:
        t0 = time.monotonic()
        started = time.time()
        try:
            proc = subprocess.Popen(argv, env={**os.environ, MEASURE_ENV: row["run_uid"]})   # same process group: Ctrl+C reaches the child too
        except FileNotFoundError:
            print(f"measure: command not found: {argv[0]}", file=sys.stderr)
            return 127, {}
        except OSError as e:
            print(f"measure: cannot execute {argv[0]}: {e.strerror or e}", file=sys.stderr)
            return 126, {}
        box["proc"] = proc
        try:   # after Popen: an ignored SIGQUIT would be inherited by the command
            old[signal.SIGQUIT] = signal.signal(signal.SIGQUIT, signal.SIG_IGN)
        except ValueError:
            pass
        early, box["pending"] = box["pending"], []
        for signum in early:
            forward(signum, None)
        try:   # recording and sampling are best effort: the wait below must always run
            row.update(pid=proc.pid, started_at=started)
            db = _store(db, row)
            if _have_psutil():
                sampler = Sampler(proc.pid, interval)
                sampler.start()
        except Exception:  # noqa: BLE001
            sampler = None
        while True:
            try:
                _, status, ru = os.wait4(proc.pid, 0)
                break
            except InterruptedError:
                continue
        box["done"] = True
        wall = time.monotonic() - t0
        proc.returncode = 0   # reaped here, not by Popen
    finally:
        box["done"] = True
        if sampler:
            sampler.stop.set()
            sampler.join(timeout=2)
        for s, h in old.items():
            signal.signal(s, h)

    code, sig = _decode(status)
    cpu_user, cpu_sys = ru.ru_utime, ru.ru_stime
    cpu = cpu_user + cpu_sys
    if sampler:
        cpu = max(cpu, sum(sampler.cum_cpu.values()))   # also covers descendants wait4 does not account for
    avg = cpu / wall if wall > 0 else 0.0
    peak_cores = peak_rss = None
    if sampler:
        floor = ru.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        peak_rss = int(sampler.peak_rss or floor)
        peak_cores = max(sampler.peak_cores, avg)
    row.update(state="done", ended_at=started + wall, wall_s=wall, cpu_user_s=cpu_user, cpu_sys_s=cpu_sys, cpu_s=cpu,
               avg_cores=avg, peak_cores=peak_cores, peak_rss_bytes=peak_rss, exit_code=code, signal=sig,
               n_samples=sampler.n if sampler else None, interrupted=1 if box["interrupted"] else 0)
    received = box["interrupted"]
    if db is None:
        try:   # the start event may have been written to the journal: retry the table once, else journal the end
            db = _open_db()
        except (sqlite3.Error, OSError):
            db = None
    db = _store(db, row)
    if db is not None:
        try:
            db.close()
        except sqlite3.Error:
            pass
    return code, dict(row, received_signal=received)


def parse(args: list[str]) -> tuple[dict, list[str]] | None:
    """Options before `--`, then the command; None on a usage error."""
    opts: dict = {"ticket": None, "agent": None, "label": None, "interval": 1.0}
    i = 0
    while i < len(args) and args[i] != "--":
        a = args[i]
        key, eq, val = a.partition("=")
        if key not in ("--ticket", "--agent", "--label", "--interval"):
            return None
        if not eq:
            i += 1
            if i >= len(args) or args[i] == "--":
                return None
            val = args[i]
        if key == "--interval":
            try:
                opts["interval"] = max(0.05, float(val))
            except ValueError:
                return None
        else:
            opts[key[2:]] = val
        i += 1
    cmd = args[i + 1:] if i < len(args) else []
    return (opts, cmd) if cmd else None


def main(args: list[str]) -> int:
    if args[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    parsed = parse(args)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    opts, cmd = parsed
    code, row = execute(cmd, **opts)
    if row:
        print(summary_line(row["command"], row), file=sys.stderr)
        got = row.get("received_signal")
        if got:   # die of the same signal, so the caller (shell, parent) sees an interrupted process
            try:
                signal.signal(got, signal.SIG_DFL)
                os.kill(os.getpid(), got)
            except (OSError, ValueError):
                pass
    return code
