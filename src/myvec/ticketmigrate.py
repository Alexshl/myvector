"""Міграція тікетів на канон v1 (ADR-024): виявлення формату, мапи полів і статусів, бриф агенту проєкту,
перевірка «що лишилось» і механічні безвтратні правки шапки (`fix_plan`).

Чисті функції без typer і БД. Читає лише ключі шапки, значення статусів і заголовки розділів (через
`mask_secrets`, hard secret відкидається), тіло тікетів у звіт не потрапляє. Пише лише `apply_fixes`."""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from myvec import filters, projects, ticketops as ops
from myvec import tickets as T

MAX_DEPTH = 4
TOP_HEADINGS = 20
SKIP_DIRS = ("node_modules", "vendor", ".git", ".worktrees", "inbox")
LEFTOVER_CODES = ("not_ticket", "legacy_epic", "legacy_group", "mixed_status_form", "leftover_todo")
POINTER_MARK = "<!-- myvec:split-pointer -->"
GOAL_HEADING = {"uk": "За один захід", "ru": "За один заход", "en": "In one pass"}

_HEAD = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")
_TASK_HEAD = re.compile(r"^([A-Za-zА-Яа-яІіЇїЄє]{0,3})(\d+)[.:)]")
_BOX = re.compile(r"^\s*[-*]\s+\[([ xX])\]")
_DONE_PAREN = re.compile(r"^done\s*\(.*\)$", re.I)

_RU_HEADS = ("за один заход", "решение", "владение файлами", "проверка", "live-проверка", "критерии приёмки",
             "критерии приемки", "сделать", "тесты", "сделано", "найденные", "состояние", "порядок", "волна")
_UK_HEADS = ("за один захід", "рішення", "володіння файлами", "перевірка наживо", "критерії", "зроблено")
_EN_HEADS = ("in one pass", "plan", "file ownership", "live check", "context", "done", "tests", "summary")

FIELD_ALIASES = {"epic": "parent", "blocked_by": "deps", "depends_on": "deps", "dependencies": "deps",
                 "name": "title", "state": "status", "estimate": "size"}
STATUS_SYNONYMS = {
    "done": "done", "closed": "done", "сделано": "done", "готово": "done", "зроблено": "done",
    "wont-do": "cancelled", "wont_do": "cancelled", "rejected": "cancelled", "won't fix": "cancelled",
    "tested": "review",
    "planning": "todo", "backlog": "todo", "open": "todo", "new": "todo",
    "in progress": "in_progress", "in-progress": "in_progress", "wip": "in_progress",
}


@dataclass
class Detection:
    dir: str                       # шлях відносно кореня проєкту (тека або файл), posix
    kind: str                      # canon | near_canon | frontmatter | single_file | notes
    is_file: bool = False
    n_md: int = 0
    n_frontmatter: int = 0
    n_with_id: int = 0
    n_canon: int = 0
    n_items: int = 0               # single_file: кількість заголовків «### T<n>.»
    n_boxes: int = 0
    n_legacy_epic: int = 0
    n_legacy_groups: int = 0
    status_form: str = "none"      # emoji | keys | mixed | none
    lang: str = "other"            # uk | en | ru | other
    git_root: str | None = None    # відносно кореня проєкту ("." - сам корінь), None - не git
    keys: dict = field(default_factory=dict)
    statuses: dict = field(default_factory=dict)
    headings: dict = field(default_factory=dict)
    estimate_is_size: bool = False  # усі значення `estimate` - S/M/L
    is_marker_dir: bool = False

    def to_json(self) -> dict:
        return {"dir": self.dir, "kind": self.kind, "n_md": self.n_md, "n_frontmatter": self.n_frontmatter,
                "n_with_id": self.n_with_id, "n_canon": self.n_canon, "status_form": self.status_form,
                "lang": self.lang, "git_root": self.git_root, "keys": self.keys, "statuses": self.statuses,
                "headings": self.headings, "n_items": self.n_items, "n_boxes": self.n_boxes,
                "legacy_epic": self.n_legacy_epic, "legacy_groups": self.n_legacy_groups}


# ---------- Кандидати ----------

def _marker(root: Path):
    try:
        return projects.read_marker(root)
    except projects.InvalidMarker:
        return None


def is_pointer(path: Path) -> bool:
    """A short file left by `--split --write` (carries POINTER_MARK): not a ticket candidate any more."""
    try:
        with open(path, "rb") as f:
            return POINTER_MARK.encode() in f.read(4096)
    except OSError:
        return False


def find_candidates(root, exclude: list[str] | None = None) -> list[str]:
    """Шляхи (posix, відносно `root`) тек і файлів-кандидатів у тікети, до глибини MAX_DEPTH."""
    root = Path(root)
    mk = _marker(root)
    excl = list(exclude) if exclude is not None else (mk.exclude if mk else [])
    exc = None
    if excl:
        import pathspec

        exc = pathspec.PathSpec.from_lines("gitignore", excl)
    found: dict[str, int] = {}

    def has_md(d: Path) -> int:
        n = 0
        for p in d.rglob("*.md"):
            if p.name in T.SKIP_FILES or "inbox" in p.relative_to(d).parts[:-1] or p.is_symlink():
                continue
            n += 1
        return n

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        rel_d = d.relative_to(root).as_posix()
        for e in entries:
            p = Path(e.path)
            rel = p.relative_to(root).as_posix()
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS or filters.is_denied_dir(e.name):
                    continue
                if projects.is_excluded_dir(exc, rel) or (p / projects.MARKER).is_file():
                    continue
                if e.name == "todo" and d.name == "docs":
                    if not filters.is_denied_path(rel + "/x.md"):
                        n = has_md(p)
                        if n:
                            found[rel] = n
                    continue
                if depth + 1 < MAX_DEPTH:
                    walk(p, depth + 1)
            elif e.name.lower().endswith(".md"):
                low = e.name.lower()
                ok = low in ("todo.md",) or (d.name == "docs" and low.startswith("todo"))
                if ok and not filters.is_denied_path(rel) and not (exc is not None and exc.match_file(rel)):
                    if not is_pointer(p):
                        found[rel] = 1

    walk(root, 0)
    first = mk.tickets_dir if mk else projects.DEFAULT_TICKETS_DIR
    return sorted(found, key=lambda r: (r != first, -found[r], r))


