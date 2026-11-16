"""systemd user unit for `myvec serve` (Linux). `MYVEC_SYSTEMD_DIR` overrides ~/.config/systemd/user (tests);
when it is set, systemctl is never called.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from myvec import config
from myvec.launchd import myvec_command, uv_tool_bin

UNIT = "myvec.service"
BASE_PATH = ["/usr/local/bin", "/usr/bin", "/bin"]


def unit_dir() -> Path:
    o = os.environ.get("MYVEC_SYSTEMD_DIR")
    if o:
        return Path(o).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT


def _q(s: str) -> str:
    """Quote for ExecStart / Environment (systemd splits on whitespace, honours double quotes, expands %)."""
    s = s.replace("%", "%%")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"' if any(c in s for c in ' \t"\\') else s


def render_unit(command: list[str] | None = None, path_dirs: list[str] | None = None) -> str:
    command = command or myvec_command()
    dirs = path_dirs if path_dirs is not None else [uv_tool_bin(), str(Path.home() / ".local" / "bin"),
                                                    str(Path(command[0]).parent), *BASE_PATH]
    seen: list[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    logs = config.logs_dir()
    env = [f"PATH={':'.join(seen)}"]
    if os.environ.get("MYVEC_HOME"):
        env.append(f"MYVEC_HOME={config.home()}")
    return (
        "[Unit]\n"
        "Description=myvec daemon (local semantic code search + memory)\n"
        "\n"
        "[Service]\n"
        f"ExecStart={' '.join(_q(c) for c in command)}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "TimeoutStopSec=15\n"
        + "".join(f"Environment={_q(e)}\n" for e in env)
        + f"StandardOutput=append:{logs / 'daemon.out.log'}\n"
        f"StandardError=append:{logs / 'daemon.err.log'}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _ctl(*args: str, check: bool = True) -> None:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")


def install(load: bool = True) -> tuple[Path, bool]:
    """Write the unit; enable + (re)start it unless `load` is False or the unit dir is overridden."""
    config.ensure_home()
    p = unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_unit(), encoding="utf-8")
    loaded = False
    if load and not os.environ.get("MYVEC_SYSTEMD_DIR"):
        _ctl("daemon-reload")
        _ctl("enable", "--now", UNIT)
        _ctl("restart", UNIT)  # `enable --now` leaves a running (older) daemon alone: restart so upgrades take effect
        loaded = True
    return p, loaded


def uninstall() -> tuple[Path, bool]:
    p = unit_path()
    managed = not os.environ.get("MYVEC_SYSTEMD_DIR")
    if managed:
        _ctl("disable", "--now", UNIT, check=False)
    existed = p.exists()
    if existed:
        p.unlink()
    if managed:
        _ctl("daemon-reload", check=False)
    return p, existed
