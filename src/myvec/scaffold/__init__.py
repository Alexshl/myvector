"""`myvec scaffold`: deterministic rendering of the orchestrator process (ADR-021).

Pure functions, no typer. Templates live next to this file (`templates/`, shipped in the wheel). The template language is
minimal on purpose: `{{key}}` and whole-line `<!-- if:key -->` ... `<!-- endif -->` blocks (boolean keys, no nesting).
An unknown key or an unclosed block is an error, never an empty string.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from myvec import filters
from myvec import ticketops as ops
from myvec.scaffold.state import STATE

TEMPLATES = Path(__file__).resolve().parent / "templates"

PROCESS_VERSION = 5  # raise on every change of the render (templates, slot defaults); tests/fixtures/scaffold/process_version.json guards it (ADR-028)

SEARCH_RULE = ".claude/rules/search.md"
AGENTS = ("planner", "coder", "tester", "reviewer", "docs-writer", "debugger", "qa")
STATIC = (
    "CLAUDE.md",
    *(f".claude/agents/{a}.md" for a in AGENTS),
    ".claude/rules/workflow.md",
    ".claude/rules/tickets.md",
    SEARCH_RULE,
    "docs/todo/ticket-template.md",
    "docs/decisions.md",
    "docs/lessons.md",
)
ADR_DEFAULT = "docs/decisions.md"   # the ADR file template is stored under this name; the output path is the `adr_path` slot
GROUP_TEMPLATE = "docs/todo/group-index.md"
INDEX = "docs/todo/INDEX.md"
SETTINGS = ".claude/settings.json"
GITIGNORE = ".gitignore"
GITIGNORE_FRAGMENT = "gitignore.fragment"
# files whose generated block (between ticket markers) is rewritten by `ticket index`: compared without it
GENERATED = (INDEX,)

TOP_KEYS = {"tickets_language", "project", "description", "stack", "test_cmd", "lint_cmd", "ui", "machines", "status_style",
            "first_group", "slots"}
REQUIRED = ("project", "description", "stack", "test_cmd")

_ID_RE = re.compile(r"^E[0-9a-z]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KEY_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_IF_RE = re.compile(r"^<!-- if:([A-Za-z_][A-Za-z0-9_]*) -->$")
_ENDIF = "<!-- endif -->"
BOOL_SLOTS = ("has_live_check", "no_unrequested_commits")
VCS_HOSTS = ("github", "gitlab", "none")
PARALLEL_MODES = ("worktree", "none")
# slots that are a single bare command (they go into backticks of the templates): no backticks, no explanations
COMMAND_SLOTS = ("tickets_check_cmd", "tickets_index_cmd")
# an explanation tail, not shell syntax: " (or ...)", " (see ...)", " (e.g. ...)" at the end, or a dash separator;
# subshells, `&&`, `||`, env vars and quotes are legitimate command text
_EXPLAINED = re.compile(r"\s\((?:or|see|e\.g\.|i\.e\.|also|use|alias|e\.g|for)\b[^()]*\)\s*$|\s[—–]\s")


class ScaffoldError(Exception):
    """Invalid answers or template (exit code 2). The message never contains secret values."""


# ---------------------------------------------------------------- slots (project-specific lines of agents/rules)

def _text(v) -> str:
    return "\n".join(v) if isinstance(v, list) else v


def _default_slots(c: dict) -> dict:
    """Generic defaults; every slot can be overridden in `answers.slots` (string or list of lines)."""
    ui = c["has_ui"]
    qa_steps = [
        '1. Take the "Live check" section from the ticket and run it step by step.',
        f"2. UI: {c['ui_url']} - {c['ui_check']}" if ui else "2. Run the scenarios from the ticket exactly as written.",
        "3. Describe a visible defect in steps (and a screenshot description for UI); the coder fixes it.",
        "4. Never quote secrets that show up by accident: report the fact and the place.",
        "5. Put the list of scenarios, pass/fail, defects and the `measure:` line of every test run into the ticket `## Log` as is.",
        "6. Run every test run of the live check through `myvec measure --ticket <ID> --agent qa -- ...` "
        "(rules: `~/.claude/skills/myvector/tickets.md`, \"Test runs\""
        + ("; in a worktree without a fresh `myvec` use `uv run myvec measure`" if c["parallel_mode"] == "worktree" else "") + "). "
        "Do not use the wrapper for `true` or experiments.",
    ]
    return {
        "coder_keywords": "implement, feature, command, refactor, fix",
        "coder_rules": "- Follow the conventions of the neighboring code.",
        "protected_paths": "`~/.claude`, LaunchAgents/systemd, global git config",
        "tickets_check_cmd": "myvec ticket check --strict",
        "tickets_index_cmd": "myvec ticket index",
        "tester_cases": "Cover edge cases and error paths.",
        "tester_isolation": "Tests must not touch real user data or the network: use temporary directories and fakes. Time-dependent tests use a fixed time.",
        "tester_run": f"`myvec measure --ticket <ID> --agent tester --label full -- {c['test_cmd']}`.",
        "planner_risks": "the data model and migrations, secrets, compatibility with what is already deployed, test isolation",
        "planner_read": "README.md, CLAUDE.md",
        "reviewer_checks": [
            "- Correctness: logic, boundaries, races, idempotence, error handling.",
            "- Security: secrets in code, logs and responses, injections, path traversal, file permissions.",
            "- Test quality: the name matches the check, no empty or always-green asserts, tests do not touch real user data.",
            "- Fit to the ticket: nothing extra and nothing missing; real data and secrets did not reach the repository.",
        ],
        "qa_description": ("Live verification of the running project against the ticket's live check"
                           + (", including the UI." if ui else ".")),
        "qa_keywords": "QA, live check, scenarios" + (", browser, screenshot, UI, layout" if ui else ""),
        "qa_steps": qa_steps,
        "docs_targets": ("README.md (commands, architecture), CLAUDE.md (only non-obvious things and pitfalls, briefly), "
                         f"`{c['adr_path']}` (ADRs)"),
        "debugger_repro": "a test, a project command, logs (read-only)",
        "debugger_checks": "check the state of configuration and locks",
        "debugger_data": "",
        "live_check_steps": [
            "1. Take the scenarios from the ticket's live check and run them against the running project.",
            "2. Record what passed, what failed and every defect in the ticket `## Log`.",
        ],
        "live_check_trigger": "When the ticket changes something that runs (the app, a service, a UI):",
        "install_rule": "",
        "docs_language": "English",
        "orchestrator_name": "orchestrator",
        "sec_goal": "In one pass",
        "sec_context": "Context",
        "sec_plan": "Plan - steps",
        "sec_files": "File ownership",
        "sec_live": "Live check",
        "pitfalls_section": '"Pitfalls"',
        "reviewer_security_note": "",
        "reviewer_keyword": "security",
        "tester_kind": "tests",
        "tester_keywords": "tests, regression, flaky, coverage",
        "tester_not_live": "live checks",
        "tester_test_word": "test",
        "tester_neighbors": "tests",
        "tester_summary": "test summary",
        "workflow_docs": f"README/CLAUDE.md/`docs/lessons.md`/`{c['adr_path']}`",
        "parallel_note": "- Do not run parallel qa runs against the same running environment.",
        "canon_path": "~/.claude/skills/myvector/tickets.md",
        "canon_ref": "`~/.claude/skills/myvector/tickets.md`",
        "status_note": "",
        "token_note": "",
        "private_data": "private user data",
        "adr_path": c["adr_path"],
        "parallel_lines": "2",
        "branch_pattern": "t/<ticket-id>-<slug>",
        "pitfalls": "None yet - add what bites.",
        "vcs_host": "github",
        "base_branch": "main",
        "parallel_mode": "worktree",
    }


def _scalar(v, name: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ScaffoldError(f"`{name}` must be a non-empty string")
    if "\n" in v or "\r" in v:
        raise ScaffoldError(f"`{name}` must be a single line")
    return v.strip()


def _check_value(v, name: str) -> None:
    """Strings go into generated files: no hard secrets, no template syntax (the value is never echoed)."""
    if isinstance(v, str):
        if filters.has_hard_secret(v):
            raise ScaffoldError(f"`{name}` looks like a secret (private key or token): remove it from the answers")
        if "{{" in v or "<!-- if:" in v or _ENDIF in v:
            raise ScaffoldError(f"`{name}` contains template syntax")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            if not isinstance(x, str):
                raise ScaffoldError(f"`{name}[{i}]` must be a string")
            _check_value(x, f"{name}[{i}]")
    elif isinstance(v, dict):
        for k, x in v.items():
            _check_value(x, f"{name}.{k}")
    elif not isinstance(v, (bool, type(None))):
        raise ScaffoldError(f"`{name}` has an unsupported type")


@dataclass
class Answers:
    project: str
    description: str
    stack: str
    test_cmd: str
    lint_cmd: str = ""
    ui_url: str = ""
    ui_check: str = ""
    machines: str = ""
    status_style: str = "emoji"
    group_id: str = "E1"
    group_slug: str = "foundation"
    group_title: str = "Foundation"
    slots: dict | None = None
    tickets_language: str = "en"


def load_answers_raw(path) -> tuple[Answers, dict]:
    """(parsed answers, the validated input dict that goes into the project state)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf8"))
    except (OSError, ValueError) as e:
        raise ScaffoldError(f"cannot read answers {path}: {type(e).__name__}") from None
    return parse_answers(raw), raw


