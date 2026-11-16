"""Project markers, hierarchy and repo discovery."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from myvec import filters
from myvec.config import _toml_str

MARKER = ".myvector.toml"
MAX_REPO_DEPTH = 4
RESERVED_TOP = {"global", "_archive", "_topics"}   # vault directories / the global memory scope
DEFAULT_TICKETS_DIR = "docs/todo"
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class MarkerRewriteRefused(ValueError):
    """The existing marker has content myvec cannot carry over verbatim; rewriting would silently drop it."""


class InvalidMarker(ValueError):
    """A .myvector.toml whose project name/path is unusable (path traversal, reserved name...)."""


def path_error(path: str) -> str | None:
    """Why `path` cannot be a project path (None if it is fine): segments of [A-Za-z0-9._-], no '.'/'..'/empty
    segment, not absolute, first segment not a reserved vault name."""
    if not path or path.startswith("/"):
        return "must be a relative path like 'name' or 'parent/name'"
    segs = path.split("/")
    for seg in segs:
        if seg in ("", ".", ".."):
            return f"segment {seg!r} is not allowed (no empty, '.' or '..' segments)"
        if not _SEGMENT.match(seg):
            return f"segment {seg!r} may contain only letters, digits, '.', '_' and '-'"
    if segs[0] in RESERVED_TOP:
        return f"{segs[0]!r} is reserved"
    return None


def tickets_dir_error(value: str) -> str | None:
    """Why `value` cannot be a tickets dir (None if fine): relative, no '..'/'.'/empty segments."""
    if not value or value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return "must be a relative path inside the project"
    for seg in value.rstrip("/").split("/"):
        if seg in ("", ".", ".."):
            return f"segment {seg!r} is not allowed"
    return None


@dataclass
class Marker:
    root: Path
    name: str
    parent: str | None = None
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    share_global: bool = True
    path: str = ""  # full project path, e.g. "project-b/site"
    tickets_dir: str = DEFAULT_TICKETS_DIR   # `[tickets] dir`, relative to root
    bad_tickets_dir: str | None = None       # the rejected raw `[tickets] dir` value (default is used instead)


@dataclass
class RepoSpec:
    path: str          # repo path, e.g. "project-b/site"
    dir: Path
    is_git: bool


def read_marker(root: Path) -> Marker | None:
    f = root / MARKER
    if not f.is_file():
        return None
    try:
        d = tomllib.loads(f.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    m = Marker(
        root=root,
        name=str(d.get("name") or root.name),
        parent=(str(d["parent"]) if d.get("parent") else None),
        include=[str(x) for x in d.get("include", [])],
        exclude=[str(x) for x in d.get("exclude", [])],
        share_global=bool(d.get("share_global", True)),
    )
    td = (d.get("tickets") or {}).get("dir") if isinstance(d.get("tickets"), dict) else None
    if td is not None:
        if isinstance(td, str) and tickets_dir_error(td) is None:
            m.tickets_dir = td.rstrip("/")
        else:
            m.bad_tickets_dir = str(td)
    m.path = _project_path(m)
    why = path_error(m.path)
    if why:
        raise InvalidMarker(f"invalid project name/path {m.path!r} in {f}: {why}")
    return m


def try_read_marker(root: Path) -> Marker | None:
    """read_marker, but an invalid marker is just "not a project" (hooks, discovery, rebuild)."""
    try:
        return read_marker(root)
    except InvalidMarker:
        return None


def _project_path(m: Marker) -> str:
    if "/" in m.name:
        return m.name.rstrip("/")    # a leading "/" is kept on purpose: path_error() rejects absolute paths
    if m.parent:
        return f"{m.parent.rstrip('/')}/{m.name}"
    # nested marker without explicit parent: inherit from nearest ancestor project
    anc = None
    if m.root.parent != m.root:
        try:
            anc = find_marker(m.root.parent)
        except InvalidMarker:
            anc = None
    return f"{anc.path}/{m.name}" if anc else m.name


def prospective_path(root: Path, name: str, parent: str | None) -> str:
    """The project path a marker with these fields would get (for validation before writing the marker)."""
    return _project_path(Marker(root=root, name=name, parent=parent))


def find_marker(start: str | Path) -> Marker | None:
    """Nearest ancestor (or self) of `start` containing a marker. Raises InvalidMarker if that marker is invalid."""
    p = Path(start).resolve()
    for d in [p, *p.parents]:
        if (d / MARKER).is_file():
            return read_marker(d)
    return None


def write_marker(root: Path, name: str, parent: str | None, include: list[str], exclude: list[str],
                 share_global: bool) -> None:
    lines = [f"name = {_toml_str(name)}"]
    if parent:
        lines.append(f"parent = {_toml_str(parent)}")
    lines.append(f"share_global = {'true' if share_global else 'false'}")
    lines.append("include = [" + ", ".join(_toml_str(x) for x in include) + "]")
    lines.append("exclude = [" + ", ".join(_toml_str(x) for x in exclude) + "]")
    lines += _preserved_toml(root / MARKER)
    _check_preserved(root / MARKER, lines)
    (root / MARKER).write_text("\n".join(lines) + "\n", encoding="utf-8")


_OWN_KEYS = {"name", "parent", "share_global", "include", "exclude"}


def _toml_value(v) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        items = [_toml_value(x) for x in v]
        return None if any(i is None for i in items) else "[" + ", ".join(items) + "]"
    return None


def _check_preserved(f: Path, new_lines: list[str]) -> None:
    """Refuse to rewrite when unknown content of the old marker would not survive (nested tables, dates, ...)."""
    try:
        old = tomllib.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return
    new = tomllib.loads("\n".join(new_lines) + "\n")
    lost = [k for k in old if k not in _OWN_KEYS and old[k] != new.get(k)]
    if lost:
        raise MarkerRewriteRefused(
            f"{MARKER} has settings myvec cannot preserve on rewrite ({', '.join(sorted(lost))}); "
            f"edit the file by hand")


def _preserved_toml(f: Path) -> list[str]:
    """Keys/tables of an existing marker that myvec does not own (e.g. `[tickets] dir`): kept on rewrite."""
    try:
        d = tomllib.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    scalars, tables = [], []
    for k, v in d.items():
        if k in _OWN_KEYS or not re.match(r"^[A-Za-z0-9_-]+$", k):
            continue
        if isinstance(v, dict):
            body = [f"{kk} = {tv}" for kk, vv in v.items()
                    if re.match(r"^[A-Za-z0-9_-]+$", kk) and (tv := _toml_value(vv)) is not None]
            if body:
                tables += ["", f"[{k}]", *body]
        elif (tv := _toml_value(v)) is not None:
            scalars.append(f"{k} = {tv}")
    return scalars + tables


def top_level(project_path: str) -> str:
    return project_path.split("/", 1)[0]


def ancestors(project_path: str) -> list[str]:
    """['project-b/site', 'project-b'] for 'project-b/site' (self first)."""
    parts = project_path.split("/")
    return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]


def exclude_spec(marker: Marker):
    """gitignore-style PathSpec of the marker's `exclude` patterns (None if there are none)."""
    if not marker.exclude:
        return None
    import pathspec

    return pathspec.PathSpec.from_lines("gitignore", marker.exclude)


