"""Node load sampler (ADR-003, ADR-012): CPU (total, per core), load average, memory, CPU temperature and the CPU of
processes split by registered project (by process cwd). Runs in its own thread `myvec-sampler` inside the daemon.

Snapshots live in memory and are written once a minute to a separate `load.db` (raw 24 h + per-minute aggregates 30
days). psutil is reached only through an injectable `source`, so the core is tested on fakes. Per snapshot only
`cpu_times` of all processes is read; `cwd` is resolved lazily, once per `(pid, create_time)`, for processes that
actually used CPU.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from myvec import config

log = logging.getLogger("myvec.nodeload")

MIN_INTERVAL = 1.0           # s, lower bound of the sample interval
PROC_MIN_PCT = 0.5           # raw per-project rows below this (% of one core) are not stored
BUFFER_MINUTES = 30          # unflushed minutes kept in memory while the database is busy
PRUNE_EVERY = 600.0          # s between retention sweeps
SLOW_EVERY = 6               # cpu_freq and temperature are read every Nth system() call
RING = 720                   # snapshots kept in memory (1 h at 5 s)
SCHEMA_VERSION = 1
ALERT_GRACE_S = 15.0         # CPU dips shorter than this do not end an alert series (ADR-018)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS node_samples(
  ts REAL PRIMARY KEY, host TEXT, cpu_pct REAL, cores TEXT, load1 REAL, load5 REAL, load15 REAL, mem_used INTEGER,
  mem_total INTEGER, swap_used INTEGER, temp_c REAL, freq_mhz REAL);
CREATE TABLE IF NOT EXISTS node_proc_samples(
  ts REAL, host TEXT, project_path TEXT, cpu_pct REAL, n_procs INTEGER, top_name TEXT, top_pid INTEGER);
CREATE INDEX IF NOT EXISTS node_proc_samples_ts ON node_proc_samples(ts);
CREATE TABLE IF NOT EXISTS node_minutes(
  minute INTEGER PRIMARY KEY, host TEXT, n INTEGER, cpu_avg REAL, cpu_max REAL, cores_avg TEXT, load1_avg REAL,
  mem_used_avg REAL, mem_used_max INTEGER, swap_used_max INTEGER, temp_avg REAL, temp_max REAL);
CREATE TABLE IF NOT EXISTS node_proc_minutes(
  minute INTEGER, host TEXT, project_path TEXT, cpu_avg REAL, cpu_max REAL, top_name TEXT,
  PRIMARY KEY(minute, project_path));
"""


# ------------------------------------------------------------------ sources

@dataclass
class ProcInfo:
    pid: int
    create_time: float
    ppid: int | None             # None/None for name: the source fills them lazily (`details`) for busy processes only
    name: str | None
    cpu_time: float | None       # user+system seconds; None when the process is not readable


def pick_temp(sensors: dict | None, override: str = "") -> float | None:
    """CPU temperature from `psutil.sensors_temperatures()`-like data: override -> k10temp (Tctl/Tdie) -> coretemp
    (Package id 0) -> cpu_thermal/soc_thermal -> None."""
    if not sensors:
        return None

    def first(entries, prefer: tuple[str, ...] = ()) -> float | None:
        entries = [e for e in entries or [] if getattr(e, "current", None) is not None]
        for want in prefer:
            for e in entries:
                if (getattr(e, "label", "") or "").lower().startswith(want):
                    return float(e.current)
        return float(entries[0].current) if entries else None

    if override and override in sensors:
        return first(sensors[override])
    for name, prefer in (("k10temp", ("tctl", "tdie")), ("coretemp", ("package id 0",)),
                         ("cpu_thermal", ()), ("soc_thermal", ())):
        if name in sensors:
            t = first(sensors[name], prefer)
            if t is not None:
                return t
    return None