def load_answers(path) -> Answers:
    return load_answers_raw(path)[0]


def parse_answers(raw) -> Answers:
    if not isinstance(raw, dict):
        raise ScaffoldError("answers must be a JSON object")
    unknown = sorted(set(raw) - TOP_KEYS)
    if unknown:
        raise ScaffoldError(f"unknown keys: {', '.join(unknown)}")
    for k in REQUIRED:
        if k not in raw:
            raise ScaffoldError(f"missing required key `{k}`")
    _check_value(raw, "answers")
    a = Answers(*(_scalar(raw[k], k) for k in REQUIRED))
    if raw.get("lint_cmd"):
        a.lint_cmd = _scalar(raw["lint_cmd"], "lint_cmd")
    machines = raw.get("machines")
    if machines:
        a.machines = _scalar(machines, "machines")
    ui = raw.get("ui")
    if ui:
        if not isinstance(ui, dict) or set(ui) != {"url", "check"}:
            raise ScaffoldError("`ui` must be an object with exactly `url` and `check`")
        a.ui_url, a.ui_check = _scalar(ui["url"], "ui.url"), _scalar(ui["check"], "ui.check")
    lang = raw.get("tickets_language", "en")
    if lang not in ("en", "uk", "ru"):
        raise ScaffoldError("`tickets_language` must be `en`, `uk` or `ru`")
    a.tickets_language = lang
    style = raw.get("status_style", "emoji")
    if style not in ("emoji", "keys"):
        raise ScaffoldError("`status_style` must be `emoji` or `keys`")
    a.status_style = style
    g = raw.get("first_group")
    if g is not None:
        if not isinstance(g, dict) or set(g) != {"id", "slug", "title"}:
            raise ScaffoldError("`first_group` must have exactly `id`, `slug`, `title`")
        a.group_id, a.group_slug, a.group_title = (_scalar(g[k], f"first_group.{k}") for k in ("id", "slug", "title"))
    if not _ID_RE.match(a.group_id):
        raise ScaffoldError("`first_group.id` must look like E1 (letter E, then digits/lowercase letters)")
    if not _SLUG_RE.match(a.group_slug):
        raise ScaffoldError("`first_group.slug` must be lowercase letters, digits and dashes")
    slots = raw.get("slots") or {}
    if not isinstance(slots, dict):
        raise ScaffoldError("`slots` must be an object")
    known = _default_slots(_context_base(a))
    bad = sorted(set(slots) - set(known) - set(BOOL_SLOTS))
    if bad:
        raise ScaffoldError(f"unknown slots: {', '.join(bad)}")
    for k, v in slots.items():
        if k in BOOL_SLOTS:
            if not isinstance(v, bool):
                raise ScaffoldError(f"slot `{k}` must be a boolean")
        elif not isinstance(v, (str, list)):
            raise ScaffoldError(f"slot `{k}` must be a string or a list of lines")
    _check_slots(a, slots)
    a.slots = dict(slots)
    return a


