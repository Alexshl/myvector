"""Passive detection of test runs (ADR-017, E6-03): a listener of the load sampler recognises runs of test runners by the
command line and records them as `source='passive'` rows - without a wrapper, never duplicating `myvec measure`.

`classify` is pure (no psutil, no I/O). `Detector` is called from the `myvec-sampler` thread: it reads `cmdline` only for
busy processes with a candidate name, once per `(pid, create_time)`, and writes only to the journal
`state/test_runs.jsonl` (the daemon folds it into `test_runs`); it never opens `index.db`.
"""
from __future__ import annotations

import logging
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Callable

from myvec import measure

log = logging.getLogger("myvec.passive")

MAX_UP = 8                          # ancestors inspected for a new candidate
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
ENV_KEYS = (measure.MEASURE_ENV, SESSION_ENV)   # the only environment variables ever read
NAMES = {"pytest", "py.test", "phpunit", "paratest", "node", "nodejs", "npm", "pnpm", "yarn", "bun", "npx", "bunx",
         "uv", "uvx", "vitest", "jest", "poetry", "myvec"}
WATCH_FLAGS = {"--collect-only", "--co", "--version", "--help", "--watch", "--watchAll", "--watch=true", "--watchAll=true"}
_EXTS = (".py", ".js", ".mjs", ".cjs", ".phar", ".exe")

# options that take a value (the next token) per launcher
_PY_VAL = {"-X", "-W", "-Q"}
_NODE_VAL = {"-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file", "--inspect-port"}
_PHP_VAL = {"-d", "-c", "-f", "-z"}
_UV_VAL = {"--python", "-p", "--with", "--with-requirements", "--project", "--directory", "--group", "--extra",
           "--env-file", "--package", "--from", "--index", "--with-editable"}
_NPX_VAL = {"-p", "--package", "--call", "-c"}
_PNPM_VAL = {"--filter", "-F", "--dir", "-C", "--workspace-root"}


def _base(tok: str) -> str:
    b = os.path.basename(tok).lower()
    for e in _EXTS:
        if b.endswith(e):
            return b[: -len(e)]
    return b