def is_excluded_dir(exc, rel: str) -> bool:
    """rel: posix path of a directory relative to the project root."""
    return exc is not None and exc.match_file(rel.rstrip("/") + "/")


def discover(marker: Marker) -> tuple[list[RepoSpec], list[Marker]]:
    """Repos of a project + child project markers found underneath (not descended into)."""
    root = marker.root
    repos: list[RepoSpec] = []
    children: list[Marker] = []
    exc = exclude_spec(marker)
    root_is_git = (root / ".git").exists()
    if root_is_git:
        repos.append(RepoSpec(marker.path, root, True))
    else:
        repos.append(RepoSpec(marker.path, root, False))  # pseudo-repo, branch '-'

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or filters.is_denied_dir(e.name):
                continue
            sub = Path(e.path)
            if is_excluded_dir(exc, sub.relative_to(root).as_posix()):  # `exclude = ["site-cloud"]` skips the dir
                continue
            if (sub / MARKER).is_file():
                cm = try_read_marker(sub)
                if cm:
                    children.append(cm)
                continue
            if (sub / ".git").exists():
                rel = sub.relative_to(root).as_posix()
                repos.append(RepoSpec(f"{marker.path}/{rel}", sub, True))
            if depth + 1 < MAX_REPO_DEPTH:
                walk(sub, depth + 1)

    walk(root, 0)
    return repos, children


def nested_dirs(marker: Marker, repos: list[RepoSpec], children: list[Marker], of: RepoSpec) -> set[Path]:
    """Dirs inside `of` that belong to other repos / child projects (to be excluded)."""
    out = {r.dir for r in repos if r.dir != of.dir and of.dir in r.dir.parents}
    out |= {c.root for c in children if of.dir in c.root.parents}
    return out
