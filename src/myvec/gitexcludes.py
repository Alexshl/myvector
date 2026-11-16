"""Keep `.myvector.toml` out of `git status` everywhere via the user's global git excludes file."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

LINE = ".myvector.toml"
_EQUIVALENT = {".myvector.toml", "**/.myvector.toml"}


def excludes_path() -> Path:
    """`core.excludesFile` (global) if set, else git's default `$XDG_CONFIG_HOME/git/ignore` (~/.config/git/ignore).

    Never sets core.excludesFile.
    """
    val = ""
    try:
        r = subprocess.run(["git", "config", "--global", "--type=path", "--get", "core.excludesFile"],
                           capture_output=True, text=True, timeout=5, env=os.environ.copy())
        if r.returncode == 0:
            val = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if val:
        p = Path(val).expanduser()
        return p if p.is_absolute() else Path.home() / p
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "git" / "ignore"


def ensure_marker_excluded() -> tuple[Path, bool]:
    """Append `.myvector.toml` to the global excludes file if absent. Returns (file, changed)."""
    p = excludes_path()
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    if any(ln.strip() in _EQUIVALENT for ln in existing.splitlines()):
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(LINE + "\n")
    return p, True
