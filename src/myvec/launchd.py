"""LaunchAgent for `myvec serve` (macOS). `MYVEC_LAUNCHAGENTS_DIR` overrides ~/Library/LaunchAgents (tests);
when it is set, launchctl is never called.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from myvec import config

LABEL = "com.myvec.serve"
BASE_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def agents_dir() -> Path:
    o = os.environ.get("MYVEC_LAUNCHAGENTS_DIR")
    return Path(o).expanduser() if o else Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def uv_tool_bin() -> str:
    """The uv tool bin dir (where `uv tool install` puts `myvec`)."""
    try:
        r = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.home() / ".local" / "bin")


def myvec_command() -> list[str]:
    exe = shutil.which("myvec")
    if exe:
        return [exe, "serve"]
    return [sys.executable, "-m", "myvec", "serve"]


def render_plist(command: list[str] | None = None, path_dirs: list[str] | None = None) -> bytes:
    command = command or myvec_command()
    dirs = path_dirs if path_dirs is not None else [uv_tool_bin(), str(Path(command[0]).parent), *BASE_PATH]
    seen: list[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    logs = config.logs_dir()
    env = {"PATH": ":".join(seen)}
    if os.environ.get("MYVEC_HOME"):
        env["MYVEC_HOME"] = str(config.home())
    data = {
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }
    return plistlib.dumps(data)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(load: bool = True) -> tuple[Path, bool]:
    """Write the plist; bootstrap it unless `load` is False or the agents dir is overridden."""
    config.ensure_home()
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(render_plist())
    loaded = False
    if load and not os.environ.get("MYVEC_LAUNCHAGENTS_DIR"):
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)  # reload if present
        for _ in range(10):  # bootout returns before the old job is gone: bootstrap then fails with an I/O error
            r = subprocess.run(["launchctl", "bootstrap", _domain(), str(p)], capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(0.5)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
        loaded = True
    return p, loaded


def uninstall() -> tuple[Path, bool]:
    p = plist_path()
    if not os.environ.get("MYVEC_LAUNCHAGENTS_DIR"):
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    existed = p.exists()
    if existed:
        p.unlink()
    return p, existed