def _check_slots(a: Answers, slots: dict) -> None:
    for k in COMMAND_SLOTS:
        if k in slots:
            _check_command(slots[k], f"slot `{k}`")
    for k in ("test_cmd", "lint_cmd"):
        if getattr(a, k):
            _check_command(getattr(a, k), f"`{k}`")
    if "vcs_host" in slots and slots["vcs_host"] not in VCS_HOSTS:
        raise ScaffoldError("slot `vcs_host` must be one of: " + ", ".join(VCS_HOSTS))
    if "parallel_mode" in slots and slots["parallel_mode"] not in PARALLEL_MODES:
        raise ScaffoldError("slot `parallel_mode` must be one of: " + ", ".join(PARALLEL_MODES))
    if "base_branch" in slots:
        v = slots["base_branch"]
        if (not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", v) or ".." in v
                or v.endswith(("/", ".lock", "."))):
            raise ScaffoldError("slot `base_branch` must be one valid branch name (letters, digits, `._/-`)")
    if "adr_path" in slots:
        _check_adr_path(slots["adr_path"])
    if "parallel_lines" in slots:
        v = slots["parallel_lines"]
        if not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]?", v):
            raise ScaffoldError("slot `parallel_lines` must be a number from 1 to 99 (a string)")
    if "branch_pattern" in slots:
        v = slots["branch_pattern"]
        if (not isinstance(v, str) or not v.strip() or "\n" in v or "`" in v or v != v.strip() or " " in v
                or "<ticket-id>" not in v):
            raise ScaffoldError("slot `branch_pattern` must be one word without backticks and contain `<ticket-id>`")


def _check_adr_path(v) -> None:
    """A normalized relative `.md` path inside the project that is not another path of the scaffold plan."""
    if not isinstance(v, str) or not v or v != v.strip() or "\n" in v or "`" in v or "\\" in v:
        raise ScaffoldError("slot `adr_path` must be a one-line relative path without backticks")
    p = PurePosixPath(v)
    if (p.is_absolute() or v.endswith("/") or "//" in v or str(p) != v
            or any(x in ("..", ".", "") for x in v.split("/")) or p.suffix.lower() != ".md"):
        raise ScaffoldError("slot `adr_path` must be a normalized relative path of a .md file inside the project")
    if p.parts[0] in (".git", ".claude") or v.startswith(TICKETS_PREFIX):
        raise ScaffoldError("slot `adr_path` must not point into .git, .claude or docs/todo")
    own = {r for r in STATIC if r != ADR_DEFAULT} | {INDEX, SETTINGS, GITIGNORE, GROUP_TEMPLATE}
    if v in own or v.lower() in {x.lower() for x in own}:
        raise ScaffoldError("slot `adr_path` collides with another file of the scaffold")