# ---------- Detect ----------

def _git_root(path: Path, root: Path) -> str | None:
    cwd = path if path.is_dir() else path.parent
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    top = Path(r.stdout.strip()).resolve()
    try:
        return top.relative_to(root.resolve()).as_posix() or "."
    except ValueError:
        return top.as_posix()


def _safe(value: str) -> str | None:
    """Замаскований короткий рядок або None, якщо в ньому hard secret."""
    value = value.strip()
    if not value or filters.has_hard_secret(value):
        return None
    return filters.mask_secrets(value)[0][:80]


def _status_form(raws) -> str:
    emoji = keys = 0
    for r in raws:
        s = str(r).replace("️", "").strip()
        if not s:
            continue
        if s[0] in T.STATUS_EMOJI:
            emoji += 1
        else:
            keys += 1
    return "mixed" if emoji and keys else "emoji" if emoji else "keys" if keys else "none"


def _lang(headings: Counter) -> str:
    score = {"uk": 0, "ru": 0, "en": 0}
    for h, n in headings.items():
        low = h.lstrip('#').strip().lower()
        if any(low.startswith(m) for m in _RU_HEADS) or re.search(r"[ыэъё]", low):
            score["ru"] += n
        elif any(low.startswith(m) for m in _UK_HEADS) or re.search(r"[іїєґ]", low):
            score["uk"] += n
        elif any(low.startswith(m) for m in _EN_HEADS):
            score["en"] += n
    best = max(score, key=score.get)
    return best if score[best] else "other"


def _md_files(base: Path):
    for f in sorted(base.rglob("*.md")):
        rel = f.relative_to(base)
        if f.name in T.SKIP_FILES or any(p in SKIP_DIRS for p in rel.parts[:-1]) or f.is_symlink():
            continue
        yield f


def _scan_body(text: str, single: bool, headings: Counter) -> int:
    """Заголовки розділів і кількість чекбоксів; тіло в звіт не йде."""
    boxes, fenced = 0, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if _BOX.match(line):
            boxes += 1
        m = _HEAD.match(line)
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2)
        if single and level == 3:
            tm = _TASK_HEAD.match(title)
            if tm:
                headings[f"### {tm.group(1)}<n>."] += 1
            continue
        if level == 2:
            s = _safe(title)
            if s:
                headings[f"## {s}"] += 1
    return boxes


def detect(root, rel: str) -> Detection:
    root = Path(root)
    path = root / rel
    det = Detection(dir=rel, kind="notes", is_file=path.is_file())
    keys: Counter = Counter()
    statuses: Counter = Counter()
    headings: Counter = Counter()
    est_vals: list[str] = []
    raw_status: list[str] = []
    mk = _marker(root)
    det.is_marker_dir = bool(mk and mk.tickets_dir == rel)
    det.git_root = _git_root(path, root)
    if det.is_file:
        det.n_md = 1
        text = path.read_text(encoding="utf8", errors="replace")
        det.n_boxes = _scan_body(text, True, headings)
        det.n_items = sum(v for k, v in headings.items() if k.startswith("### "))
        det.kind = "single_file"
    else:
        for f in _md_files(path):
            det.n_md += 1
            text = f.read_text(encoding="utf8", errors="replace")
            try:
                parsed = T.parse_frontmatter(text)
            except T.FrontmatterError:
                parsed = None
            if parsed is not None:
                det.n_frontmatter += 1
                fm = parsed[0]
                for k in fm:
                    if _safe(k):
                        keys[k] += 1
                if fm.get("id"):
                    det.n_with_id += 1
                st = fm.get("status")
                if isinstance(st, str) and st.strip():
                    raw_status.append(st)
                    s = _safe(st)
                    if s:
                        statuses[s] += 1
                est = fm.get("estimate")
                if est is not None:
                    est_vals.append(str(est))
            det.n_boxes += _scan_body(text, False, headings)
        ts = T.load_dir(root, rel)
        det.n_canon = sum(1 for t in ts.tickets.values() if not t.legacy)
        det.n_legacy_epic = sum(1 for t in ts.tickets.values() if t.legacy and t.kind != "group")
        det.n_legacy_groups = sum(1 for t in ts.tickets.values() if t.legacy and t.kind == "group")
        if det.n_with_id == 0 and not ts.tickets:
            det.kind = "frontmatter" if det.n_frontmatter else "notes"
        elif not det.n_legacy_epic and not det.n_legacy_groups and det.n_canon >= det.n_with_id > 0:
            det.kind = "canon"
        else:
            det.kind = "near_canon"
    det.status_form = _status_form(raw_status)
    det.lang = _lang(headings)
    det.estimate_is_size = bool(est_vals) and all(v.strip().upper() in T.SIZES for v in est_vals)
    det.keys = dict(keys.most_common())
    det.statuses = dict(statuses.most_common())
    det.headings = dict(headings.most_common(TOP_HEADINGS))
    return det


