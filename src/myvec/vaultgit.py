"""The vault is a local git repo (no remote, no push): every memory change becomes a commit.

Everything here is best effort: it must be fast and must never make a command fail (errors go to
logs/vault-git.log).
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager

from myvec import config, logutil

GITIGNORE = ".obsidian/workspace*.json\n.trash/\n"

# the vault repo is independent of the user's git setup: no global/system config (hooks, signing, templates)
_ID = ["-c", "user.name=myvec", "-c", "user.email=myvec@localhost", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null"]


def _log(msg: str) -> None:
    logutil.append(config.logs_dir() / "vault-git.log", msg)


def _git(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", *_ID, "-C", str(config.vault_dir()), *args], capture_output=True, env=env,
                          timeout=20)


def ensure_repo() -> bool:
    """git init the vault (with .gitignore) on first use. True if the vault is a git repo afterwards."""
    v = config.vault_dir()
    try:
        if (v / ".git").exists():
            return True
        v.mkdir(parents=True, exist_ok=True)
        r = _git("init", "-q", "-b", "main")
        if r.returncode != 0:
            _git("init", "-q")  # git < 2.28 has no -b
        gi = v / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE, encoding="utf-8")
        _git("add", ".gitignore")  # existing notes are committed by the first real change, with its message
        _git("commit", "-q", "-m", "myvec: init vault")
        return (v / ".git").exists()
    except Exception as e:  # noqa: BLE001 - never fail the command
        _log(f"init failed: {e!r}")
        return False


LOCK_WAIT_S = 30.0


@contextmanager
def _commit_lock(wait: float = LOCK_WAIT_S):
    """Serialize add/commit across processes (CLI, daemon, lifecycle): concurrent git runs collide on
    `.git/index.lock`, one of them fails and its change stays uncommitted. The lock file is outside the vault repo."""
    config.ensure_home()
    f = open(config.home() / "vault-git.lock", "a+")
    got = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        if got:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def autocommit(message: str) -> bool:
    """Commit everything pending in the vault. Returns True if a commit was made."""
    try:
        with _commit_lock() as got:
            if not got:
                _log(f"autocommit skipped, vault git lock busy ({message})")
                return False
            if not ensure_repo():
                return False
            _git("add", "-A")
            if _git("diff", "--cached", "--quiet").returncode == 0:
                return False  # nothing to commit
            r = _git("commit", "-q", "-m", message)
            if r.returncode != 0:
                _log(f"commit failed ({message}): {r.stderr.decode(errors='replace')[:300]}")
                return False
            return True
    except Exception as e:  # noqa: BLE001
        _log(f"autocommit failed ({message}): {e!r}")
        return False


def last_commit_time() -> float | None:
    """Unix time of the newest vault commit (None if the vault is not a git repo or has no commits). Read-only."""
    try:
        if not (config.vault_dir() / ".git").exists():
            return None
        r = _git("log", "-1", "--format=%ct")
        out = r.stdout.decode().strip()
        return float(out) if r.returncode == 0 and out else None
    except Exception:  # noqa: BLE001
        return None