def _check_command(v, name: str) -> None:
    """A command goes into backticks of the templates as is: one line, no backticks, no trailing explanation."""
    if not isinstance(v, str) or not v.strip() or "\n" in v or "\r" in v:
        raise ScaffoldError(f"{name} must be one command on a single line")
    if "`" in v:
        raise ScaffoldError(f"{name} must be a bare command without backticks")
    if _EXPLAINED.search(v):
        raise ScaffoldError(f"{name} must be a bare command without an explanation in parentheses")


# ---------------------------------------------------------------- template language

def _slot_str(a: Answers, key: str, default: str) -> str:
    v = (a.slots or {}).get(key)
    return v.strip() if isinstance(v, str) and v.strip() else default


def _context_base(a: Answers) -> dict:
    has_ui = bool(a.ui_url)
    branch = _slot_str(a, "branch_pattern", "t/<ticket-id>-<slug>")
    done = "✅ 2026-11-09" if a.status_style == "emoji" else "done 2026-11-09"
    return {
        "project": a.project, "description": a.description, "stack": a.stack, "test_cmd": a.test_cmd,
        "lint_cmd": a.lint_cmd, "ui_url": a.ui_url, "ui_check": a.ui_check, "machines": a.machines,
        "has_ui": has_ui, "no_ui": not has_ui, "has_lint": bool(a.lint_cmd), "has_machines": bool(a.machines),
        "status_todo": "⬜" if a.status_style == "emoji" else "todo",
        "status_example": done,
        "status_style_text": ("emoji (`⬜`, `✅ 2026-11-09`)" if a.status_style == "emoji"
                              else "keys (`todo`, `done 2026-11-09`)"),
        "group_id": a.group_id, "group_slug": a.group_slug,
        "group_title": a.group_title, "group_title_yaml": ops._yaml_scalar(a.group_title),
        "tickets_language": a.tickets_language,
        "is_emoji": a.status_style == "emoji", "is_keys": a.status_style == "keys",
        "adr_path": _slot_str(a, "adr_path", ADR_DEFAULT),
        "parallel_mode": _slot_str(a, "parallel_mode", "worktree"),
        "branch_example": branch.replace("<ticket-id>", "E1-02").replace("<slug>", "short-slug"),
    }


def context(a: Answers) -> dict:
    ctx = _context_base(a)
    slots = _default_slots(ctx)
    slots.update({k: v for k, v in (a.slots or {}).items() if k not in BOOL_SLOTS})
    for k, v in slots.items():
        ctx[k] = _text(v).rstrip("\n")
    ctx["no_unrequested_commits"] = bool((a.slots or {}).get("no_unrequested_commits", False))
    ctx.update(_git_context(ctx))
    ctx["has_install_rule"] = bool(ctx["install_rule"])
    ctx["general_rules"] = render_text(_read_template("partials/general-rules.md"), ctx, "general-rules").rstrip("\n")
    ctx["has_live_check"] = (a.slots or {}).get("has_live_check", ctx["has_ui"] or "live_check_steps" in (a.slots or {}))
    return ctx


def _git_context(ctx: dict) -> dict:
    """Git-process wording derived from the `vcs_host`, `base_branch` and `parallel_mode` slots."""
    host, base = ctx["vcs_host"], ctx["base_branch"]
    if host == "github":
        words = dict(pr_word="PR", pr_plural="PRs", pr_a="a PR", pr_open="PR open",
                     pr_via="a PR via `gh pr create`", pr_body="the PR body has",
                     git_tail="the orchestrator also pushes and opens the PR",
                     claude_review="a PR on GitHub (the PR body has the path to the ticket)")
    elif host == "gitlab":
        words = dict(pr_word="MR", pr_plural="MRs", pr_a="an MR", pr_open="MR open",
                     pr_via="an MR via `glab mr create`", pr_body="the MR description has",
                     git_tail="the orchestrator also pushes and opens the MR",
                     claude_review="an MR on GitLab (the MR description has the path to the ticket)")
    else:
        words = dict(pr_word="merge", pr_plural="merges", pr_a="a review request", pr_open="ready for merge",
                     pr_via=f"no remote: after review a local merge into `{base}`",
                     pr_body="the merge commit message has",
                     git_tail="there is no remote, nothing is pushed",
                     claude_review=f"a local merge into `{base}` after review (no remote)")
    # "git push at the end" would contradict the no-unrequested-commits rule: the line is left out then
    words["has_remote_machines"] = bool(ctx["has_machines"]) and host != "none" and not ctx.get("no_unrequested_commits")
    words["is_worktree"] = ctx["parallel_mode"] == "worktree"
    words["no_worktree"] = ctx["parallel_mode"] == "none"
    return words


