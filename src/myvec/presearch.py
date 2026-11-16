"""PreToolUse `pre-search` classifier (E8-11, ADR-040): is this tool call a grep over the project tree?

Pure functions, stdlib only (`glob json os re shlex`). The command text is only inspected here, never stored or logged.
A heuristic without a real shell parser: when in doubt, stay silent (`None`).
"""
from __future__ import annotations

import glob
import os
import re
import shlex

QUIET_AFTER_SEARCH_S = 1800   # silence after `myvec search` in the same (session, agent)
HINT_EVERY_S = 600            # at most one hint per (session, agent) per this interval

HINT_TEMPLATE = (
    '[myvec] Indexed project: before grepping the tree, try myvec search "{q}" '
    "(add --kind docs for docs/ADR/tickets); keep grep for exact lines in files already found or a full count."
)

MAX_WORDS = 6
MAX_WORD = 40
_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_PLAIN_ALT = re.compile(r"[A-Za-z0-9_. -]+")
_VAR_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ALT_SPLIT = re.compile(r"\\\||\|")
_SEPARATORS = {"&&", "||", ";", "&", "(", ")", ";;", "&;"}
_PIPES = {"|", "|&"}
_REDIRECT_CHARS = set("<>")
_GREPS = ("grep", "egrep", "fgrep")
_TREE_TOOLS = ("rg", "ag", "ack")
_SKIP_DIRS = ("logs", "log", "tmp")   # log/temp trees are not the project's knowledge

# options that take a value, per tool family (short letters / long names)
_GREP_SHORT_VAL = set("efmABCdD")
_GREP_LONG_VAL = {"regexp", "file", "max-count", "after-context", "before-context", "context", "include", "exclude",
                  "exclude-from", "exclude-dir", "directories", "devices", "label", "binary-files"}
_RG_SHORT_VAL = set("efgmABCtTjMEdr")
_RG_LONG_VAL = {"regexp", "file", "glob", "iglob", "max-count", "after-context", "before-context", "context", "type",
                "type-not", "type-add", "threads", "max-columns", "encoding", "max-depth", "maxdepth", "sort", "sortr",
                "replace", "pre", "pre-glob", "ignore-file", "path-separator", "colors", "color", "engine", "dfa-size-limit",
                "regex-size-limit", "max-filesize", "context-separator", "field-context-separator",
                "field-match-separator", "hostname-bin", "hyperlink-format"}
_AG_SHORT_VAL = set("efgGmABCpGW")
_ACK_SHORT_VAL = set("ABCmgGtT")
_COUNT_SHORT = set("c")
_COUNT_LONG = {"count", "count-matches"}


def _tokens(cmd: str) -> list[str] | None:
    """Shell-like tokens with `&&`, `||`, `;`, `|` kept as separate tokens; redirects dropped. None when unparsable."""
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        return None
    out: list[str] = []
    skip = False
    for t in raw:
        if skip:
            skip = False
            continue
        if t and set(t) <= _REDIRECT_CHARS | {"&"} and set(t) & _REDIRECT_CHARS:
            if out and out[-1].isdigit():
                out.pop()   # the `2` of `2>/dev/null`
            skip = True   # the redirect target follows (`2>&1` -> `2`, `>&`, `1`)
            continue
        out.append(t)
    return out


def _stages(tokens: list[str]) -> list[list[list[str]]]:
    """tokens -> commands -> pipeline stages -> words."""
    cmds: list[list[list[str]]] = []
    cur: list[list[str]] = [[]]
    for t in tokens:
        if t in _SEPARATORS:
            cmds.append(cur)
            cur = [[]]
        elif t in _PIPES:
            cur.append([])
        else:
            cur[-1].append(t)
    cmds.append(cur)
    return [[s for s in c if s] for c in cmds if any(c)]


def _unwrap(w: list[str]) -> tuple[list[str], bool]:
    """Drop wrappers (`VAR=x`, sudo, env, timeout N, uv run, xargs + options). Returns (words, via_xargs)."""
    xargs = False
    while w:
        head = os.path.basename(w[0])
        if _VAR_ASSIGN.match(w[0]) or head in ("sudo", "env", "nohup", "command", "time", "exec", "nice"):
            w = w[1:]
        elif head == "timeout":
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[2:] if w[0] in ("-k", "-s", "--kill-after", "--signal") else w[1:]
            if w and re.fullmatch(r"[0-9.]+[smhd]?", w[0]):
                w = w[1:]
        elif head == "uv" and len(w) > 1 and w[1] == "run":
            w = w[2:]
            while w and w[0].startswith("-"):
                w = w[1:]
        elif head == "xargs":
            xargs = True
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[2:] if w[0] in ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "-l", "-i") else w[1:]
        else:
            break
    return w, xargs


