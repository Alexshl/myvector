"""What never gets indexed: denied paths, binaries, big files, secrets."""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

MAX_FILE_BYTES = 1_000_000

DENY_DIRS = {
    "vendor", "node_modules", "dist", "build", "templates_c", "graphify-out", ".git",
    ".cache", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".nuxt", ".gradle", ".idea", ".vscode", "target", "coverage", ".parcel-cache",
    ".svn", ".hg", "bower_components", ".terraform", ".sass-cache", "cache", "tmp",
}
# graphify-out is denied except its report
ALLOW_FILES = {"graphify-out/GRAPH_REPORT.md"}

DENY_FILE_GLOBS = [
    ".env", ".env.*", "*.env", ".dev.vars", ".dev.vars.*", "*password*", "*secret*", "*.pem", "*.key", "id_*",
    "*.sql", "*.sqlite", "*.sqlite3", "*.db", "*.min.*",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock", "Cargo.lock",
    "poetry.lock", "uv.lock", "Gemfile.lock", "Pipfile.lock", "*.lock", "go.sum",
    ".DS_Store", ".myvector.toml", "*.bak-*",
]
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".pdf", ".zip", ".gz", ".tar",
    ".tgz", ".bz2", ".xz", ".7z", ".rar", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".mov", ".avi", ".wav", ".ogg", ".flac", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".pyc", ".o", ".a", ".bin", ".dat", ".psd", ".ai", ".svg", ".svgz", ".map", ".wasm", ".iso", ".dmg",
}


def is_denied_dir(name: str) -> bool:
    return name in DENY_DIRS or (name.startswith(".") and name not in {".github"})


def is_denied_path(rel: str) -> bool:
    """rel: posix path relative to a repo/project root."""
    if rel in ALLOW_FILES:
        return False
    p = PurePosixPath(rel)
    parts = p.parts
    for d in parts[:-1]:
        if d in DENY_DIRS or (d.startswith(".") and d != ".github"):
            return True
    name = parts[-1]
    lname = name.lower()
    for g in DENY_FILE_GLOBS:
        if fnmatch.fnmatchcase(lname, g.lower()):
            return True
    return p.suffix.lower() in BINARY_EXTS


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


MASK = "•••"

# Chunks containing any of these are dropped entirely (and never stored / embedded / remembered).
HARD_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"),   # a bare END is as good as a BEGIN
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b[sr]k_live_[0-9A-Za-z]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
]

_BEGIN_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
_END_KEY = re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
_KEY_BODY = re.compile(r"^\s*(?:[A-Za-z0-9+/=_-]{8,}|[A-Za-z-]+:.*|)\s*$")   # base64 line, PEM header, blank


def private_key_ranges(text: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of PEM private keys in a whole file (BEGIN..END). A BEGIN without END runs on
    while the lines still look like key material; a bare END reaches upwards the same way. Chunks overlapping any of
    these ranges must be dropped: the middle of a long key has no marker of its own."""
    lines = text.split("\n")
    ranges: list[tuple[int, int]] = []
    open_at: int | None = None
    for i, ln in enumerate(lines, 1):
        if open_at is None:
            if _BEGIN_KEY.search(ln):
                if _END_KEY.search(ln):
                    ranges.append((i, i))
                else:
                    open_at = i
            elif _END_KEY.search(ln):  # END without BEGIN: walk up over the key body
                j = i
                while j > 1 and _KEY_BODY.match(lines[j - 2]) and lines[j - 2].strip():
                    j -= 1
                ranges.append((j, i))
        elif _END_KEY.search(ln):
            ranges.append((open_at, i))
            open_at = None
    if open_at is not None:  # BEGIN without END
        j = open_at
        while j < len(lines) and _KEY_BODY.match(lines[j]):
            j += 1
        ranges.append((open_at, j))
    return ranges


def overlaps_ranges(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= re_ and end >= rs for rs, re_ in ranges)


# `<key><op><value>`: the key is any identifier CONTAINING a secret-ish word (SECRET_KEY, AWS_SECRET_ACCESS_KEY,
# $db_pass, dbPassword...); the value is a quoted literal or a bare literal-looking token.
_KEYWORDS = (r"password|passwd|pass|pwd|secret|token|api[_-]?key|access_?key|private_?key|secret_?key|auth_?key|"
             r"client_?secret|dsn")
_ASSIGN = re.compile(
    rf"""(?<![\w$])(?P<key>[\w$]*?(?:{_KEYWORDS})\w*)(?P<q>["']?)(?P<op>\s*(?:=>|:=|=|:)\s*)"""
    r"""(?:(?P<dq>"[^"\n]{4,}")|(?P<sq>'[^'\n]{4,}')|(?P<bare>[^\s'"`,;)\[\]}]{6,}))""",
    re.IGNORECASE,
)
# PHP `define('DB_PASSWORD', 'value')` - the name and the value are separate string literals
_DEFINE = re.compile(
    rf"""(?P<head>\bdefine\s*\(\s*(?P<nq>["'])[\w$]*(?:{_KEYWORDS}|key)\w*(?P=nq)\s*,\s*)"""
    r"""(?:(?P<dq>"[^"\n]{4,}")|(?P<sq>'[^'\n]{4,}'))""",
    re.IGNORECASE,
)
_TEMPLATE_DQ = re.compile(r"\{\{|\$\{|\{\$|\$[A-Za-z_]|%\(|%s|#\{|<%")
_TEMPLATE_SQ = re.compile(r"\{\{|%\(|%s")
_PURE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z_.]*$")


def _bare_is_literal(v: str) -> bool:
    if v[0] in "$@&*%!<>" or "->" in v or "::" in v or "(" in v:  # variable, reference, call
        return False
    return not _PURE_IDENT.match(v)  # identifiers / member access: `token = refresh`, `self.token`


def has_hard_secret(text: str) -> bool:
    """Private keys and well-known token formats: the whole chunk must not be stored."""
    return any(p.search(text) for p in HARD_SECRET_PATTERNS)


has_secret = has_hard_secret  # backwards-compatible name


def mask_secrets(text: str) -> tuple[str, int]:
    """Replace literal values of password/secret/token/api_key... assignments with `•••`. Returns (text, count)."""
    count = 0

    def mask_quoted(m: re.Match, head: str) -> str:
        nonlocal count
        grp = "dq" if m.group("dq") is not None else "sq"
        val = m.group(grp)
        if (_TEMPLATE_DQ if grp == "dq" else _TEMPLATE_SQ).search(val):
            return m.group(0)
        count += 1
        return head + val[0] + MASK + val[-1]

    def repl(m: re.Match) -> str:
        nonlocal count
        for grp in ("dq", "sq", "bare"):
            if m.group(grp) is not None:
                break
        val = m.group(grp)
        head = m.group(0)[: len(m.group(0)) - len(val)]
        if grp == "bare":
            if not _bare_is_literal(val):
                return m.group(0)
            count += 1
            return head + MASK
        return mask_quoted(m, head)

    text = _DEFINE.sub(lambda m: mask_quoted(m, m.group("head")), text)
    return _ASSIGN.sub(repl, text), count