def render_text(tpl: str, ctx: dict, name: str = "template") -> str:
    out: list[str] = []
    keep = True
    open_ = False
    for n, line in enumerate(tpl.split("\n"), 1):
        m = _IF_RE.match(line)
        if m:
            if open_:
                raise ScaffoldError(f"{name}:{n}: nested if blocks are not supported")
            key = m.group(1)
            if key not in ctx:
                raise ScaffoldError(f"{name}:{n}: unknown key `{key}`")
            if not isinstance(ctx[key], bool):
                raise ScaffoldError(f"{name}:{n}: condition `{key}` is not boolean")
            open_, keep = True, ctx[key]
            continue
        if line == _ENDIF:
            if not open_:
                raise ScaffoldError(f"{name}:{n}: endif without if")
            open_, keep = False, True
            continue
        if not keep:
            continue

        def sub(mm: re.Match) -> str:
            key = mm.group(1)
            if key not in ctx:
                raise ScaffoldError(f"{name}:{n}: unknown key `{key}`")
            if isinstance(ctx[key], bool):
                raise ScaffoldError(f"{name}:{n}: key `{key}` is boolean, use it in an if block")
            return ctx[key]

        whole = _KEY_RE.fullmatch(line)
        new = _KEY_RE.sub(sub, line)
        if whole and new == "":
            continue  # a line that is only an empty slot disappears
        out.append(new)
    if open_:
        raise ScaffoldError(f"{name}: unclosed if block")
    return "\n".join(out)


def allow_prefix(cmd: str) -> str:
    """`uv run pytest -q` -> `uv run pytest` (the permission matches by command prefix)."""
    words: list[str] = []
    for w in cmd.split():
        if w.startswith("-"):
            break
        words.append(w)
    return " ".join(words) or cmd


def settings_allow(a: Answers) -> list[str]:
    return [f"Bash({allow_prefix(a.test_cmd)}:*)", "Bash(myvec ticket:*)"]


def _read_template(rel: str) -> str:
    return (TEMPLATES / rel).read_text(encoding="utf8")


def render(a: Answers, with_group: bool = True) -> dict[str, str]:
    """relpath -> text for every file the scaffold owns whole (.gitignore and settings.json are merged separately).
    `with_group=False` (an existing project: it has its own tickets, or --no-tickets) leaves the first group out."""
    ctx = context(a)
    files = {(ctx["adr_path"] if rel == ADR_DEFAULT else rel): render_text(_read_template(rel), ctx, rel) for rel in STATIC}
    files[INDEX] = render_text(_read_template(INDEX), ctx, INDEX)
    if with_group:
        files[f"docs/todo/{a.group_id}-{a.group_slug}/index.md"] = render_text(_read_template(GROUP_TEMPLATE), ctx, GROUP_TEMPLATE)
    for rel, text in files.items():
        if "{{" in text or "<!-- if:" in text:
            raise ScaffoldError(f"{rel}: unresolved template syntax left in the output")
    return files