def detect_all(root, exclude: list[str] | None = None) -> list[Detection]:
    return [detect(root, rel) for rel in find_candidates(root, exclude)]


def recommended(dets: list[Detection]) -> str | None:
    for d in dets:
        if d.kind != "notes" and d.n_md:
            return d.dir
    return None


def detect_json(dets: list[Detection]) -> str:
    return json.dumps({"candidates": [d.to_json() for d in dets], "recommended_dir": recommended(dets)},
                      ensure_ascii=False, indent=2)


# ---------- Мапи ----------

def field_map(det: Detection) -> list[dict]:
    """[{key, count, target, note}]; target - канонічне поле, `=` (вже канон) або `local` (лишити як є)."""
    out = []
    for key, n in det.keys.items():
        if key in T.KNOWN_KEYS:
            out.append({"key": key, "count": n, "target": "=", "note": ""})
        elif key == "estimate":
            if det.estimate_is_size:
                out.append({"key": key, "count": n, "target": "size", "note": "all values are S/M/L"})
            else:
                out.append({"key": key, "count": n, "target": "local", "note": "values are not S/M/L"})
        elif key in FIELD_ALIASES:
            out.append({"key": key, "count": n, "target": FIELD_ALIASES[key], "note": ""})
        else:
            out.append({"key": key, "count": n, "target": "local", "note": "keep as a project-local field"})
    return out


def status_map(det: Detection) -> list[dict]:
    """[{raw, count, target, note}]; target - ключ канону або `?` (рішає власник проєкту)."""
    out = []
    for raw, n in det.statuses.items():
        ns = T.normalize_status(raw)
        if ns is not None:
            out.append({"raw": raw, "count": n, "target": ns[0], "note": ""})
            continue
        low = raw.strip().lower()
        if _DONE_PAREN.match(low):
            out.append({"raw": raw, "count": n, "target": "done", "note": "move the comment to the ticket Log"})
        elif low in STATUS_SYNONYMS:
            out.append({"raw": raw, "count": n, "target": STATUS_SYNONYMS[low], "note": ""})
        else:
            out.append({"raw": raw, "count": n, "target": "?", "note": "the project owner decides"})
    return out


# ---------- Бриф ----------

def render_brief(det: Detection, project: str, dir_: str | None = None) -> str:
    fmap, smap = field_map(det), status_map(det)
    d = dir_ or det.dir
    L: list[str] = []
    L.append(f"# Migration brief: {project} tickets -> MyVector canon v1\n")
    L.append("Goal: bring the project's tickets to canon v1 (`~/.claude/skills/myvector/tickets.md`, "
             "sections Frontmatter, Statuses, Body, Log). The tool never rewrites tickets itself: you do it, "
             "the owner approves the decisions marked `?`.\n")
    L.append("## Facts (structure only, no ticket text)\n")
    L.append(f"- location: `{d}` ({'single file' if det.is_file else 'directory'}), format: `{det.kind}`")
    L.append(f"- markdown files: {det.n_md}; with a header: {det.n_frontmatter}; with `id`: {det.n_with_id}; "
             f"already canonical: {det.n_canon}")
    if det.is_file:
        L.append(f"- task headings (`### T<n>.`): {det.n_items}; checkboxes: {det.n_boxes}")
    if det.n_legacy_epic or det.n_legacy_groups:
        L.append(f"- legacy: {det.n_legacy_epic} tickets with `epic:`, {det.n_legacy_groups} synthetic groups")
    L.append(f"- status form: `{det.status_form}`; section language: `{det.lang}`")
    if det.git_root is None:
        L.append("- git: not inside a git repository (history rules below do not apply; keep a copy)")
    elif det.git_root == ".":
        L.append("- git root: the project root")
    else:
        L.append(f"- git root: `{det.git_root}` (a sub-repository: the branch and PR live there)")
    L.append("")
    L.append("## Header fields: now -> canon\n")
    if fmap:
        L.append("| field now | files | canon |")
        L.append("|---|---|---|")
        for f in fmap:
            tgt = {"=": "same", "local": "keep (local field)"}.get(f["target"], f"`{f['target']}`")
            L.append(f"| `{f['key']}` | {f['count']} | {tgt}{' - ' + f['note'] if f['note'] and f['target'] != 'local' else ''} |")
    else:
        L.append("No headers yet: every ticket needs `id`, `title`, `status`, `parent`, `size`, `deps`, `blocks`.")
    L.append("")
    L.append("## Statuses: now -> canon\n")
    if smap:
        L.append("| value now | files | canon |")
        L.append("|---|---|---|")
        for s in smap:
            L.append(f"| `{s['raw']}` | {s['count']} | {'**?**' if s['target'] == '?' else '`' + s['target'] + '`'}"
                     f"{' - ' + s['note'] if s['note'] else ''} |")
        L.append("\nValues marked `?` need an owner decision before any edit.")
    else:
        L.append("No statuses found. Use keys (`todo`, `in_progress`, `done` ...); `done` carries a date when "
                 "it is known from the header, `updated` or git.")
    L.append("")
    if det.headings:
        L.append("## Section headings found (names only)\n")
        for h, n in det.headings.items():
            L.append(f"- `{h}` x {n}")
        L.append("")
    L.append("## Rules\n")
    L.append("- **History.** One branch `t/<project-ticket>-tickets-migrate` and one PR in the project. First a "
             "commit with only `git mv` (renames/moves, so git sees renames), then a commit with the edits. Never "
             "delete + create the same ticket. When splitting one file (a `todo.md`), create the new files and "
             "leave a short pointer to `INDEX.md` in the old one (or delete it in the same PR if the owner agrees); "
             "move the text verbatim.")
    L.append("- **Do not lose anything.** Keep existing `## Log`, `<!-- -->` comments, project-local header fields "
             "and historical sections in the body as they are. Do not rewrite `## Log`; add one migration line with "
             "`myvec ticket log` in the project's migration ticket (not in every ticket).")
    L.append(f"- **Statuses.** Keep the project's form (`{det.status_form}`); with none, use keys. Mixed forms are "
             "unified to one.")
    L.append(f"- **Language.** Section names stay in the project's language (`{det.lang}`); a goal heading, if you add "
             "one, must be one the parser knows (see the alias table in `tickets.md`); bodies are not changed "
             "unless the owner agrees (`--fix --add-goal-heading`); `Acceptance criteria` (and its Russian and "
             "Ukrainian forms), `Log`, `Comments` are language-independent.")
    L.append("- **Ids and groups.** Propose the id scheme (folder -> `kind: group`, file -> `<group>-<NN>`) and get "
             "the owner's approval before editing; `deps`/`blocks` must be symmetric.")
    L.append("- **Secrets.** Never copy secrets; do not touch `.env*` or deny-listed files.")
    L.append(f"- **Marker.** If the tickets are not in `docs/todo`, add to `.myvector.toml` (by hand, the tool never "
             f"rewrites it):\n\n  ```toml\n  [tickets]\n  dir = \"{d if not det.is_file else '<new directory>'}\"\n  ```\n")
    L.append("## Steps\n")
    L.append("1. `myvec ticket migrate --fix` (dry-run: assigns missing ids, `epic` -> `parent`, group statuses), then "
             "`--fix --write`; a single todo file: `--dir FILE --split` (dry-run), then `--split --write`.")
    L.append("2. Edits by this brief (statuses, ids, sections) after the owner approves the `?` rows.")
    L.append(f"3. `myvec ticket migrate --dir {d} --check` until it exits 0.")
    L.append("4. `myvec ticket check --strict`, then `myvec ticket index`, then the PR.")
    return "\n".join(L) + "\n"