class SystemSource:
    """The real source (psutil)."""

    def __init__(self, temp_sensor: str = ""):
        import psutil

        self.ps = psutil
        self.temp_sensor = temp_sensor
        self._host: dict | None = None
        self._n_system = 0
        self._slow: tuple[float | None, float | None] = (None, None)   # (freq_mhz, temp_c), refreshed every SLOW_EVERY
        self._boot = float(psutil.boot_time()) if sys.platform.startswith("linux") else None
        self._clk = os.sysconf("SC_CLK_TCK") if self._boot is not None else 0
        self._fast_ok = self._boot is not None and os.path.exists("/proc/self/stat")
        psutil.cpu_percent(percpu=True)   # prime: the next call returns usage since this one

    def system(self) -> dict:
        ps = self.ps
        cores = [float(x) for x in ps.cpu_percent(percpu=True)]
        try:
            load = tuple(float(x) for x in os.getloadavg())
        except OSError:
            load = (0.0, 0.0, 0.0)
        vm, sw = ps.virtual_memory(), ps.swap_memory()
        if self._n_system % SLOW_EVERY == 0:
            self._slow = self._read_slow()
        self._n_system += 1
        freq, temp = self._slow
        return {"cores": cores, "cpu_pct": sum(cores) / len(cores) if cores else 0.0, "load": load,
                "mem_used": int(vm.total - vm.available), "mem_total": int(vm.total), "swap_used": int(sw.used),
                "freq_mhz": freq, "temp_c": temp}

    def _read_slow(self) -> tuple[float | None, float | None]:
        ps = self.ps
        try:
            f = ps.cpu_freq()
            freq = float(f.current) if f and f.current else None
        except Exception:  # noqa: BLE001 - not available on every platform
            freq = None
        try:
            temp = pick_temp(ps.sensors_temperatures(), self.temp_sensor)
        except Exception:  # noqa: BLE001 - psutil has no sensors on macOS
            temp = None
        return freq, temp

    def _procs_proc(self) -> list[ProcInfo]:
        """Linux: utime+stime and starttime straight from /proc/<pid>/stat (about half the cost of psutil)."""
        out = []
        clk, boot = self._clk, self._boot
        with os.scandir("/proc") as it:
            names = [e.name for e in it if e.name.isdigit()]
        for n in names:
            try:
                with open(f"/proc/{n}/stat", "rb") as f:
                    raw = f.read()
                rest = raw[raw.rindex(b")") + 2:].split()
                out.append(ProcInfo(int(n), boot + int(rest[19]) / clk, None, None,
                                    (int(rest[11]) + int(rest[12])) / clk))
            except (OSError, ValueError, IndexError):
                continue   # the process is gone or not readable
        return out

    def procs(self) -> Iterable[ProcInfo]:
        if self._fast_ok:
            try:
                return self._procs_proc()
            except OSError:
                self._fast_ok = False
        return self._procs_psutil()

    def _procs_psutil(self) -> Iterable[ProcInfo]:
        for p in self.ps.process_iter(["cpu_times", "create_time"]):
            i = p.info
            ct = i.get("create_time")
            if ct is None:
                continue
            t = i.get("cpu_times")
            yield ProcInfo(p.pid, float(ct), None, None, float(t.user + t.system) if t is not None else None)

    def details(self, pid: int) -> tuple[int, str]:
        p = self.ps.Process(pid)
        return p.ppid(), p.name()

    def cwd(self, pid: int) -> str | None:
        return self.ps.Process(pid).cwd()

    def cmdline(self, pid: int) -> list[str]:
        return self.ps.Process(pid).cmdline()

    def create_time(self, pid: int) -> float:
        if self._fast_ok:   # the same source and cached boot time as procs(): keys (pid, create_time) stay equal
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
            return self._boot + int(raw[raw.rindex(b")") + 2:].split()[19]) / self._clk
        return float(self.ps.Process(pid).create_time())

    def cpu_time(self, pid: int) -> float:
        t = self.ps.Process(pid).cpu_times()
        return float(t.user + t.system)

    def environ_keys(self, pid: int, keys: Iterable[str]) -> dict:
        """Only the requested environment variables; the rest of the environment is never kept."""
        env = self.ps.Process(pid).environ()
        return {k: env[k] for k in keys if k in env}

    def host_info(self) -> dict:
        if self._host is None:
            self._host = {"hostname": socket.gethostname(), "cpu_model": _cpu_model(),
                          "cores": self.ps.cpu_count() or 0, "ram_total": int(self.ps.virtual_memory().total)}
        return self._host


def _cpu_model() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  timeout=2).stdout.strip()
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ------------------------------------------------------------------ projects

