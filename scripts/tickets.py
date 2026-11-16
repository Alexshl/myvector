#!/usr/bin/env python3
"""Обгортка над `myvec ticket`: `uv run python scripts/tickets.py check|index`.
check = `myvec ticket check --strict`. Один парсер — `myvec.tickets`."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from myvec.ticketcli import ticket_app  # noqa: E402

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        ticket_app(["check", "--strict", "--root", str(ROOT)])
    elif cmd == "index":
        ticket_app(["index", "--root", str(ROOT)])
    print("використання: uv run python scripts/tickets.py check|index", file=sys.stderr)
    sys.exit(2)