# ---------- Перевірка ----------

def _form_of(raw) -> str | None:
    s = str(raw or "").replace("️", "").strip()
    if not s:
        return None
    return "emoji" if s[0] in T.STATUS_EMOJI else "keys"


def migration_check(root, rel: str) -> tuple[list[T.Issue], int]:
    """(issues, к-сть тікетів). `done` = немає помилок і жодного коду з LEFTOVER_CODES."""
    root = Path(root)
    path = root / rel
    if path.is_file():
        det = detect(root, rel)
        return [T.Issue("warning", "not_ticket", None, rel,
                        f"single file with {det.n_items} tasks and {det.n_boxes} checkboxes: split into tickets")], 0
    ts = T.load_dir(root, rel)
    issues = T.check(ts)
    forms = set()
    for t in ts.tickets.values():
        if t.legacy and t.kind == "group":
            issues.append(T.Issue("warning", "legacy_group", t.id, t.path,
                                  "synthetic group without id: run --fix --write"))
        if t.kind == "ticket" and not T.body_sections(t.body)["goal"]:
            issues.append(T.Issue("warning", "no_goal", t.id, t.path,
                                  "no recognized goal heading (optional; `--fix --add-goal-heading` adds one)"))
        if not t.legacy or t.kind != "group":
            f = _form_of(t.status_raw)
            if f:
                forms.add(f)
    if len(forms) > 1:
        issues.append(T.Issue("warning", "mixed_status_form", None, rel, "statuses mix emoji and keys"))
    known = {t.path for t in ts.tickets.values()}
    for f in _md_files(path):
        shown = f"{rel}/{f.relative_to(path).as_posix()}"
        if shown in known:
            continue
        try:
            parsed = T.parse_frontmatter(f.read_text(encoding="utf8", errors="replace"))
        except T.FrontmatterError:
            continue
        if parsed is None or not parsed[0].get("id"):
            if parsed is None and ops.START in f.read_text(encoding="utf8", errors="replace"):
                continue      # a generated index under a custom name (`ticket index --index-name`)
            if f.name == "index.md" and T._EPIC_DIR.match(f.parent.name):
                continue
            issues.append(T.Issue("warning", "not_ticket", None, shown, "no header or no id: not a ticket"))
    for other in find_candidates(root):
        if other == rel or other.startswith(rel.rstrip("/") + "/"):
            continue
        d = detect(root, other)
        if d.n_items or d.n_boxes or (not d.is_file and d.kind != "notes" and d.kind != "canon"):
            issues.append(T.Issue("warning", "leftover_todo", None, other, f"tasks outside {rel} ({d.kind})"))
    issues += T.health(ts.tickets.values(), datetime.date.today(), lang=ops.lang_for(path))
    issues += legacy_script_issues(root)
    n = sum(1 for t in ts.tickets.values() if t.kind != "group")
    return issues, n


def left_over(issues) -> list[T.Issue]:
    return [i for i in issues if i.severity == "error" or i.code in LEFTOVER_CODES]


# ---------- Механічні правки ----------