def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class ProjectMap:
    """cwd -> registered project: deepest root containing the cwd, else the main checkout of the linked git worktree
    the cwd is in (a worktree outside the project tree). In memory, no SQL per process."""

    def __init__(self, roots: Iterable[tuple[str, str]] = ()):
        self.set_roots(roots)

    def set_roots(self, roots: Iterable[tuple[str, str]]) -> None:
        self.roots = sorted(((os.path.normpath(r), p) for p, r in roots), key=lambda x: -len(x[0]))
        self._top: dict[str, str | None] = {}

    def _match(self, path: str) -> str | None:
        for root, project in self.roots:
            if _under(path, root):
                return project
        return None

    def _worktree_main(self, cwd: str) -> str | None:
        from myvec.sessions import main_root_of

        d = cwd
        if d not in self._top:
            top = None
            for _ in range(24):
                if os.path.isfile(os.path.join(d, ".git")):
                    top = d
                    break
                if os.path.exists(os.path.join(d, ".git")):   # a normal checkout: nothing to follow
                    break
                nd = os.path.dirname(d)
                if nd == d:
                    break
                d = nd
            self._top[cwd] = main_root_of(top) if top else None
            if len(self._top) > 4096:
                self._top.clear()
        main = self._top.get(cwd)
        return os.path.normpath(main) if main else None

    def project_for(self, cwd: str | None) -> str | None:
        if not cwd:
            return None
        cwd = os.path.normpath(cwd)
        hit = self._match(cwd)
        if hit:
            return hit
        main = self._worktree_main(cwd)
        return self._match(main) if main else None


# ------------------------------------------------------------------ sampler

@dataclass
class Snapshot:
    ts: float
    cpu_pct: float                    # % of all cores (0-100)
    cores: list[float]
    load: tuple[float, float, float]
    mem_used: int
    mem_total: int
    swap_used: int
    temp_c: float | None
    freq_mhz: float | None
    projects: dict = field(default_factory=dict)   # path|None -> {cpu_pct (of one core), n, top_name, top_pid}
    sampler_cpu_pct: float = 0.0

    def to_dict(self) -> dict:
        return {"ts": self.ts, "cpu_pct": self.cpu_pct, "cores": self.cores, "load": list(self.load),
                "mem_used": self.mem_used, "mem_total": self.mem_total, "swap_used": self.swap_used,
                "temp_c": self.temp_c, "freq_mhz": self.freq_mhz, "sampler_cpu_pct": self.sampler_cpu_pct,
                "projects": [{"project": k, **v} for k, v in self.projects.items()]}


class ProcView:
    """A process that used CPU in this snapshot; `cwd()`/`cmdline()` are lazy and tolerate AccessDenied."""

    def __init__(self, info: ProcInfo, cpu_pct: float, project: str | None, source):
        self.pid, self.create_time, self.name = info.pid, info.create_time, info.name
        self.ppid, self.cpu_pct, self.project = info.ppid, cpu_pct, project
        self.cpu_time = info.cpu_time          # cumulative user+system seconds (passive test detector, E6-03)
        self._source = source

    def cwd(self) -> str | None:
        try:
            return self._source.cwd(self.pid)
        except Exception:  # noqa: BLE001 - AccessDenied / NoSuchProcess / Zombie
            return None

    def cmdline(self) -> list[str]:
        try:
            return list(self._source.cmdline(self.pid))
        except Exception:  # noqa: BLE001
            return []


