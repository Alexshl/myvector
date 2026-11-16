"""Persistent search log (ADR-029): `logs/searches.jsonl`, one record per search, without the query text.

A record keeps the shape of the query (length, words, one-identifier flag, secret flag) and a keyed hash for spotting
repeats, never the text. The bus and `queries.jsonl` keep the text (the UI shows recent searches) but masked.
Stdlib + `filters`, `logutil`, `config`; nothing here raises into a search.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

from myvec import config, filters, logutil

MAX_BYTES = 8 * 1024 * 1024
HIDDEN = "•••"
VERSION = 1
VIAS = ("direct", "daemon", "ui")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_FIELDS = ("via", "project", "repo", "scope", "kind", "mode", "route", "results", "tokens", "ms", "fmt")


def path() -> Path:
    return config.logs_dir() / "searches.jsonl"


def salt_path() -> Path:
    return config.home() / "state" / "searchlog.salt"


def _salt() -> bytes:
    """32 random bytes, created once (`O_EXCL`, 0600)."""
    p = salt_path()
    for _ in range(2):
        try:
            data = p.read_bytes()
            if len(data) >= 32:
                return data[:32]
            p.unlink(missing_ok=True)  # empty/short (a crash between create and write): regenerate, never a weak salt
        except FileNotFoundError:
            pass
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    return hashlib.sha256(os.urandom(32)).digest()  # lost every race: a per-process salt beats a zero one


def mask_q(q: str) -> str:
    """The query as it may be stored: hard secret -> `•••`, literal secret values masked."""
    if not q:
        return q
    return HIDDEN if filters.has_hard_secret(q) else filters.mask_secrets(q)[0]


def _session(v) -> str | None:
    return v if isinstance(v, str) and _UUID.fullmatch(v) else None


def shape(q: str, *, session: str | None = None, ts: float | None = None, **fields) -> dict:
    """Record v1. No query text; `q_hash` is null for a query with a hard secret."""
    q = q or ""
    secret = filters.has_hard_secret(q)
    norm = " ".join(q.lower().split())
    qhash = None if secret or not norm else hmac.new(_salt(), norm.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    rec = {"ts": time.time() if ts is None else ts, "v": VERSION, "session": _session(session)}
    for k in _FIELDS:
        rec[k] = fields.get(k)
    if rec["via"] not in VIAS:
        rec["via"] = None
    words = q.split()
    rec.update(q_len=len(q), q_words=len(words), q_ident=len(words) == 1, q_secret=secret, q_hash=qhash)
    return rec


def record(rec: dict) -> None:
    """Append one record (0600; rotation at 8 MB into `.1`). Never raises."""
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
        logutil.append(p, json.dumps(rec, ensure_ascii=False), stamp=False, max_bytes=MAX_BYTES)
        if p.stat().st_mode & 0o777 != 0o600:  # rotation recreates the file with the umask
            os.chmod(p, 0o600)
    except Exception:  # noqa: BLE001 - a log must never break a search
        pass


def publish(bus, *, session: str | None, log_via: str | None = None, **ev) -> None:
    """The search event for the bus (masked `q`) plus the persistent record. One call per search. `log_via`
    overrides `via` in the record only (the UI's own searches are `ui`; the bus keeps `daemon` for its display)."""
    q = str(ev.get("q") or "")
    try:
        fields = {k: v for k, v in ev.items() if k in _FIELDS}
        if log_via:
            fields["via"] = log_via
        record(shape(q, session=session, **fields))
    except Exception:  # noqa: BLE001
        pass
    bus.publish("search", **{**ev, "q": mask_q(q)})


def read(since: float, until: float | None = None) -> list[dict]:
    """Records with `since <= ts < until`, oldest first (`searches.jsonl.1` then `searches.jsonl`)."""
    p = path()
    out: list[dict] = []
    for f in (p.with_name(p.name + ".1"), p):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            ts = r.get("ts") if isinstance(r, dict) else None
            if isinstance(ts, (int, float)) and ts >= since and (until is None or ts < until):
                out.append(r)
    return out