@dataclass
class Fix:
    path: str          # відносно кореня проєкту
    changes: list      # [(було, стало)]
    new_text: str
    source: str = ""   # the text the plan was built from


def _fm_key_line(lines: list[str], end: int, key: str) -> int | None:
    pat = re.compile(rf"^{re.escape(key)}:(?:\s|$)")
    for i in range(1, end):
        if pat.match(lines[i]):
            return i
    return None


_NUM_ID = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+[a-z]?)(?=-|$)")
_STEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")


def _first_h1(body: str) -> str | None:
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            m = _H1.match(line)
            if m:
                return m.group(1)
    return None


_KEY_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:(?:\s|$)")


def _value_end(lines: list[str], ln: int, end: int) -> int:
    """Index just after the last line of the value of the key at `ln` (a new key goes on a key boundary, never
    inside a multi-line value): the next unindented `key:` line or the end of the header."""
    j = ln + 1
    while j < end and not _KEY_LINE.match(lines[j]):
        j += 1
    while j > ln + 1 and not lines[j - 1].strip():
        j -= 1
    return j


def _fix_text(text: str, folder: str, is_index: bool, group_status: str, assign: dict | None = None,
              goal: str = "") -> tuple[str, list] | None:
    """Mechanical header fixes. `assign`: {id, title?, parent?, kind?} for a header without id (ids come from
    `_assign_ids`); `goal`: the goal heading to add to a ticket body that has none (None/"" - bodies untouched)."""
    nl = ops._nl(text)
    try:
        parsed = T.parse_frontmatter(text)
    except T.FrontmatterError:
        return None
    em = T._EPIC_DIR.match(folder)
    if parsed is None:
        if is_index and em:
            hdr = [f"id: {em.group(1)}", f"title: {em.group(2)}", "kind: group", f"status: {group_status}"]
            return "---" + nl + nl.join(hdr) + nl + "---" + nl + text, [("(no header)", "; ".join(hdr))]
        return None
    fm = parsed[0]
    lines = text.splitlines(keepends=True)
    changes: list = []

    def put(key: str, value: str, top: bool = False, label: str = "", after: str | None = None) -> None:
        """Replace an existing (empty) key line or insert a new one; never duplicates a key."""
        _, end = ops._fm_bounds(lines)
        ln = _fm_key_line(lines, end, key)
        if ln is not None:
            lines[ln] = f"{key}: {value}" + (lines[ln][len(lines[ln].rstrip("\r\n")):] or nl)
        else:
            tl = _fm_key_line(lines, end, after or "title")
            at = 1 if top else (_value_end(lines, tl, end) if tl is not None else end)
            lines.insert(at, f"{key}: {value}" + nl)
        changes.append((label or f"({key} missing)", f"{key}: {value}"))

    if not fm.get("id"):
        if assign:
            put("id", assign["id"], top=True, label="(no id)")
            if not fm.get("title") and assign.get("title"):
                put("title", ops._yaml_scalar(assign["title"]), after="id")
            if assign.get("kind") and fm.get("kind") in (None, ""):
                put("kind", assign["kind"], after="title")
            if assign.get("status") and fm.get("status") in (None, ""):
                put("status", assign["status"], after="kind")
            if assign.get("parent") and not fm.get("parent") and not fm.get("epic"):
                put("parent", assign["parent"], after="title")
        else:
            if not (is_index and em) or fm.get("kind") not in (None, "", "group"):
                return None
            put("id", em.group(1), top=True, label="(no id)")
            if not fm.get("title"):
                put("title", em.group(2))
            if fm.get("kind") in (None, ""):
                put("kind", "group")
            if fm.get("status") in (None, ""):
                put("status", group_status)
    epic = fm.get("epic")
    if epic and isinstance(epic, str) and not fm.get("parent"):
        _, end = ops._fm_bounds(lines)
        i = _fm_key_line(lines, end, "epic")
        if i is not None and lines[i].split(":", 1)[1].strip()[:1] not in (">", "|"):
            old = lines[i].rstrip("\r\n")
            lines[i] = lines[i].replace("epic:", "parent:", 1)
            changes.append((old, lines[i].rstrip("\r\n")))
    if goal:
        add = _add_goal(lines, fm, assign, goal, nl)
        if add:
            changes.append(add)
    return ("".join(lines), changes) if changes else None


def _add_goal(lines: list[str], fm: dict, assign: dict | None, goal: str, nl: str):
    """Inserts the goal heading into a ticket body that has none (explicit flag only). Returns the change or None."""
    kind = (assign or {}).get("kind") or fm.get("kind") or "ticket"
    if kind == "group":
        return None
    _, end = ops._fm_bounds(lines)
    body = "".join(lines[end + 1:])
    if T.body_sections(body)["goal"] or any(T._GOAL_HEAD.match(l) for l in body.splitlines()):
        return None
    title = str(fm.get("title") or (assign or {}).get("title") or "").strip()
    at = end + 1
    while at < len(lines) and (not lines[at].strip() or _H1.match(lines[at].rstrip("\r\n"))):
        at += 1
    intro = at < len(lines) and not lines[at].startswith("## ")
    if intro:
        block = [f"## {goal}{nl}", nl]
    elif title:
        block = [f"## {goal}{nl}", nl, f"{title}{nl}", nl]
    else:
        block = [f"## {goal}{nl}", nl]
    if at >= len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    lines[at:at] = block
    return ("(no goal heading)", f"## {goal}")


