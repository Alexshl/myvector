"""Tiny stdlib client for the local daemon (`myvec serve`). Every failure means "no daemon": callers fall back."""
from __future__ import annotations

import http.client
import json
import os
import re

from myvec import config

CONNECT_TIMEOUT = 0.15


def read_daemon() -> dict | None:
    """daemon.json ({port, pid, token}) if it exists and its pid is alive."""
    try:
        d = json.loads(config.daemon_json_path().read_text(encoding="utf-8"))
        os.kill(int(d["pid"]), 0)
        int(d["port"])
        str(d["token"])
        return d
    except (OSError, ValueError, KeyError, TypeError):
        return None


def request(method: str, path: str, body: dict | None = None, *, read_timeout: float = 120.0,
            connect_timeout: float = CONNECT_TIMEOUT, info: dict | None = None) -> dict | None:
    """JSON request to the daemon; returns the decoded JSON body, or None on any failure (incl. non-200)."""
    info = info or read_daemon()
    if info is None:
        return None
    port = int(info["port"])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=connect_timeout)
    try:
        conn.connect()
        conn.sock.settimeout(read_timeout)
        payload = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
        headers = {"Host": f"127.0.0.1:{port}", "X-CSRF-Token": str(info["token"])}
        session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if session and re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session):
            headers["X-Myvec-Session"] = session
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        r = conn.getresponse()
        data = r.read()
        if r.status != 200:
            return None
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