def _skip_opts(args: list[str], valued: set[str]) -> list[str]:
    """Drop leading options of a launcher (and the values of known value options)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--":
            i += 1
            break
        i += 2 if args[i] in valued else 1
    return args[i:]


def _normalize(argv: list[str]) -> list[str]:
    """npm retitles its process: cmdline is `['npm test', '', '']`. Drop empty tokens; split a lone spaced token."""
    a = [t for t in argv if t]
    if len(a) == 1 and " " in a[0]:
        a = a[0].split()
    return a


def _unwrap(argv: list[str]) -> list[str]:
    """Strip launchers and interpreters (`uv run`, `python -m`, `node script`, `php`, `npx`, ...): the effective argv."""
    a = _normalize(argv)
    for _ in range(6):
        if not a:
            return []
        b = _base(a[0])
        rest = a[1:]
        if b.startswith("python") or b in ("py", "pypy", "pypy3"):
            i = 0
            while i < len(rest):
                t = rest[i]
                if t == "-m":
                    return rest[i + 1:] if i + 1 < len(rest) else []
                if t == "-c":
                    return []
                if not t.startswith("-"):
                    break
                i += 2 if t in _PY_VAL else 1
            a = rest[i:]
        elif b in ("node", "nodejs"):
            rest = _skip_opts(rest, _NODE_VAL)
            if not rest or rest[0] in ("-e", "-p"):
                return []
            a = rest
        elif re.fullmatch(r"php(\d+(\.\d+)*)?", b):
            a = _skip_opts(rest, _PHP_VAL)
        elif b == "uv":
            r = _skip_opts(rest, _UV_VAL)
            if r and r[0] == "run":
                a = _skip_opts(r[1:], _UV_VAL)
            elif r and r[0] == "tool" and len(r) > 1 and r[1] == "run":
                a = _skip_opts(r[2:], _UV_VAL)
            else:
                return a
        elif b in ("uvx", "npx", "bunx"):
            a = _skip_opts(rest, _NPX_VAL | _UV_VAL if b == "uvx" else _NPX_VAL)
        elif b == "pnpm":
            r = _skip_opts(rest, _PNPM_VAL)
            if r and r[0] in ("exec", "dlx"):
                a = _skip_opts(r[1:], _NPX_VAL)
            else:
                return a
        elif b == "poetry":
            r = _skip_opts(rest, set())
            if r and r[0] == "run":
                a = r[1:]
            else:
                return a
        else:
            return a
    return a


def _is_watch(a: list[str]) -> bool:
    return any(t in WATCH_FLAGS for t in a)


def classify(argv: list[str]) -> str | None:
    """Kind of test run for a command line (`pytest`, `phpunit`, `artisan test`, `vitest`, `jest`, `paratest`,
    `npm test`, `pnpm test`, `yarn test`, `bun test`), else None. Watch modes are not runs."""
    a = _unwrap(argv or [])
    if not a or _is_watch(a):
        return None
    b, rest = _base(a[0]), a[1:]
    if b in ("pytest", "py.test"):
        return "pytest"
    if b in ("phpunit", "paratest"):
        return b
    if b == "artisan":
        r = _skip_opts(rest, set())
        return "artisan test" if r and r[0] == "test" else None
    if b == "jest" or b == "jest-cli":
        return "jest"
    if b == "vitest":
        r = _skip_opts(rest, set())
        if "--run" in rest or (r and r[0] == "run"):
            return "vitest"
        return None   # bare vitest / `vitest watch|dev` is a watch mode
    if b in ("npm", "pnpm", "yarn", "bun"):
        r = _skip_opts(rest, _PNPM_VAL)
        if not r:
            return None
        if r[0] in ("test", "t", "tst") and (b != "yarn" or r[0] == "test") and (b != "bun" or r[0] == "test"):
            return f"{b} test"
        if r[0] in ("run", "run-script") and len(r) > 1 and r[1] == "test":
            return f"{b} test"
    return None


def is_measure(argv: list[str]) -> bool:
    """`myvec measure ...` (also `python -m myvec measure`, `uv run myvec measure`)."""
    a = _unwrap(argv or [])
    if not a or _base(a[0]) != "myvec":
        return False
    r = _skip_opts(a[1:], set())
    return bool(r) and r[0] == "measure"


def _candidate_name(name: str | None) -> bool:
    n = (name or "").lower().split(" ", 1)[0]   # npm retitles itself to «npm test»
    return n.startswith(("python", "php")) or n in NAMES


# ------------------------------------------------------------------ detector

@dataclass
class _Run:
    uid: str
    kind: str
    root_key: tuple[int, float]
    started_at: float
    row: dict
    cpu: dict = field(default_factory=dict)         # (pid, create_time) -> last cumulative cpu_time of the member
    pids: dict = field(default_factory=dict)        # pid -> key
    peak_cores: float = 0.0
    n: int = 0


class Detector:
    """Sampler listener `(snap, views, gone)`. Writes `running`/`done` events of passive runs to the test_runs journal."""

    def __init__(self, source, project_map, host: Callable[[], str] | str | None = None, interval: float = 5.0):
        self.source, self.map, self.interval = source, project_map, interval
        self._host = host if callable(host) else (lambda h=host: h or socket.gethostname())
        self.owner: dict[int, tuple[tuple[int, float], _Run | None]] = {}   # pid -> (key, run); None run = ignored
        self.seen: dict[tuple[int, float], list[str]] = {}                  # cmdline cache (once per key)
        self.runs: list[_Run] = []
        self.journal: Callable[[dict], None] = measure._journal

    # -- helpers
    def _argv(self, key, view=None) -> list[str]:
        if key in self.seen:
            return self.seen[key]
        try:
            argv = list(view.cmdline() if view is not None else self.source.cmdline(key[0]))
        except Exception:  # noqa: BLE001 - AccessDenied / NoSuchProcess
            argv = []
        if len(self.seen) > 4096:   # keys of unreadable processes never reach `gone`: bound the cache
            self.seen.clear()
        self.seen[key] = argv
        return argv

    def _write(self, row: dict) -> None:
        try:
            self.journal(row)
        except Exception:  # noqa: BLE001 - never break the sampler
            log.warning("passive journal write failed", exc_info=True)

    def _env(self, pid: int) -> dict:
        try:
            return dict(self.source.environ_keys(pid, ENV_KEYS) or {})
        except Exception:  # noqa: BLE001
            return {}

    # -- listener
    def __call__(self, snap, views, gone) -> None:
        sums: dict[int, float] = {}
        for v in views:
            run = self._handle(v, snap)
            if run is not None:
                sums[id(run)] = sums.get(id(run), 0.0) + v.cpu_pct / 100.0
        for run in self.runs:
            if id(run) in sums:
                run.n += 1
            run.peak_cores = max(run.peak_cores, sums.get(id(run), 0.0))
        for k in gone:
            self.seen.pop(k, None)
            cur = self.owner.get(k[0])
            if cur is None or cur[0] != k:
                continue
            del self.owner[k[0]]
            run = cur[1]
            if run is not None and run.root_key == k:
                self._finish(run, snap.ts)

    def _handle(self, v, snap) -> _Run | None:
        k = (v.pid, v.create_time)
        cur = self.owner.get(v.pid)
        if cur is not None and cur[0] == k:
            run = cur[1]
            if run is not None and v.cpu_time is not None:
                run.cpu[k] = v.cpu_time
            return run
        par = self.owner.get(v.ppid) if v.ppid else None
        if par is not None:     # child of a known run (xdist worker, ...) or of an ignored one: no cmdline read
            run = par[1]
            self.owner[v.pid] = (k, run)
            if run is not None:
                run.pids[v.pid] = k
                run.cpu[k] = v.cpu_time or 0.0
            return run
        if k in self.seen or not _candidate_name(v.name):
            return None
        argv = self._argv(k, v)
        if classify(argv) is None and not is_measure(argv):
            return None
        return self._discover(v, k, snap)

    def _discover(self, v, k, snap) -> _Run | None:
        chain = [(k, v.pid, v.name, v.cpu_time)]    # candidate first, then ancestors
        root = 0
        measure_seen = is_measure(self.seen.get(k, []))
        pid, hops = v.ppid, 0
        joined = None
        while pid and pid > 1 and hops < MAX_UP:
            hops += 1
            if pid in self.owner:
                joined = self.owner[pid]
                break
            try:
                ppid, name = self.source.details(pid)
                ct = float(self.source.create_time(pid))
            except Exception:  # noqa: BLE001
                break
            ak = (pid, ct)
            if _candidate_name(name):
                argv = self._argv(ak)
                if is_measure(argv):
                    measure_seen = True
                if classify(argv) is not None:
                    root = len(chain)
            chain.append((ak, pid, name, None))
            pid = ppid
        members = chain[: root + 1]
        if joined is not None and joined[1] is None or measure_seen:
            return self._ignore(members, None)
        if joined is not None:      # an ancestor already belongs to a run: this process is its member
            run = joined[1]
            self._join(run, members)
            return run if members[0][0] == k else None
        rkey, rpid = members[-1][0], members[-1][1]
        env = {**self._env(v.pid), **self._env(rpid)} if rpid != v.pid else self._env(v.pid)
        if measure.MEASURE_ENV in env:
            return self._ignore(members, None)
        try:
            cwd = self.source.cwd(rpid)
        except Exception:  # noqa: BLE001
            cwd = None
        project = self.map.project_for(cwd)
        if project is None:
            return self._ignore(members, None)
        kind = classify(self._argv(rkey))
        host = self._host()
        uid = f"passive-{host}-{rpid}-{int(rkey[1] * 1000)}"
        row = {"run_uid": uid, "source": "passive", "state": "running", "host": host, "pid": rpid,
               "project_path": project, "cwd": cwd, "agent": None, "label": kind,
               "session_id": env.get(SESSION_ENV) or None, "command": measure.safe_command(_normalize(self._argv(rkey))),
               "started_at": rkey[1], "sample_interval_s": self.interval}
        run = _Run(uid, kind, rkey, rkey[1], row)
        self.runs.append(run)
        self._join(run, members)
        self._write(row)
        return run if members[0][0] == k else None

    def _ignore(self, members, _run) -> None:
        for key, pid, _n, _c in members:
            self.owner[pid] = (key, None)
        return None

    def _join(self, run: _Run, members) -> None:
        for key, pid, _n, cpu in members:
            self.owner[pid] = (key, run)
            run.pids[pid] = key
            if cpu is None:
                try:
                    cpu = float(self.source.cpu_time(pid))
                except Exception:  # noqa: BLE001
                    cpu = 0.0
            run.cpu[key] = cpu

    def _finish(self, run: _Run, ts: float) -> None:
        if run in self.runs:
            self.runs.remove(run)
        cpu = sum(run.cpu.values())
        wall = max(0.0, ts - run.started_at)
        row = dict(run.row, state="done", ended_at=ts, wall_s=wall, cpu_s=cpu, peak_cores=run.peak_cores,
                   avg_cores=(cpu / wall) if wall > 0 else None, n_samples=run.n or None)
        for pid in [p for p, (_k, r) in self.owner.items() if r is run]:
            del self.owner[pid]
        self._write(row)