def group_status_from_children(folder: Path, emoji: bool) -> str:
    """Status of a group by its children (the ticket files next to `index.md`): all done -> done with the latest
    date; any in progress/review -> in progress; otherwise todo. No children -> todo."""
    todo, prog = ("⬜", "🟦") if emoji else ("todo", "in_progress")
    states: list[str] = []
    dates: list[str] = []
    for f in sorted(folder.glob("*.md")):
        if f.name == "index.md" or f.is_symlink():
            continue
        try:
            parsed = T.parse_frontmatter(f.read_text(encoding="utf8", errors="replace"))
        except (T.FrontmatterError, OSError):
            continue
        if not parsed or "status" not in parsed[0]:
            continue
        ns = T.normalize_status(parsed[0]["status"])
        if ns is None or ns[0] == "cancelled":
            continue
        states.append(ns[0])
        if ns[0] == "done":
            d = ns[1] or str(parsed[0].get("updated") or "")[:10]
            if ops.DATE_RE.match(d):
                dates.append(d)
    if states and all(x == "done" for x in states):
        d = max(dates) if dates else datetime.date.today().isoformat()
        return f"✅ {d}" if emoji else f"done {d}"
    if any(x in ("in_progress", "review", "partial") for x in states):
        return prog
    return todo


def _ident(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", name).upper()


def _assign_ids(base: Path, files: list[Path], emoji: bool) -> dict[Path, dict]:
    """Ids for headers without `id` (deterministic): the leading `<letters><digits>-<n>` of the file name when it
    has one; otherwise the file name; otherwise `<group>-<NN>`, where the group is the `E..` folder, the id of the folder's `index.md` or the
    folder name in capitals, and NN follows the file-name order after the highest NN already taken. Never reuses an
    id. The file name itself (`01-home`) is the id when it is free, so strict checks stay green; only when it is
    taken does the id become `<group>-<NN>` and the file should be renamed by hand. `parent` is the id of the folder's `index.md` (existing or assigned here)."""
    heads: dict[Path, tuple[dict, str]] = {}
    used: set[str] = set()
    for f in files:
        try:
            parsed = T.parse_frontmatter(ops.read_text(f))
        except T.FrontmatterError:
            continue
        if not parsed:
            continue
        heads[f] = parsed
        if parsed[0].get("id"):
            used.add(str(parsed[0]["id"]))
    out: dict[Path, dict] = {}
    for folder in sorted({f.parent for f in files}):
        em = T._EPIC_DIR.match(folder.name)
        idx = folder / "index.md"
        idx_fm = heads[idx][0] if idx in heads else None
        if idx_fm is not None and idx_fm.get("id"):
            gid = str(idx_fm["id"])
        elif em:
            gid = em.group(1)
        elif folder == base:
            gid = None
        else:
            gid = _ident(folder.name) or None
        has_group = bool(gid) and ((idx_fm is not None and bool(idx_fm.get("id"))) or (em is not None and idx.is_file()))
        if idx_fm is not None and not idx_fm.get("id") and not em and gid and gid not in used:
            out[idx] = {"id": gid, "title": _first_h1(heads[idx][1]) or folder.name, "kind": "group",
                        "status": group_status_from_children(folder, emoji)}
            used.add(gid)
            has_group = True

        def next_num(gid=gid) -> int:
            nums = [int(m.group(1)) for i in used if gid and (m := re.fullmatch(re.escape(gid) + r"-(\d+)", i))]
            return max(nums, default=0) + 1

        todo = [x for x in files if x.parent == folder and x in heads and x.name != "index.md"
                and not heads[x][0].get("id")]
        # files with a canonical `E1-05` prefix first: they keep their number, the rest get what is free
        todo.sort(key=lambda x: (not _NUM_ID.match(x.stem), ops.natural_key(x.name)))
        for f in todo:
            nm = _NUM_ID.match(f.stem)
            tid = nm.group(1) if nm and nm.group(1) not in used else None
            if tid is None and _STEM_ID.match(f.stem) and f.stem not in used:
                tid = f.stem            # the file name starts with its id: no `id_filename_mismatch`
            if tid is None:
                if gid is None:
                    continue
                tid = f"{gid}-{next_num():02d}"
            used.add(tid)
            a = {"id": tid}
            h1 = _first_h1(heads[f][1])
            if h1:
                a["title"] = h1
            if has_group:
                a["parent"] = gid
            out[f] = a
    return out


def fix_plan(root, rel: str, add_goal: bool = False) -> list[Fix]:
    """Mechanical fixes. `add_goal` (explicit flag only) also adds the goal heading to ticket bodies that lack one."""
    root = Path(root)
    base = inside(root, rel)
    if not base.is_dir():
        return []
    det_forms = []
    files = []
    for f in _md_files(base):
        shown = f"{rel}/{f.relative_to(base).as_posix()}"
        try:
            inside(root, shown)
        except ops.TicketOpError:
            continue
        files.append(f)
        try:
            parsed = T.parse_frontmatter(f.read_text(encoding="utf8", errors="replace"))
        except T.FrontmatterError:
            continue
        if parsed and isinstance(parsed[0].get("status"), str):
            det_forms.append(parsed[0]["status"])
    emoji = _status_form(det_forms) == "emoji"
    assign = _assign_ids(base, files, emoji)
    goal = GOAL_HEADING[ops.lang_for(base)] if add_goal else ""
    plan: list[Fix] = []
    for f in files:
        shown = f"{rel}/{f.relative_to(base).as_posix()}"
        text = ops.read_text(f)
        group_status = group_status_from_children(f.parent, emoji) if f.name == "index.md" else ("⬜" if emoji else "todo")
        res = _fix_text(text, f.parent.name, f.name == "index.md", group_status, assign.get(f), goal)
        if res and res[0] != text:
            plan.append(Fix(shown, res[1], res[0], text))
    return plan


# ---------- Розбиття одного файлу (`--split`) ----------

@dataclass
class SplitFile:
    path: str          # відносно кореня проєкту
    text: str
    kind: str          # group | ticket | pointer
    source: str = ""   # pointer: the text it replaces (re-checked before the write)


def _slug_word(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class AlreadySplit(ops.TicketOpError):
    """The file is the pointer left by an earlier `--split --write` (not an error for the CLI)."""


def split_plan(root, rel: str, group_id: str = "E1", group_slug: str = "backlog", group_title: str | None = None,
               dest: str = "docs/todo", status_style: str = "keys") -> list[SplitFile]:
    """Deterministic split of a single todo file into a group and tickets; the text is moved verbatim.

    Items are the `### <letters><n>. Title` headings (or, when there are none, the `## ` sections). An item runs to
    the next heading of the same or a higher level; everything outside the items (the preamble, `# Plan`, the
    sections after the last item) goes into the group `index.md`. Every item heading stays as the H1 of its ticket.
    Status by checkboxes: all ticked - done, none - todo, some - partial (a group: by its children). Raises
    TicketOpError when the file has no items, the target exists or the result would lose a line."""
    root = Path(root)
    src = inside(root, rel)
    if not src.is_file():
        raise ops.TicketOpError(f"{rel}: not a file")
    if is_pointer(src):
        raise AlreadySplit(f"{rel}: already split (it is a pointer now)")
    if not T._EPIC_DIR.match(f"{group_id}-{group_slug}"):
        raise ops.TicketOpError("group id/slug must look like E1 / backlog")
    dest_dir = f"{dest.rstrip('/')}/{group_id}-{group_slug}"
    inside(root, dest_dir)
    if (root / dest_dir).exists():
        raise ops.TicketOpError(f"{dest_dir}: already exists, refusing to split into it")
    text = ops.read_text(src)
    nl = ops._nl(text)
    lines = text.splitlines(keepends=True)

    def heads(level: int, task_only: bool) -> list[int]:
        out, fenced = [], False
        for n, l in enumerate(lines):
            if l.lstrip().startswith("```"):
                fenced = not fenced
                continue
            m = None if fenced else _HEAD.match(l.rstrip("\r\n"))
            if m and len(m.group(1)) == level and (not task_only or _TASK_HEAD.match(m.group(2))):
                out.append(n)
        return out

    level = 3
    starts = heads(3, True)
    if not starts:
        level, starts = 2, heads(2, False)
    if not starts:
        raise ops.TicketOpError(f"{rel}: no items (`### T<n>.` or `## ` headings) to split")

    def is_boundary(n: int) -> bool:
        """A heading above the item level closes the item (same-level headings only if they are items)."""
        m = re.match(r"^(#{1,6})\s", lines[n])
        return bool(m) and len(m.group(1)) < level

    fenced_at: set[int] = set()
    fenced = False
    for n, l in enumerate(lines):
        if l.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            fenced_at.add(n)
    items: list[tuple[int, int]] = []
    rest: list[str] = []
    prev_end = 0
    for k, st in enumerate(starts):
        rest += lines[prev_end:st]
        end = st + 1
        while end < len(lines) and not (end in starts or (end not in fenced_at and is_boundary(end))):
            end += 1
        items.append((st, end))
        prev_end = end
    rest += lines[prev_end:]
    h1 = next((_H1.match(l.rstrip("\r\n")).group(1) for l in lines if _H1.match(l.rstrip("\r\n"))), None)
    title = group_title or h1 or group_slug
    emoji = status_style == "emoji"
    st_todo, st_part, st_done = ("⬜", "🟧", "✅") if emoji else ("todo", "partial", "done")
    files: list[SplitFile] = []
    states: list[str] = []
    for n, (a, b) in enumerate(items, 1):
        head = _HEAD.match(lines[a].rstrip("\r\n")).group(2)
        tm = _TASK_HEAD.match(head)
        name = re.sub(r"^\S+?[.:)]\s*", "", head, count=1) if tm else head
        boxes = [_BOX.match(l).group(1) for l in lines[a + 1:b] if _BOX.match(l)]
        state = st_todo if not boxes or not any(x != " " for x in boxes) else (
            st_done if all(x != " " for x in boxes) else st_part)
        states.append(state)
        tid = f"{group_id}-{n:02d}"
        slug = _slug_word((tm.group(1) + tm.group(2)) if tm else "") or "item"
        words = _slug_word(name).split("-")
        slug = "-".join([slug] + [w for w in words if w][:4]) if _slug_word(name) else slug
        fm = ["---", f"id: {tid}", f"title: {ops._yaml_scalar(name.strip())}", "kind: ticket", f"status: {state}",
              f"parent: {group_id}", "---", ""]
        body = [f"# {head}" + nl] + lines[a + 1:b]
        files.append(SplitFile(f"{dest_dir}/{tid}-{slug}.md", nl.join(fm) + "".join(body), "ticket"))
    all_done = all(x == st_done for x in states)
    gstate = st_done if all_done else (("🟦" if emoji else "in_progress") if any(x != st_todo for x in states)
                                       else st_todo)
    gfm = ["---", f"id: {group_id}", f"title: {ops._yaml_scalar(title)}", "kind: group", f"status: {gstate}", "---", ""]
    files.insert(0, SplitFile(f"{dest_dir}/index.md", nl.join(gfm) + "".join(rest), "group"))
    pointer = (f"{POINTER_MARK}{nl}{nl}This file was split into tickets: see `{dest_dir}/` "
               f"(index: `{dest.rstrip('/')}/INDEX.md`).{nl}")
    files.append(SplitFile(rel, pointer, "pointer", text))
    # nothing may be lost: every line of the original is in the new files
    from collections import Counter as _C

    have = _C(l.rstrip("\r\n") for f in files if f.kind != "pointer" for l in f.text.splitlines())
    heads_as_h1 = {a: "# " + _HEAD.match(lines[a].rstrip("\r\n")).group(2) for a, _ in items}
    need = _C(heads_as_h1.get(n, l.rstrip("\r\n")) for n, l in enumerate(lines))
    lost = [k for k, v in need.items() if k.strip() and have[k] < v]
    if lost:
        raise ops.TicketOpError(f"{rel}: split would lose {len(lost)} line(s); not written")
    return files


def apply_split(root, files: list[SplitFile]) -> None:
    root = Path(root)
    for f in files:
        inside(root, f.path)
    for f in files:
        if f.kind != "pointer" and (root / f.path).exists():
            raise ops.TicketOpError(f"{f.path}: already exists, refusing to overwrite")
    for f in files:
        if f.kind == "pointer" and ops.read_text(root / f.path) != f.source:
            raise ops.TicketOpError(f"{f.path}: changed since the plan was made; run --split again")
    for f in files:
        if f.kind != "pointer":
            (root / f.path).parent.mkdir(parents=True, exist_ok=True)
        ops.write_atomic(root / f.path, f.text)


# ---------- Проєктні скрипти зі старими полями ----------

_LEGACY_KEY = re.compile(r"\b(epic|estimate)\b")
_SCRIPT_MAX = 200_000


def _script_hits(path: Path, shown: str) -> list[tuple[str, int]]:
    """[(shown, line)] of lines that mention `tickets` and an old field; contents are never returned."""
    if filters.is_denied_path(shown):   # .env*, keys, secrets, ...: never opened (same deny-list as the indexer)
        return []
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _SCRIPT_MAX:
            return []
        lines = path.read_text(encoding="utf8", errors="replace").splitlines()
    except OSError:
        return []
    named = "ticket" in path.name.lower()
    whole = any(_LEGACY_KEY.search(l) for l in lines) and (named or any("tickets" in l.lower() for l in lines))
    hits = []
    for n, l in enumerate(lines, 1):
        low = l.lower()
        if "myvec ticket" in low:
            continue
        if "tickets" in low and (whole or _LEGACY_KEY.search(l)):
            hits.append((shown, n))
    if not hits and named and whole and not any("myvec ticket" in l.lower() for l in lines):
        hits.append((shown, next(n for n, l in enumerate(lines, 1) if _LEGACY_KEY.search(l))))
    return hits


def legacy_scripts(root) -> list[tuple[str, int]]:
    """Project scripts (package.json scripts, Makefile, top-level scripts/*) that mention `tickets` and parse the
    old header fields (`epic`, `estimate`): [(file, line)]. Names and line numbers only, no contents."""
    root = Path(root)
    out: list[tuple[str, int]] = []
    pj = root / "package.json"
    try:
        if not filters.is_denied_path("package.json") and pj.is_file() and not pj.is_symlink() and pj.stat().st_size <= _SCRIPT_MAX:
            text = pj.read_text(encoding="utf8", errors="replace")
            scripts = json.loads(text).get("scripts")
            if isinstance(scripts, dict):
                lines = text.splitlines()
                for key, val in scripts.items():
                    if "tickets" in f"{key} {val}".lower() and "myvec ticket" not in str(val).lower():
                        n = next((i for i, l in enumerate(lines, 1) if l.strip().startswith(json.dumps(key))), 1)
                        out.append(("package.json", n))
    except (OSError, ValueError, AttributeError):
        pass
    for name in ("Makefile", "makefile", "GNUmakefile"):
        out += _script_hits(root / name, name)
    sd = root / "scripts"
    if sd.is_dir() and not sd.is_symlink():
        for f in sorted(sd.iterdir())[:200]:
            out += _script_hits(f, f"scripts/{f.name}")
    return sorted(set(out))


def legacy_script_issues(root) -> list[T.Issue]:
    return [T.Issue("warning", "legacy_script", None, f"{f}:{n}",
                    "project script mentions tickets and old fields (epic/estimate): replace with `myvec ticket`")
            for f, n in legacy_scripts(root)]


def inside(root, rel: str) -> Path:
    """`root/rel` resolved; it must stay inside `root` and no component below `root` may be a symlink."""
    root = Path(root).resolve()
    p = root / rel
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise ops.TicketOpError(f"{rel}: symlink in the path, refused")
    try:
        p.resolve().relative_to(root)
    except ValueError:
        raise ops.TicketOpError(f"{rel}: outside the project root, refused") from None
    return p


def apply_fixes(root, plan: list[Fix]) -> None:
    for fx in plan:   # all paths are checked before the first write
        inside(root, fx.path)
    for fx in plan:
        p = Path(root) / fx.path
        if ops.read_text(p) != fx.source:
            raise ops.TicketOpError(f"{fx.path}: changed since the plan was made; run --fix again")
        ops.write_atomic(p, fx.new_text)