def _parse_args(tool: str, args: list[str]) -> tuple[list[str], list[str], set[str], bool] | None:
    """(`-e` patterns, positionals, short flags seen, counting) or None when unclear."""
    if tool in _GREPS:
        sv, lv = _GREP_SHORT_VAL, _GREP_LONG_VAL
    elif tool == "rg":
        sv, lv = _RG_SHORT_VAL, _RG_LONG_VAL
    elif tool == "ag":
        sv, lv = _AG_SHORT_VAL, set()
    else:
        sv, lv = _ACK_SHORT_VAL, set()
    pats: list[str] = []
    pos: list[str] = []
    flags: set[str] = set()
    counting = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            pos.extend(args[i:])
            break
        if a.startswith("--") and len(a) > 2:
            name, eq, val = a[2:].partition("=")
            flags.add("--" + name)
            if name in _COUNT_LONG:
                counting = True
            if name == "files":
                return None
            if name in ("regexp", "file") or name in lv:
                if not eq:
                    if i >= len(args):
                        return None
                    val = args[i]
                    i += 1
                if name == "file":
                    return None   # patterns come from a file: unknown
                if name == "regexp":
                    pats.append(val)
            continue
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                flags.add(c)
                if c in _COUNT_SHORT:
                    counting = True
                if c in sv:
                    val = a[j + 1:]
                    if not val:
                        if i >= len(args):
                            return None
                        val = args[i]
                        i += 1
                    if c == "e":
                        pats.append(val)
                    elif c == "f":
                        return None
                    break
                j += 1
            continue
        pos.append(a)
    return pats, pos, flags, counting


def _tool_targets(tool: str, pats: list[str], pos: list[str], flags: set[str], via_xargs: bool,
                  piped: bool) -> tuple[list[str], list[str]] | None:
    """(patterns, paths) when this grep-like call scans a tree, else None."""
    if tool in _GREPS:
        if not via_xargs and not ({"r", "R", "--recursive", "--dereference-recursive"} & flags):
            return None
    elif piped and not via_xargs and len(pos) <= (0 if pats else 1):
        return None   # `... | rg foo` filters a stream, not a tree
    if pats:
        return pats, pos
    if not pos:
        return None
    return [pos[0]], pos[1:]


def _tree_target(paths: list[str], cwd: str, root: str) -> bool:
    """True if the call scans at least one directory and every scanned directory is inside `root` (not a log/tmp tree)."""
    if not paths:
        paths = ["."]
    dirs: list[str] = []
    for p in paths:
        if "$" in p or "`" in p:
            return False
        p = os.path.expanduser(p)
        full = p if os.path.isabs(p) else os.path.join(cwd, p)
        cands = glob.glob(full) if any(ch in p for ch in "*?[") else [full]
        for c in cands:
            if os.path.isdir(c):
                dirs.append(os.path.realpath(c))
    if not dirs:
        return False
    root = os.path.realpath(root)
    for d in dirs:
        if not (d == root or d.startswith(root + os.sep)):
            return False
        rel = d[len(root):].split(os.sep)
        if any(part in _SKIP_DIRS for part in rel):
            return False
    return True


def _alts(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        out.extend(a.strip() for a in _ALT_SPLIT.split(p) if a.strip())
    return out


def words(alts: list[str]) -> list[str]:
    """Sanitised words for the hint: `[A-Za-z0-9_.-]`, at most MAX_WORDS of MAX_WORD chars, no duplicates.
    An alternative with any other character (regex syntax, quotes, `$(...)`) is dropped as a whole."""
    out: list[str] = []
    for a in alts:
        if not _PLAIN_ALT.fullmatch(a):
            continue
        for w in _WORD.findall(a):
            if len(w) <= MAX_WORD and w not in out:
                out.append(w)
            if len(out) >= MAX_WORDS:
                return out
    return out


def hint(ws: list[str]) -> str:
    return HINT_TEMPLATE.format(q=" ".join(ws) if ws else "<question>")


def exploratory(tool: str, tool_input, cwd: str, root: str) -> list[str] | None:
    """Words for the hint when the call greps a directory tree inside `root`, else None. Never raises."""
    try:
        return _exploratory(tool, tool_input, cwd, root)
    except Exception:  # noqa: BLE001 - classifier must never break a hook
        return None


def _exploratory(tool: str, tool_input, cwd: str, root: str) -> list[str] | None:
    if not isinstance(tool_input, dict):
        return None
    if tool == "Grep":
        pat, path = tool_input.get("pattern"), tool_input.get("path")
        if not isinstance(pat, str) or not pat.strip():
            return None
        if not _tree_target([path] if isinstance(path, str) and path else [], cwd, root):
            return None
        return words(_alts([pat]))
    if tool != "Bash" or not isinstance(tool_input.get("command"), str):
        return None
    toks = _tokens(tool_input["command"])
    if toks is None:
        return None
    for cmd in _stages(toks):
        unwrapped = [_unwrap(s) for s in cmd]
        if any(w and os.path.basename(w[0]) == "wc" for w, _ in unwrapped):
            continue   # counting (`| wc -l`)
        for idx, (w, xargs) in enumerate(unwrapped):
            if not w:
                continue
            head = os.path.basename(w[0])
            if head not in _GREPS and head not in _TREE_TOOLS:
                continue
            parsed = _parse_args(head, w[1:])
            if parsed is None or parsed[3]:
                continue
            pats, pos, flags, _ = parsed
            tp = _tool_targets(head, pats, pos, flags, xargs, idx > 0)
            if tp is None:
                continue
            patterns, paths = tp
            if not any(p.strip() for p in patterns):
                continue
            if not _tree_target(paths, cwd, root):
                continue
            return words(_alts(patterns))
    return None


def is_myvec_search(tool: str, tool_input) -> bool:
    """Bash segment `myvec search|recall` (after wrappers)."""
    if tool != "Bash" or not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        return False
    try:
        toks = _tokens(tool_input["command"])
        if toks is None:
            return False
        for cmd in _stages(toks):
            w, _ = _unwrap(cmd[0])
            if len(w) > 1 and os.path.basename(w[0]) == "myvec" and w[1] in ("search", "recall"):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False
