"""Thin git helpers (subprocess; read-only, no index locks)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

def _env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def run(dirpath: str | Path, *args: str, check: bool = True) -> bytes:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", str(dirpath), *args],
        capture_output=True, env=_env(),
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {dirpath}: {r.stderr.decode(errors='replace')[:300]}")
    return r.stdout


def is_repo(d: str | Path) -> bool:
    return (Path(d) / ".git").exists()


def _git_dir(d: Path) -> Path | None:
    g = d / ".git"
    if g.is_dir():
        return g
    if g.is_file():
        try:
            line = g.read_text().strip()
            if line.startswith("gitdir:"):
                p = Path(line[7:].strip())
                return p if p.is_absolute() else (d / p).resolve()
        except OSError:
            return None
    return None


def head_branch(d: str | Path) -> str | None:
    """Current branch by reading .git/HEAD (no subprocess). 'HEAD@<sha7>' when detached."""
    gd = _git_dir(Path(d))
    if gd is None:
        return None
    try:
        head = (gd / "HEAD").read_text().strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return "HEAD@" + head[:7]


def head_commit(d: str | Path) -> str | None:
    try:
        out = run(d, "rev-parse", "--short", "HEAD", check=False).decode().strip()
    except OSError:
        return None
    return out or None


def local_branches(d: str | Path) -> set[str]:
    out = run(d, "branch", "--list", "--format=%(refname:short)", check=False).decode()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def ls_files_staged(d: str | Path) -> list[tuple[str, str, str]]:
    """[(mode, sha, path)] for stage-0 entries (gitlinks/symlinks excluded)."""
    out = run(d, "ls-files", "-s", "-z")
    res = []
    for ent in out.split(b"\0"):
        if not ent:
            continue
        meta, _, path = ent.partition(b"\t")
        mode, sha, stage = meta.decode().split()
        if stage != "0" or mode in ("160000", "120000"):
            continue
        res.append((mode, sha, path.decode("utf8", "replace")))
    return res


def ls_untracked(d: str | Path) -> list[str]:
    out = run(d, "ls-files", "-o", "--exclude-standard", "-z")
    return [p.decode("utf8", "replace") for p in out.split(b"\0") if p and not p.endswith(b"/")]


def status_dirty(d: str | Path) -> tuple[set[str], set[str]]:
    """(worktree-modified paths needing rehash, worktree-deleted paths)."""
    out = run(d, "status", "--porcelain", "-z", "--untracked-files=no")
    parts = out.split(b"\0")
    dirty: set[str] = set()
    deleted: set[str] = set()
    i = 0
    while i < len(parts):
        ent = parts[i]
        i += 1
        if len(ent) < 4:
            continue
        x, y = chr(ent[0]), chr(ent[1])
        path = ent[3:].decode("utf8", "replace")
        if x in "RC":
            i += 1  # skip the "orig path" entry
        if y == "D":
            deleted.add(path)
        elif y != " " or x == "U":
            dirty.add(path)
    return dirty, deleted


# ---------------------------------------------------------------- freshness / branch lifecycle

def fingerprint(d: str | Path, is_git: bool) -> str:
    """Cheap change signature (a few stat calls, no subprocess): .git/HEAD text + mtime of .git/index.

    Non-git (pseudo) repos: newest mtime among the dir itself and its top-level directories.
    """
    d = Path(d)
    if is_git:
        gd = _git_dir(d)
        if gd is None:
            return "nogit"
        try:
            head = (gd / "HEAD").read_text().strip()
        except OSError:
            head = ""
        try:
            idx = (gd / "index").stat().st_mtime_ns
        except OSError:
            idx = 0
        return f"{head}|{idx}"
    newest = 0
    try:
        newest = d.stat().st_mtime_ns
        with os.scandir(d) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime_ns)
    except OSError:
        pass
    return f"-|{newest}"


def branch_exists(d: str | Path, branch: str) -> bool:
    r = subprocess.run(["git", "-C", str(d), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                       capture_output=True, env=_env())
    return r.returncode == 0


def default_branch(d: str | Path) -> str | None:
    """main/master/dev: origin/HEAD target if it exists locally, else the first of main/master/dev that exists."""
    out = run(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False).decode().strip()
    if out:
        name = out.split("/", 1)[1] if "/" in out else out
        if branch_exists(d, name):
            return name
    for cand in ("main", "master", "dev"):
        if branch_exists(d, cand):
            return cand
    return None


def rev(d: str | Path, ref: str) -> str | None:
    out = run(d, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).decode().strip()
    return out or None


def is_ancestor(d: str | Path, a: str, b: str) -> bool:
    r = subprocess.run(["git", "-C", str(d), "merge-base", "--is-ancestor", a, b], capture_output=True, env=_env())
    return r.returncode == 0


def merged_by_merge_commit(d: str | Path, tip: str, default: str, limit: int = 500) -> bool:
    """True if `tip` is the second (or later) parent of a recent merge commit on `default`."""
    out = run(d, "log", "--merges", "--format=%P", f"-n{limit}", default, check=False).decode()
    for ln in out.splitlines():
        if tip in ln.split()[1:]:
            return True
    return False


def merge_base(d: str | Path, a: str, b: str) -> str | None:
    """Full sha of the best common ancestor of two refs (None if unrelated / unknown)."""
    out = run(d, "merge-base", a, b, check=False).decode().strip()
    return out or None


def hash_object(d: str | Path, rel: str, data: bytes) -> str | None:
    """Blob sha git would store for `data` at path `rel` (applies clean filters / eol conversion, writes nothing)."""
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(d), "hash-object", f"--path={rel}", "--stdin"],
                           input=data, capture_output=True, env=_env())
    except OSError:
        return None
    return r.stdout.decode().strip() or None if r.returncode == 0 else None