class Sampler:
    def __init__(self, source, project_map: ProjectMap, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, thread_cpu: Callable[[], float] = time.thread_time):
        self.source, self.map, self.clock, self.wall, self.thread_cpu = source, project_map, clock, wall, thread_cpu
        self._prev: dict[tuple[int, float], float] = {}
        self._proj: dict[tuple[int, float], str | None] = {}
        self._t = None
        self.listeners: list[Callable] = []

    def add_listener(self, fn: Callable) -> None:
        self.listeners.append(fn)

    def reset_projects(self) -> None:
        """The list of projects changed: forget resolved cwd -> project (a pid is re-resolved when it next uses CPU)."""
        self._proj.clear()

    def sample(self) -> Snapshot:
        now, own0 = self.clock(), self.thread_cpu()
        sysd = self.source.system()
        dt = (now - self._t) if self._t is not None else None
        procs = list(self.source.procs())
        keys = {(p.pid, p.create_time): p for p in procs}
        gone = set(self._prev) - set(keys)
        for k in gone:
            self._prev.pop(k, None)
            self._proj.pop(k, None)
        for k in set(self._proj) - set(keys):
            self._proj.pop(k, None)
        views: list[ProcView] = []
        ncores = max(1, len(sysd["cores"]))
        for k, p in keys.items():
            if p.cpu_time is None:
                continue
            prev = self._prev.get(k)
            self._prev[k] = p.cpu_time
            if prev is None or not dt or dt <= 0:
                continue
            delta = p.cpu_time - prev
            if delta <= 0:
                continue
            if p.name is None:
                try:
                    p.ppid, p.name = self.source.details(p.pid)
                except Exception:  # noqa: BLE001 - the process is gone or not readable
                    p.ppid, p.name = 0, "?"
            views.append(ProcView(p, delta / dt * 100.0, self._project_of(k, p, keys), self.source))
        projects: dict = {}
        for v in views:
            if v.project is None:
                continue
            e = projects.setdefault(v.project, {"cpu_pct": 0.0, "n": 0, "top_name": v.name, "top_pid": v.pid,
                                                "_top": -1.0})
            e["cpu_pct"] += v.cpu_pct
            e["n"] += 1
            if v.cpu_pct > e["_top"]:
                e["_top"], e["top_name"], e["top_pid"] = v.cpu_pct, v.name, v.pid
        for e in projects.values():
            del e["_top"]
        total_one_core = sysd["cpu_pct"] * ncores
        other = max(0.0, total_one_core - sum(e["cpu_pct"] for e in projects.values()))
        projects[None] = {"cpu_pct": other, "n": 0, "top_name": None, "top_pid": None}
        own = self.thread_cpu() - own0
        snap = Snapshot(self.wall(), sysd["cpu_pct"], sysd["cores"], tuple(sysd["load"]), sysd["mem_used"],
                        sysd["mem_total"], sysd["swap_used"], sysd["temp_c"], sysd["freq_mhz"], projects,
                        (own / dt * 100.0) if dt else 0.0)
        self._t = now
        for fn in list(self.listeners):
            try:
                fn(snap, views, gone)
            except Exception:  # noqa: BLE001 - a listener must not break the sampler
                log.exception("nodeload listener failed")
        return snap

    def _project_of(self, k, p: ProcInfo, keys: dict) -> str | None:
        if k in self._proj:
            return self._proj[k]
        project = None
        try:
            project = self.map.project_for(self.source.cwd(p.pid))
        except Exception:  # noqa: BLE001 - AccessDenied / NoSuchProcess / Zombie
            project = None
        if project is None:   # unknown cwd -> the project of the parent, if it is already resolved
            parent = next((kk for kk in self._proj if kk[0] == p.ppid and kk in keys), None)
            if parent is not None:
                project = self._proj[parent]
        self._proj[k] = project
        return project


# ------------------------------------------------------------------ storage

def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