def render_digest(a: Answers) -> str:
    """sha256 of everything the scaffold would write for `a` (owned files, .gitignore, settings allow list)."""
    h = hashlib.sha256()
    parts = dict(render(a))
    parts[GITIGNORE] = _merge_gitignore(None, a) or ""
    parts[SETTINGS] = "\n".join(settings_allow(a))
    for rel in sorted(parts):
        h.update(rel.encode("utf8") + b"\0" + parts[rel].encode("utf8") + b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------- plan / apply

def _norm(rel: str, text: str) -> str:
    return ops.replace_generated_text(text, "") if rel in GENERATED or rel.endswith("/index.md") else text


def _gitignore_lines(a=None) -> list[str]:
    return [l for l in _read_template(GITIGNORE_FRAGMENT).splitlines() if l.strip() and not l.startswith("#")]


def _merge_gitignore(old: str | None, a: Answers | None = None) -> str | None:
    """Text with the missing lines appended, or None if nothing is missing."""
    have = {l.strip() for l in (old or "").splitlines()}
    no_wt = a is not None and (a.slots or {}).get("parallel_mode") == "none"
    miss = [l for l in _gitignore_lines() if l not in have and not (no_wt and l == ".worktrees/")]
    if not miss:
        return None
    base = (old or "")
    if base and not base.endswith("\n"):
        base += "\n"
    return base + "\n".join(miss) + "\n"


def _merge_settings(old: str | None, allow: list[str]) -> str | None:
    """Merged settings text (None: nothing to add). ValueError: existing file is not a JSON object."""
    if old is None:
        data: dict = {}
    else:
        try:
            data = json.loads(old)
        except ValueError:
            raise ValueError("not valid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError("unexpected permissions shape")
    cur = perms.setdefault("allow", [])
    if not isinstance(cur, list):
        raise ValueError("unexpected permissions shape")
    miss = [x for x in allow if x not in cur]
    if not miss and old is not None:
        return None
    cur.extend(miss)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _safe(root: Path, rel: str) -> Path:
    p = root / rel
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise ScaffoldError(f"{rel}: a symbolic link on the path, refusing to write")
    return p


class ScaffoldChanged(ScaffoldError):
    """The directory changed between plan and apply (exit code 3): nothing was written."""


PROCESS_HEADING = "## Development process"
TICKETS_PREFIX = "docs/todo/"
_ABSENT, _NONFILE = "-", "nonfile"


class _Appeared(Exception):
    """A file showed up where `create` was planned (internal)."""


class Plan(list):
    """[(relpath, action)] plus `fp`: relpath -> sha256 of the existing content (or `-` absent, `nonfile`)."""

    fp: dict[str, str]

    def __init__(self, rows=(), fp=None):
        super().__init__(rows)
        self.fp = dict(fp or {})


def _fingerprint(p: Path) -> str:
    if p.is_symlink() or (p.exists() and not p.is_file()):
        return _NONFILE
    if not p.exists():
        return _ABSENT
    return hashlib.sha256(p.read_bytes()).hexdigest()


def process_section(rendered_claude_md: str) -> str:
    """The `## Development process` section of the rendered CLAUDE.md (to the end of the template)."""
    i = rendered_claude_md.find(PROCESS_HEADING)
    if i < 0:
        raise ScaffoldError("the CLAUDE.md template has no process section")
    return rendered_claude_md[i:]


APPEND_HEADINGS = ("## Stack and commands", "## Pitfalls", PROCESS_HEADING)


def append_sections(rendered_claude_md: str, old: str) -> str:
    """Sections of the rendered CLAUDE.md to append to an existing one: Stack, Pitfalls and the process, each only
    if the file does not have a heading of that name yet (a repeat does not duplicate)."""
    have = {l.strip().lower() for l in old.splitlines()}
    parts: dict[str, list[str]] = {}
    cur = None
    for line in rendered_claude_md.split("\n"):
        if line.startswith("## "):
            cur = line.strip()
            parts[cur] = []
        if cur is not None:
            parts[cur].append(line)
    out = []
    for h in APPEND_HEADINGS:
        if h not in parts:
            raise ScaffoldError(f"the CLAUDE.md template has no `{h}` section")
        if h.lower() not in have:
            out.append("\n".join(parts[h]).rstrip("\n") + "\n")
    return "\n".join(out)


TICKET_TEMPLATE = "docs/todo/ticket-template.md"


def plan(root: Path, rendered: dict[str, str], a: Answers | None = None, tickets: str | None = None,
         with_template: bool = False) -> Plan:
    """[(relpath, action)]: create | same | conflict | merge | skip-foreign | skip. `a` enables the merged files
    (.gitignore, settings.json). `tickets`: `foreign` (the ticket tree is someone else's) or `no` (--no-tickets)
    leave every path under docs/todo/ alone (`with_template` still plans the ticket template there)."""
    root = Path(root)
    out = Plan()
    for rel, text in rendered.items():
        p = _safe(root, rel)
        out.fp[rel] = _fingerprint(p)
        if tickets and rel.startswith(TICKETS_PREFIX) and not (with_template and rel == TICKET_TEMPLATE):
            out.append((rel, "skip-foreign" if tickets == "foreign" else "skip"))
        elif not p.exists():
            twin = p.parent.is_dir() and any(e.name.lower() == p.name.lower() for e in p.parent.iterdir())
            out.append((rel, "conflict" if twin else "create"))  # a file that differs only in case (macOS: the same file)
        elif not p.is_file():
            out.append((rel, "conflict"))
        else:
            old = ops.read_text(p)
            out.append((rel, "same" if _norm(rel, old) == _norm(rel, text) else "conflict"))
    if a is not None:
        p = _safe(root, GITIGNORE)
        out.fp[GITIGNORE] = _fingerprint(p)
        old = ops.read_text(p) if p.is_file() else None
        out.append((GITIGNORE, "create" if old is None else ("same" if _merge_gitignore(old, a) is None else "merge")))
        p = _safe(root, SETTINGS)
        out.fp[SETTINGS] = _fingerprint(p)
        old = ops.read_text(p) if p.is_file() else None
        try:
            merged = _merge_settings(old, settings_allow(a))
            out.append((SETTINGS, "create" if old is None else ("same" if merged is None else "merge")))
        except ValueError:
            out.append((SETTINGS, "conflict"))
    return out


def check_choices(root: Path, pl: list[tuple[str, str]], rendered: dict[str, str],
                  flags: dict[str, set[str]]) -> None:
    """`flags`: {"--overwrite": paths, "--keep": paths, "--append": paths}. Raises ScaffoldError (exit 2)."""
    known = {r for r, _ in pl}
    seen: dict[str, str] = {}
    for flag, paths in flags.items():
        if STATE in paths:
            raise ScaffoldError(f"{flag}: {STATE} is written by the scaffold itself, it takes no choices")
        bad = sorted(paths - known)
        if bad:
            raise ScaffoldError(f"{flag}: not a scaffold path: {', '.join(bad)}")
        for r in paths:
            if r in seen:
                raise ScaffoldError(f"{r}: given to both {seen[r]} and {flag}")
            seen[r] = flag
    acts = dict(pl)
    for r in flags.get("--append", set()):
        if r != "CLAUDE.md":
            raise ScaffoldError("--append works only for CLAUDE.md")
        if acts[r] == "conflict":
            p = _safe(Path(root), r)
            if p.is_file() and PROCESS_HEADING.lower() in {l.strip().lower() for l in ops.read_text(p).splitlines()}:
                raise ScaffoldError(f"{r} already has the `{PROCESS_HEADING}` section: merge by hand or use --keep")


def conflicts(pl: list[tuple[str, str]], *resolved: set[str]) -> list[str]:
    done = set().union(*resolved) if resolved else set()
    return [rel for rel, act in pl if act == "conflict" and rel not in done]


def labelled(pl, overwrite: set[str], keep: set[str] = frozenset(), append: set[str] = frozenset()):
    """The plan with the owner's choices applied: conflict -> overwrite | keep | append."""
    out = []
    for rel, act in pl:
        if act == "conflict":
            act = "keep" if rel in keep else "append" if rel in append else "overwrite" if rel in overwrite else act
        elif act in ("merge", "create") and rel in keep:
            act = "keep"
        out.append((rel, act))
    return out


def with_state(root: Path, pl: Plan, rendered: dict[str, str], raw: dict, rows, a: Answers | None = None) -> None:
    """Adds the state row (create | same | merge) to the plan and its text to `rendered` (ADR-028).
    `rows` is the plan with the owner's choices applied. Raises ScaffoldError on a hard secret."""
    from myvec.scaffold import state as st

    skip = {f"docs/todo/{a.group_id}-{a.group_slug}/index.md"} if a is not None else set()  # a seed, not process
    owned = {r: t for r, t in rendered.items() if r not in skip}
    text = st.build(raw, owned, rows, PROCESS_VERSION, _norm)
    if filters.has_hard_secret(text):
        raise ScaffoldError("the scaffold state would contain a secret, refusing to write it")
    p = _safe(Path(root), STATE)
    pl.fp[STATE] = _fingerprint(p)
    if p.exists() and not p.is_file():
        raise ScaffoldError(f"{STATE}: exists and is not a regular file")
    rendered[STATE] = text
    if not p.exists():
        pl.append((STATE, "create"))
    else:
        pl.append((STATE, "same" if ops.read_text(p) == text else "merge"))


def _new_text(root: Path, rel: str, rendered: dict[str, str], a: Answers | None, append: bool) -> str:
    p = root / rel
    if rel == STATE:
        return rendered[STATE]
    old = ops.read_text(p) if p.is_file() else None
    if rel == GITIGNORE:
        return _merge_gitignore(old, a) or ""
    if rel == SETTINGS:
        try:
            merged = _merge_settings(old, settings_allow(a))
        except ValueError:
            merged = _merge_settings(None, settings_allow(a))  # explicit --overwrite of a broken file
        return merged or ""
    if append:
        base = old if old.endswith("\n") else old + "\n"
        add = append_sections(rendered[rel], old)
        return base + "\n" + add if add else old
    return rendered[rel]


def diffs(root: Path, pl, rendered: dict[str, str], a: Answers | None,
          append: set[str] = frozenset()) -> dict[str, dict]:
    """rel -> {"diff": unified diff, secrets masked} or {"diff_withheld": True} for conflict/merge/append rows."""
    root = Path(root)
    out: dict[str, dict] = {}
    for rel, act in pl:
        if act not in ("conflict", "merge", "overwrite", "append"):
            continue
        if rel == STATE:
            out[rel] = {"diff_withheld": True, "note": "scaffold state, rewritten by the scaffold itself"}
            continue
        p = _safe(root, rel)
        if p.exists() and not p.is_file():
            out[rel] = {"diff_withheld": True}
            continue
        old = ops.read_text(p) if p.is_file() else ""
        if filters.has_hard_secret(old):
            out[rel] = {"diff_withheld": True}
            continue
        new = _new_text(root, rel, rendered, a, rel in append)
        old_m, new_m = filters.mask_secrets(old)[0], filters.mask_secrets(new)[0]
        out[rel] = {"diff": "".join(difflib.unified_diff(
            old_m.splitlines(True), new_m.splitlines(True), f"a/{rel}", f"b/{rel}"))}
    return out


def _bak(p: Path, stamp: str) -> Path:
    return p.with_name(p.name + f".bak-{stamp}")


def apply(root: Path, pl: list[tuple[str, str]], rendered: dict[str, str], overwrite: set[str],
          a: Answers | None = None, keep: set[str] = frozenset(), append: set[str] = frozenset()) -> list[tuple[str, str]]:
    """Transactional write of create/merge/overwrite/append (ADR-023).

    1. unresolved conflicts -> nothing is written (the plan is returned);
    2. every path is re-checked (symlinks, fingerprint from `plan`): a difference -> ScaffoldChanged, nothing written;
    3. prepare: all new texts go to tmp files next to the targets, backups are made; OSError -> undo, nothing changed;
    4. replace: `create` via os.link (never overwrites a file that appeared), the rest via os.replace; OSError ->
       best-effort rollback of what was already replaced."""
    root = Path(root)
    if conflicts(pl, overwrite, keep, append):
        return pl
    fp = getattr(pl, "fp", None) or {}
    for rel, _ in pl:
        try:
            p = _safe(root, rel)
        except ScaffoldError:
            raise ScaffoldChanged(f"{rel}: a symbolic link appeared after the plan was made, repeat the dry-run") from None
        if rel in fp and _fingerprint(p) != fp[rel]:
            raise ScaffoldChanged(f"{rel}: changed after the plan was made, repeat the dry-run")
    result = labelled(pl, overwrite, keep, append)
    todo = [(rel, act) for rel, act in result if act in ("create", "merge", "overwrite", "append")]
    stamp = time.strftime("%Y%m%d%H%M%S")
    tmps: list[Path] = []
    baks: list[Path] = []
    dirs: list[Path] = []
    prepared: list[tuple[Path, Path, str, bytes | None]] = []  # (target, tmp, act, original bytes)
    bak_of: dict[Path, Path] = {}

    def undo_prep():
        for t in tmps + baks:
            t.unlink(missing_ok=True)
        for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass

    try:
        for rel, act in todo:
            p = root / rel
            if p.exists() and not p.is_file():
                raise ScaffoldError(f"{rel}: exists and is not a regular file")
            missing = [d for d in reversed([*p.parent.parents, p.parent]) if d != root and root in d.parents and not d.exists()]
            p.parent.mkdir(parents=True, exist_ok=True)
            dirs.extend(missing)
            text = _new_text(root, rel, rendered, a, act == "append")
            orig = p.read_bytes() if p.is_file() else None
            tmp = p.with_name(f".{p.name}.myvec-{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
            tmps.append(tmp)
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf8"))
            if p.is_file():
                shutil.copymode(p, tmp)
                if rel != GITIGNORE:
                    b = _bak(p, stamp)
                    shutil.copy2(p, b)
                    baks.append(b)
                    bak_of[p] = b
            prepared.append((p, tmp, act, orig))
    except (OSError, ScaffoldError):
        undo_prep()
        raise

    done: list[tuple[Path, str, bytes | None]] = []
    try:
        for p, tmp, act, orig in prepared:
            if act == "create":
                try:
                    os.link(tmp, p)
                except FileExistsError:
                    raise _Appeared(str(p.relative_to(root))) from None
                tmp.unlink(missing_ok=True)
            else:
                os.replace(tmp, p)
            done.append((p, act, orig))
    except (OSError, _Appeared) as e:
        failed: list[str] = []
        for p, act, orig in reversed(done):
            try:
                if orig is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(orig)
                    bak_of.pop(p, None)  # restored: its backup is redundant
            except OSError:
                bak = bak_of.get(p)
                failed.append(f"{p.relative_to(root)} (backup kept at {bak})" if bak else f"{p.relative_to(root)} (no backup)")
                if bak:
                    baks.remove(bak)  # the only good copy: never delete
        undo_prep()
        if failed:
            raise OSError(f"{e}; rollback failed for: {', '.join(failed)}") from None
        if isinstance(e, _Appeared):
            raise ScaffoldChanged(f"{e}: appeared after the plan was made, rolled back, repeat the dry-run") from None
        raise OSError(f"{e}; rolled back {len(done)} already written file(s)") from None
    return result


def regenerate_index(root: Path) -> list[str]:
    """Fills the generated blocks of INDEX.md and the group tables; returns ticket-check errors (empty = ok)."""
    from myvec import tickets as T

    root = Path(root)
    ts = T.load_dir(root, "docs/todo")
    issues = [f"{i.path}: {i.message} [{i.code}]" for i in T.check(ts) if i.severity == "error"]
    if issues:
        return issues
    lang = ops.lang_for(root / "docs/todo")
    ops.replace_generated(root / INDEX, ops.render_index(ts, "docs/todo", lang))
    for t in ts.tickets.values():
        if t.kind == "group":
            p = root / t.path
            if p.is_file() and ops.has_markers(ops.read_text(p)):
                ops.replace_generated(p, ops.render_group_table(ts, t, lang))
    return []
