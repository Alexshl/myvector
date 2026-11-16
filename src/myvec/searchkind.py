"""Sub-kind of a self-search tool call (E8-16, ADR-042): which grep/find/Grep/Glob calls are exploratory.

Pure functions, stdlib + the argument parser of `presearch` (E8-11). The command text, pattern and path are only
inspected here and never stored: callers keep the label (`kind`) and the doc flag. Unlike `presearch.exploratory`
nothing touches the file system (the history is classified long after the fact), the target is judged by its text.
"""
from __future__ import annotations

import os
import re

from myvec import presearch as P

DOC_EXTS = (".md", ".markdown", ".mdx", ".rst", ".adoc")   # `--kind docs`: a path filter over code chunks (ADR-032)

KINDS = ("logs", "count", "file", "explore", "exact", "name")
EXPLORATORY = ("explore", "exact")   # the denominator of the share (with unclassified, which is `kind` NULL)

_LOG_DIRS = ("logs", "log", "tmp")
_LOG_ABS = ("/tmp", "/private/tmp", "/var/log")
_REGEX = re.compile(r"\.[*+]|\[|\(|\\[wsdbWSDB]")
_GLOB = re.compile(r"[*?\[$`]")
_FIND_TOOLS = ("find", "fd", "fdfind")
_PATTERN_TOOLS = ("grep", "egrep", "fgrep", "rg", "ag", "ack")


def is_doc(s: str | None) -> bool:
    return isinstance(s, str) and s.strip().lower().endswith(DOC_EXTS)


def _is_logs(paths: list[str]) -> bool:
    for p in paths:
        p = p.strip().rstrip("/") or "/"
        if p.endswith(".log") or any(p == a or p.startswith(a + "/") for a in _LOG_ABS):
            return True
        if any(part in _LOG_DIRS for part in p.split("/")):
            return True
    return False


def _is_file(p: str) -> bool:
    """A path that looks like one file: an extension on the last component, no glob/variable."""
    base = os.path.basename(p.rstrip("/"))
    return bool(base) and not _GLOB.search(p) and "." in base.lstrip(".") and base not in (".", "..")


def _search_kind(patterns: list[str], paths: list[str], flat_files: bool) -> str:
    """logs > file > explore > exact for a pattern search (the caller handles counting before `file`)."""
    if paths and (flat_files or all(_is_file(p) for p in paths)):
        return "file"
    if len(patterns) > 1 or len(P._alts(patterns)) > 1 or any(_REGEX.search(p) for p in patterns):
        return "explore"
    return "exact"


def _tool(tool_input: dict, name: str) -> tuple[str | None, bool]:
    pat, path, glob = tool_input.get("pattern"), tool_input.get("path"), tool_input.get("glob")
    path = path if isinstance(path, str) and path else None
    if name == "Glob":
        place = [x for x in (path, pat) if isinstance(x, str)]
        kind = "logs" if _is_logs(place) else "name"
        return kind, any(is_doc(x) for x in place)
    if not isinstance(pat, str) or not pat.strip():
        return None, False
    paths = [path] if path else []
    typ = tool_input.get("type")
    doc = is_doc(path) or is_doc(glob) or (isinstance(typ, str) and typ.lower() in ("md", "markdown"))
    if _is_logs(paths):
        return "logs", doc
    if tool_input.get("output_mode") == "count":
        return "count", doc
    return _search_kind([pat], paths, False), doc


def _find_paths(args: list[str]) -> list[str]:
    out = []
    for a in args:
        if a.startswith("-") or a in ("(", "!", ")"):
            break
        out.append(a)
    return out


def _bash_stage(w: list[str], xargs: bool, counted: bool) -> tuple[str | None, bool] | None:
    """(kind, doc) of one unwrapped pipeline stage if it is a self-search, else None (try the next command)."""
    head = os.path.basename(w[0])
    args = w[1:]
    if head in _FIND_TOOLS:
        if head == "find":
            paths = _find_paths(args)
        else:
            pos = [a for a in args if not a.startswith("-")]
            paths = pos[1:2]
        doc = any(is_doc(a) for a in args) or any(a in ("md", "markdown") for a in args)
        if _is_logs(paths) or _is_logs([a for a in args if a.endswith(".log")]):
            return "logs", doc
        return ("count" if counted else "name"), doc
    if head == "ls":
        if not any((a.startswith("-") and not a.startswith("--") and "R" in a) or a == "--recursive" for a in args):
            return None
        paths = [a for a in args if not a.startswith("-")]
        return ("logs" if _is_logs(paths) else "name"), any(is_doc(a) for a in paths)
    if head not in _PATTERN_TOOLS:
        return None
    parsed = P._parse_args(head, args)
    if parsed is None:
        return None, False   # recognised as a search, but the arguments are unclear: unclassified
    pats, pos, flags, counting = parsed
    if pats:
        patterns, paths = pats, pos
    elif pos:
        patterns, paths = [pos[0]], pos[1:]
    else:
        return None, False
    skip = set(patterns)
    doc = any(is_doc(a) for a in args if a not in skip)
    for i, a in enumerate(args):
        if a in ("-t", "--type") and i + 1 < len(args) and args[i + 1].lower() in ("md", "markdown"):
            doc = True
        if a.lower() in ("-tmd", "--type=md", "--type=markdown", "-tmarkdown"):
            doc = True
    recursive = bool({"r", "R", "--recursive", "--dereference-recursive"} & flags) or head in P._TREE_TOOLS or xargs
    if not recursive and not paths:
        return None, False   # filters a stream we cannot see
    if _is_logs(paths):
        return "logs", doc
    if counting or counted:
        return "count", doc
    return _search_kind(patterns, paths, not recursive), doc


def kind_of(name: str | None, tool_input) -> tuple[str | None, bool]:
    """`(kind, doc)` of a call that `tokens.classify` named `self`. `kind` is one of `KINDS` or None (unclassified:
    unparsable command, unclear arguments); `doc` is True when the target looks like documentation. Never raises."""
    try:
        if not isinstance(tool_input, dict):
            return None, False
        if name in ("Grep", "Glob"):
            return _tool(tool_input, name)
        cmd = tool_input.get("command")
        if name != "Bash" or not isinstance(cmd, str):
            return None, False
        toks = P._tokens(cmd)
        if toks is None:
            return None, False
        for stages in P._stages(toks):
            unwrapped = [P._unwrap(s) for s in stages]
            w, xargs = unwrapped[0]
            if not w:
                continue
            counted = any(x and os.path.basename(x[0]) == "wc" for x, _ in unwrapped[1:])
            res = _bash_stage(w, xargs, counted)
            if res is not None:
                return res
        return None, False
    except Exception:  # noqa: BLE001 - the classifier must never break a transcript scan
        return None, False
