"""Project-side scaffold state: `.myvec/scaffold.json` (ADR-028).

Holds the validated answers, the process version and a copy of the render as the base for a later 3-way merge.
Stdlib only; `read` never raises (a broken state is `invalid`, a missing one is `none`).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

STATE = ".myvec/scaffold.json"
FORMAT = 1
MAX_BYTES = 1024 * 1024
_MODES = {"create": "owned", "same": "owned", "overwrite": "owned", "keep": "keep", "append": "append"}


def build_modes(raw: dict, rendered: dict[str, str], modes: dict[str, str], version: int, norm) -> str:
    """Deterministic state text from explicit modes ({rel: owned|keep|append}); `rendered` supplies the base text."""
    files = {}
    for rel, mode in modes.items():
        if rel in rendered:
            text = rendered[rel]
            files[rel] = {"mode": mode, "sha256": hashlib.sha256(norm(rel, text).encode("utf8")).hexdigest(),
                          "base": text}
    data = {"format": FORMAT, "version": version, "answers": raw, "files": files}
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def build(raw: dict, rendered: dict[str, str], rows, version: int, norm) -> str:
    """Deterministic state text. `rows`: [(rel, action)] with the owner's choices applied (`labelled`); only files
    the scaffold owns whole and actually handled (not skip/skip-foreign/conflict) are recorded. `norm(rel, text)`
    drops the generated blocks before hashing."""
    modes = {rel: _MODES[act] for rel, act in rows if rel in rendered and act in _MODES}
    return build_modes(raw, rendered, modes, version, norm)


def _result(status: str, version=None, current=None, files=None, reason: str = "") -> dict:
    return {"status": status, "version": version, "current": current, "files": files or {}, "reason": reason}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def read(root) -> dict:
    """{status: none|current|outdated|newer|invalid, version, current, files: {rel: mode}, reason}. Never raises."""
    from myvec.scaffold import PROCESS_VERSION

    try:
        cur = Path(root)
        for part in Path(STATE).parts:
            cur = cur / part
            if cur.is_symlink():
                return _result("invalid", current=PROCESS_VERSION, reason="a symbolic link on the path")
        if not cur.exists():
            return _result("none", current=PROCESS_VERSION, reason="unknown version: no scaffold state")
        if not cur.is_file():
            return _result("invalid", current=PROCESS_VERSION, reason="not a regular file")
        if cur.stat().st_size > MAX_BYTES:
            return _result("invalid", current=PROCESS_VERSION, reason="file is too large")
        fd = os.open(cur, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            data = json.loads(f.read(MAX_BYTES + 1).decode("utf8"))
    except (OSError, ValueError, RecursionError):
        return _result("invalid", current=PROCESS_VERSION, reason="cannot read or parse the file")
    if not isinstance(data, dict):
        return _result("invalid", current=PROCESS_VERSION, reason="not a JSON object")
    fmt, ver = data.get("format"), data.get("version")
    if not _is_int(fmt) or fmt < 1 or not _is_int(ver) or ver < 1:
        return _result("invalid", current=PROCESS_VERSION, reason="`format` and `version` must be integers >= 1")
    files = data.get("files")
    listed = {k: v["mode"] for k, v in files.items()
              if isinstance(v, dict) and isinstance(k, str) and v.get("mode") in ("owned", "keep", "append")} \
        if isinstance(files, dict) else {}
    status = "newer" if fmt > FORMAT or ver > PROCESS_VERSION else "outdated" if ver < PROCESS_VERSION else "current"
    return _result(status, ver, PROCESS_VERSION, listed)


def load(root) -> tuple[dict, dict | None]:
    """Like `read`, but also returns the validated state data (answers, files with `mode` and `base`) or None unless
    the status is `current`/`outdated`. `read` itself is unchanged (E0-10 reads it)."""
    rep = read(root)
    if rep["status"] not in ("current", "outdated"):
        return rep, None
    try:
        fd = os.open(Path(root) / STATE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            data = json.loads(f.read(MAX_BYTES + 1).decode("utf8"))
    except (OSError, ValueError, RecursionError):
        return _result("invalid", current=rep["current"], reason="cannot read or parse the file"), None
    answers, files = data.get("answers"), data.get("files")
    if not isinstance(answers, dict):
        return _result("invalid", rep["version"], rep["current"], reason="`answers` must be an object"), None
    if not isinstance(files, dict):
        return _result("invalid", rep["version"], rep["current"], reason="`files` must be an object"), None
    for rel, v in files.items():
        if not isinstance(rel, str) or not isinstance(v, dict) or v.get("mode") not in ("owned", "keep", "append") \
                or not isinstance(v.get("base"), str):
            return _result("invalid", rep["version"], rep["current"], reason="a `files` entry is malformed"), None
    return rep, data