class MinuteAggregator:
    """Collects snapshots; a finished minute becomes one batch (raw rows + the minute aggregate) in `ready`."""

    def __init__(self, host: str):
        self.host = host
        self.cur: list[Snapshot] = []
        self.minute: int | None = None
        self.ready: deque = deque(maxlen=BUFFER_MINUTES)

    def add(self, s: Snapshot) -> None:
        m = int(s.ts // 60)
        if self.minute is not None and m != self.minute:
            self._close()
        self.minute = m
        self.cur.append(s)

    def close_current(self) -> None:
        """Finish the minute in progress (on shutdown)."""
        self._close()

    def _close(self) -> None:
        snaps, m, h = self.cur, self.minute, self.host
        self.cur = []
        if not snaps:
            return
        raw = [(s.ts, h, s.cpu_pct, json.dumps([round(c, 1) for c in s.cores]), s.load[0], s.load[1], s.load[2],
                s.mem_used, s.mem_total, s.swap_used, s.temp_c, s.freq_mhz) for s in snaps]
        praw, pmin = [], {}
        for s in snaps:
            for proj, e in s.projects.items():
                if proj is not None and e["cpu_pct"] < PROC_MIN_PCT:
                    continue
                praw.append((s.ts, h, proj or "", e["cpu_pct"], e["n"], e["top_name"], e["top_pid"]))
            for proj, e in s.projects.items():
                a = pmin.setdefault(proj or "", {"cpu": [], "top": (-1.0, None)})
                a["cpu"].append(e["cpu_pct"])
                if e["top_name"] and e["cpu_pct"] > a["top"][0]:
                    a["top"] = (e["cpu_pct"], e["top_name"])
        n = len(snaps)
        ncore = len(snaps[0].cores)
        cores_avg = [round(sum(s.cores[i] for s in snaps if i < len(s.cores)) / n, 1) for i in range(ncore)]
        temps = [s.temp_c for s in snaps if s.temp_c is not None]
        mins = (m, h, n, sum(s.cpu_pct for s in snaps) / n, max(s.cpu_pct for s in snaps), json.dumps(cores_avg),
                sum(s.load[0] for s in snaps) / n, sum(s.mem_used for s in snaps) / n,
                max(s.mem_used for s in snaps), max(s.swap_used for s in snaps), _avg(temps),
                max(temps) if temps else None)
        pm = [(m, h, proj, sum(a["cpu"]) / n, max(a["cpu"]), a["top"][1]) for proj, a in pmin.items()
              if proj == "" or max(a["cpu"]) >= PROC_MIN_PCT]
        self.ready.append((raw, praw, mins, pm))

    def flush(self, db: sqlite3.Connection) -> int:
        """Write all finished minutes in one transaction; on error they stay buffered. Returns the number of minutes."""
        batches = list(self.ready)
        if not batches:
            return 0
        db.execute("BEGIN IMMEDIATE")
        try:
            for raw, praw, mins, pm in batches:
                db.executemany("INSERT OR REPLACE INTO node_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", raw)
                db.executemany("INSERT INTO node_proc_samples VALUES(?,?,?,?,?,?,?)", praw)
                db.execute("INSERT OR REPLACE INTO node_minutes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", mins)
                db.executemany("INSERT OR REPLACE INTO node_proc_minutes VALUES(?,?,?,?,?,?)", pm)
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        for _ in batches:
            self.ready.popleft()
        return len(batches)


def connect(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    path = path or config.load_db_path()
    if readonly:   # never creates the file
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0, isolation_level=None,
                             check_same_thread=False)
    else:
        config.ensure_home()
        db = sqlite3.connect(str(path), timeout=2.0, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=2000")
    if not readonly:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return db


def prune(db: sqlite3.Connection, now: float, raw_hours: float, minutes_days: float) -> None:
    raw_cut = now - raw_hours * 3600
    min_cut = int((now - minutes_days * 86400) // 60)
    db.execute("DELETE FROM node_samples WHERE ts < ?", (raw_cut,))
    db.execute("DELETE FROM node_proc_samples WHERE ts < ?", (raw_cut,))
    db.execute("DELETE FROM node_minutes WHERE minute < ?", (min_cut,))
    db.execute("DELETE FROM node_proc_minutes WHERE minute < ?", (min_cut,))


# ------------------------------------------------------------------ read API

PERIODS = {"1h": 3600, "24h": 86400, "30d": 30 * 86400}


def series(db: sqlite3.Connection, period: str, now: float | None = None) -> dict:
    """Chart points. 1h: raw (<= 720), 24h: minutes, 30d: minutes folded to 30 min."""
    if period not in PERIODS:
        raise ValueError(f"unknown period {period!r}")
    now = time.time() if now is None else now
    since = now - PERIODS[period]
    if period == "1h":
        rows = db.execute("SELECT ts, cpu_pct, cpu_pct AS cpu_max, load1, mem_used, temp_c FROM node_samples "
                          "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        step = 5
    elif period == "24h":
        rows = db.execute("SELECT minute*60.0 AS ts, cpu_avg AS cpu_pct, cpu_max, load1_avg AS load1, "
                          "mem_used_avg AS mem_used, temp_max AS temp_c FROM node_minutes WHERE minute >= ? "
                          "ORDER BY minute", (int(since // 60),)).fetchall()
        step = 60
    else:
        rows = db.execute("SELECT (minute/30)*1800.0 AS ts, sum(cpu_avg*n)/sum(n) AS cpu_pct, max(cpu_max) AS cpu_max, "
                          "sum(load1_avg*n)/sum(n) AS load1, sum(mem_used_avg*n)/sum(n) AS mem_used, "
                          "max(temp_max) AS temp_c FROM node_minutes WHERE minute >= ? GROUP BY minute/30 "
                          "ORDER BY minute/30", (int(since // 60),)).fetchall()
        step = 1800
    return {"period": period, "step_s": step, "now": now,
            "points": [{k: r[k] for k in ("ts", "cpu_pct", "cpu_max", "load1", "mem_used", "temp_c")} for r in rows]}


def top_projects(snaps: Iterable[Snapshot], since: float) -> list[dict]:
    """Average CPU (% of one core) per project over the snapshots newer than `since`, with the heaviest process name."""
    snaps = [s for s in snaps if s.ts >= since]
    if not snaps:
        return []
    n = len(snaps)
    acc: dict = {}
    for s in snaps:
        for proj, e in s.projects.items():
            a = acc.setdefault(proj, {"sum": 0.0, "max": 0.0, "top": (-1.0, None, None)})
            a["sum"] += e["cpu_pct"]
            a["max"] = max(a["max"], e["cpu_pct"])
            if e["top_name"] and e["cpu_pct"] > a["top"][0]:
                a["top"] = (e["cpu_pct"], e["top_name"], e["top_pid"])
    out = [{"project": proj, "cpu_pct": a["sum"] / n, "cpu_max": a["max"], "top_name": a["top"][1],
            "top_pid": a["top"][2]} for proj, a in acc.items()]
    out.sort(key=lambda x: (x["project"] is None, -x["cpu_pct"]))
    return out


def cpu_alert(snaps: Iterable[Snapshot], pct: float, minutes: float, interval: float, now: float) -> dict | None:
    """Sustained high CPU (ADR-018): walk back from the newest snapshot while `cpu_pct >= pct`.

    Dips below `pct` shorter than ALERT_GRACE_S keep the series; a time gap over 3 intervals (sleep) or a stale newest
    snapshot ends it. Active when the series has lasted `minutes`. The culprit is the heaviest project in the series.
    """
    snaps = list(snaps)
    if not snaps:
        return None
    gap = 3 * interval
    last = snaps[-1]
    if now - last.ts > gap:
        return None
    hot = None
    for i in range(len(snaps) - 1, -1, -1):
        s = snaps[i]
        if i < len(snaps) - 1 and snaps[i + 1].ts - s.ts > gap:
            break
        if s.cpu_pct >= pct:
            hot = i
        elif (snaps[hot].ts if hot is not None else last.ts) - s.ts > max(ALERT_GRACE_S, interval):
            break
    if hot is None:
        return None
    onset = snaps[hot].ts
    if last.ts - onset < minutes * 60:
        return None
    ranked = top_projects(snaps[hot:], onset)
    top = next((p for p in ranked if p["project"] is not None), ranked[0] if ranked else None)
    return {"since": onset, "minutes": (last.ts - onset) / 60, "pct": pct,
            "project": top["project"] if top else None, "top_name": top["top_name"] if top else None,
            "top_pid": top["top_pid"] if top else None, "cores": (top["cpu_pct"] / 100) if top else None}


# ------------------------------------------------------------------ runner

class Runner:
    """Owns the sampler thread: snapshot every `interval` s, write a batch once a minute."""

    def __init__(self, source, project_map: ProjectMap, *, interval: float = 5.0, raw_hours: float = 24.0,
                 minutes_days: float = 30.0, db_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 passive_tests: bool = True, alert_pct: float = 85.0, alert_minutes: float = 5.0):
        self.source = source
        self.alert_pct, self.alert_minutes = alert_pct, alert_minutes
        self.interval = max(MIN_INTERVAL, float(interval))
        self.raw_hours, self.minutes_days = raw_hours, minutes_days
        self.db_path = db_path
        self.wall = wall
        self.sampler = Sampler(source, project_map, clock, wall)
        self.map = project_map
        self.host = socket.gethostname()
        self.hostinfo: dict | None = None       # filled in the sampler thread (psutil / sysctl)
        self._roots_lock = threading.Lock()
        self._pending_roots: list | None = None
        self.agg = MinuteAggregator(self.host)
        self.passive = None
        if passive_tests:   # E6-03: the detector rides on the sampler thread, no thread of its own
            from myvec import passive

            self.passive = passive.Detector(source, project_map, lambda: self.host, self.interval)
            self.sampler.add_listener(self.passive)
        self.ring: deque[Snapshot] = deque(maxlen=RING)
        self.last: Snapshot | None = None
        self._db: sqlite3.Connection | None = None
        self._last_prune = 0.0
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="myvec-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _load_host(self) -> None:
        if self.hostinfo is None:
            try:
                self.hostinfo = dict(self.source.host_info())
                self.host = self.agg.host = self.hostinfo.get("hostname") or self.host
            except Exception:  # noqa: BLE001 - header info is optional
                self.hostinfo = {"hostname": self.host}

    def _loop(self) -> None:
        self._load_host()
        nxt = self._clock()
        while not self._stop.wait(max(0.0, nxt - self._clock())):
            try:
                self.step()
            except Exception:  # noqa: BLE001 - keep sampling
                log.exception("nodeload step failed")
            nxt += self.interval
            if nxt < self._clock():   # fell behind (suspend): do not burst
                nxt = self._clock() + self.interval
        try:
            self.agg.close_current()
            self._flush()
            if self._db is not None:
                self._db.close()
        except sqlite3.Error:
            pass

    def set_roots(self, roots: Iterable[tuple[str, str]]) -> None:
        """Thread-safe: applied by the sampler thread at the start of its next step."""
        with self._roots_lock:
            self._pending_roots = list(roots)

    def _apply_roots(self) -> None:
        with self._roots_lock:
            roots, self._pending_roots = self._pending_roots, None
        if roots is None:
            return
        new = sorted(((os.path.normpath(r), p) for p, r in roots), key=lambda x: -len(x[0]))
        if new != self.map.roots:
            self.map.set_roots([(p, r) for r, p in new])
            self.sampler.reset_projects()

    # -- one tick (called by the thread; tests call it directly)
    def step(self) -> Snapshot:
        self._load_host()
        self._apply_roots()
        snap = self.sampler.sample()
        self.last = snap
        self.ring.append(snap)
        self.agg.add(snap)
        if self.agg.ready:
            self._flush()
        return snap

    def _flush(self) -> None:
        try:
            if self._db is None:
                self._db = connect(self.db_path)
            self.agg.flush(self._db)
            now = self.wall()
            if now - self._last_prune >= PRUNE_EVERY:
                prune(self._db, now, self.raw_hours, self.minutes_days)
                self._last_prune = now
        except (sqlite3.Error, OSError) as e:   # busy / unavailable: batches stay buffered until the next minute
            log.warning("nodeload flush deferred: %s", e)

    # -- read API
    def _read(self) -> sqlite3.Connection | None:
        try:
            return connect(self.db_path, readonly=True)
        except sqlite3.Error:   # no load.db yet
            return None

    def alert_info(self) -> dict:
        """`alert` (or None) and the rule it was judged by; no actions are taken on processes."""
        return {"alert": cpu_alert(list(self.ring), self.alert_pct, self.alert_minutes, self.interval, self.wall()),
                "alert_rule": {"pct": self.alert_pct, "minutes": self.alert_minutes}}

    def current(self) -> dict:
        s = self.last
        out = {"enabled": True, "host": self.hostinfo or {"hostname": self.host}, "snapshot": s.to_dict() if s else None,
               "projects": self.projects(1)["projects"], "temp_max_24h": None, **self.alert_info()}
        temps = [x.temp_c for x in list(self.ring) if x.temp_c is not None]
        tmax = max(temps) if temps else None
        try:
            db = self._read()
            r = None
            if db is not None:
                try:
                    r = db.execute("SELECT max(temp_max) FROM node_minutes WHERE minute >= ?",
                                   (int((self.wall() - 86400) // 60),)).fetchone()
                finally:
                    db.close()
            if r and r[0] is not None:
                tmax = r[0] if tmax is None else max(tmax, r[0])
        except sqlite3.Error:
            pass
        out["temp_max_24h"] = tmax
        return out

    def series(self, period: str) -> dict:
        db = self._read()
        if db is None:
            return {"period": period, "step_s": 0, "now": self.wall(), "points": []}
        try:
            return series(db, period, self.wall())
        except sqlite3.OperationalError:   # tables not created yet
            return {"period": period, "step_s": 0, "now": self.wall(), "points": []}
        finally:
            db.close()

    def projects(self, minutes: float = 1.0) -> dict:
        now = self.wall()
        return {"minutes": minutes, "projects": top_projects(list(self.ring), now - minutes * 60)}

    def status(self) -> dict:
        recent = list(self.ring)[-120:]   # ~10 min
        return {"enabled": True, "interval_s": self.interval, "last_ts": self.last.ts if self.last else None,
                "sampler_cpu_pct": (sum(x.sampler_cpu_pct for x in recent) / len(recent)) if recent else 0.0,
                **self.alert_info()}
