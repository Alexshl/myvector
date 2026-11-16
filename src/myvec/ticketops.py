"""Операції над тікетами поверх `myvec.tickets` (ADR-004): індекс, шаблон, Log, claim.

Чистий модуль: без typer і БД. Файловий ввід-вивід — лише читання/запис тікетів і журналу claim."""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from myvec import config, filters
from myvec.tickets import STATUS_EMOJI, Ticket, TicketSet, progress_all

START, END = "<!-- generated:start -->", "<!-- generated:end -->"
_KEY_EMOJI = {k: e for e, k in STATUS_EMOJI.items()}
_KEY_EMOJI["frozen"] = "❄️"
_BY_CHECKBOX = ("in_progress", "review", "waiting", "blocked", "partial", "frozen")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\Z")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


class TicketOpError(ValueError):
    """Помилка операції, текст показується користувачу."""


def natural_key(tid: str) -> list:
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", tid) if p]


# ---------- Запис файлів ----------

def read_text(path: Path) -> str:
    return Path(path).read_bytes().decode("utf8")


def write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    if path.is_symlink():
        raise TicketOpError(f"{path} — символічне посилання: запис відхилено")
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    if tmp.is_symlink():
        raise TicketOpError(f"{tmp} — символічне посилання: запис відхилено")
    try:
        tmp.write_bytes(text.encode("utf8"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


# ---------- Шапка ----------

def _fm_bounds(lines: list[str]) -> tuple[int, int]:
    if not lines or lines[0].rstrip("\r\n").rstrip() != "---":
        raise TicketOpError("у файлі нема шапки frontmatter")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").rstrip() == "---":
            return 0, i
    raise TicketOpError("шапку frontmatter не закрито")


def set_fm_line(text: str, key: str, value: str | None) -> str:
    """Замінює або додає один рядок шапки `key: value`; решта файла не чіпається."""
    lines = text.splitlines(keepends=True)
    _, end = _fm_bounds(lines)
    nl = _nl(text)
    new = f"{key}: {value}" if value not in (None, "") else f"{key}:"
    pat = re.compile(rf"^{re.escape(key)}:(?:\s|$)")
    for i in range(1, end):
        if pat.match(lines[i]):
            raw = lines[i].split(":", 1)[1].strip()
            nxt = lines[i + 1] if i + 1 < end else ""
            cont = raw != "" and nxt[:1] in (" ", "\t") and nxt.strip() != ""
            if (raw == "" and re.match(r"^\s+-(\s|$)", nxt)) or raw[:1] in (">", "|") or cont:
                raise TicketOpError(f"поле {key} у блочному форматі: відредагуй вручну")
            eol = lines[i][len(lines[i].rstrip("\r\n")):] or nl
            lines[i] = new + eol
            return "".join(lines)
    lines.insert(end, new + nl)
    return "".join(lines)


def _yaml_scalar(s: str) -> str:
    if s == "" or s != s.strip() or re.search(r"[:#\[\]{}&*!|>'\"%@`,]", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


# ---------- Статуси ----------

def status_text(t: Ticket) -> str:
    emoji = _KEY_EMOJI.get(t.status, t.status)
    return f"{emoji} {t.done_date}" if t.status == "done" and t.done_date else emoji


def _pct(v: float | None) -> int | None:
    return None if v is None else int(v + 0.5)


# ---------- Індекс ----------
# Відсоток у індексі ЗВАЖЕНИЙ: тікет у роботі дає частку своїх acceptance criteria (або 0), тож `75% (0/1)` -
# це не розбіжність: лічильник `(k/n)` рахує лише ЗАВЕРШЕНІ листи, відсоток - ще й часткову роботу.
# Лічильник показується всюди поруч із відсотком (загальний прогрес, групи, таблиця групи) в одному форматі.

def _rel(path: str, from_dir: str) -> str:
    return os.path.relpath(path, from_dir).replace(os.sep, "/")


def _kids(ts: TicketSet) -> dict[str, list[Ticket]]:
    kids: dict[str, list[Ticket]] = {}
    for t in ts.tickets.values():
        if t.parent in ts.tickets:
            kids.setdefault(t.parent, []).append(t)
    for v in kids.values():
        v.sort(key=lambda t: natural_key(t.id))
    return kids


def _leaf_counts(t: Ticket, kids: dict, seen: frozenset = frozenset()) -> tuple[int, int]:
    """(готово, усього) серед нескасованих листів піддерева."""
    ch = [c for c in kids.get(t.id, []) if c.id not in seen and c.id != t.id]
    if not ch:
        if t.kind == "group" or t.status == "cancelled":
            return 0, 0
        return int(t.status == "done"), 1
    d = n = 0
    for c in ch:
        a, b = _leaf_counts(c, kids, seen | {t.id})
        d, n = d + a, n + b
    return d, n


_EN_MARK = "## In one pass"
_RU_MARK = "## За один заход"


_UK_MARK = "## За один захід"
_HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")
_LANG_SAMPLE = 200   # ticket files whose `## ` headings are read when there is no ticket-template


def _heading_lang(base: Path) -> str | None:
    """Language of the ticket headings (`## ` lines only, nothing else is read) when there is no template."""
    score = {"ru": 0, "uk": 0, "en": 0}
    n = 0
    try:
        files = sorted(base.rglob("*.md"))
    except OSError:
        return None
    for f in files:
        if f.name in ("INDEX.md", "ticket-template.md") or f.is_symlink() or not f.is_file():
            continue
        n += 1
        if n > _LANG_SAMPLE:
            break
        try:
            if f.stat().st_size > 256 * 1024:
                continue
            text = f.read_text(encoding="utf8", errors="replace")
        except OSError:
            continue
        fenced = False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
            m = None if fenced else _HEAD_RE.match(line)
            if not m:
                continue
            h = m.group(1).lower()
            if h.startswith(("за один заход", "критерии", "владение", "решение", "проверка", "задачи", "сделано",
                                  "сделать", "тесты", "зависимости", "связи")) \
                    or re.search(r"[ыэъё]", h):
                score["ru"] += 1
            elif h.startswith(("за один захід", "критерії", "володіння", "рішення", "перевірка")) \
                    or re.search(r"[іїєґ]", h):
                score["uk"] += 1
            elif h.startswith(("in one pass", "file ownership", "live check", "plan")):
                score["en"] += 1
    best = max(score, key=lambda k: (score[k], k == "uk"))
    return best if score[best] else None


def lang_for(base: Path) -> str:
    """`en`/`ru`/`uk` by the project's ticket-template section names; without a template - by the `## ` headings of
    the tickets; `uk` (default) when nothing is recognised."""
    base = Path(base)
    tpl = base / "ticket-template.md"
    try:
        text = read_text(tpl) if tpl.is_file() else ""
    except OSError:
        return "uk"
    if _EN_MARK in text:
        return "en"
    if _RU_MARK in text:
        return "ru"
    if text:
        return "uk"
    return _heading_lang(base) or "uk"


_L = {
    "uk": {"total": "Загальний прогрес", "done": "готово", "loose": "Без групи", "prog": "Прогрес",
           "head": ["ID", "Назва", "Статус", "%", "Розмір", "Залежить від", "PR"],
           "index_head": "# Тікети\n\nКанон: `~/.claude/skills/myvector/tickets.md`.\n\n## Статуси\n\n⬜ todo · ⏳ очікує · 🟦 у роботі · 🟨 ревю · 🟧 частково · ⛔ заблоковано · ✅ готово (дата) · 🚫 скасовано · ❄️ заморожено\n\n## Дерево\n\n"},
    "en": {"total": "Overall progress", "done": "done", "loose": "No group", "prog": "Progress",
           "head": ["ID", "Title", "Status", "%", "Size", "Depends on", "PR"],
           "index_head": "# Tickets\n\nCanon: `~/.claude/skills/myvector/tickets.md`.\n\n## Statuses\n\n⬜ todo · ⏳ waiting · 🟦 in progress · 🟨 review · 🟧 partial · ⛔ blocked · ✅ done (date) · 🚫 cancelled · ❄️ frozen\n\n## Tree\n\n"},
    "ru": {"total": "Общий прогресс", "done": "готово", "loose": "Без группы", "prog": "Прогресс",
           "head": ["ID", "Название", "Статус", "%", "Размер", "Зависит от", "PR"],
           "index_head": "# Тикеты\n\nКанон: `~/.claude/skills/myvector/tickets.md`.\n\n## Статусы\n\n⬜ todo · ⏳ ожидает · 🟦 в работе · 🟨 ревью · 🟧 частично · ⛔ заблокировано · ✅ готово (дата) · 🚫 отменено · ❄️ заморожено\n\n## Дерево\n\n"},
}


def render_index(ts: TicketSet, tickets_dir: str = "docs/todo", lang: str = "uk") -> str:
    L = _L[lang]
    pr = progress_all(ts)
    kids = _kids(ts)
    roots = sorted(ts.roots(), key=lambda t: natural_key(t.id))
    lines: list[str] = []

    def node(t: Ticket, depth: int, seen: frozenset):
        ind = "  " * depth
        link = f"[{t.id}]({_rel(t.path, tickets_dir)})"
        ch = [c for c in kids.get(t.id, []) if c.id not in seen and c.id != t.id]
        if t.kind == "group" or ch:
            d, n = _leaf_counts(t, kids)
            p = _pct(pr.get(t.id))
            pct = "" if p is None else f" — {p}% ({d}/{n} {L['done']})"
            lines.append(f"{ind}- **[{t.id} {t.title}]({_rel(t.path, tickets_dir)})**{pct}")
            for c in ch:
                node(c, depth + 1, seen | {t.id})
            return
        p = _pct(pr.get(t.id))
        pct = f" — {p}%" if t.status in _BY_CHECKBOX and p is not None and (p or t.ac_total) else ""
        lines.append(f"{ind}- {status_text(t)} {link} {t.title}{pct}")

    loose = []
    for t in roots:
        if t.kind == "group" or kids.get(t.id):
            node(t, 0, frozenset())
        else:
            loose.append(t)
    if loose:
        lines.append(f"- **{L['loose']}**")
        for t in loose:
            node(t, 1, frozenset())
    vals = [v for t in roots if (v := pr.get(t.id)) is not None]
    total = int(sum(vals) / len(vals) + 0.5) if vals else 0
    done = count = 0
    for t in roots:
        d, n = _leaf_counts(t, kids)
        done, count = done + d, count + n
    return f"{L['total']}: {total}% ({done}/{count})\n\n" + "\n".join(lines)


def render_group_table(ts: TicketSet, group: Ticket, lang: str = "uk") -> str:
    L = _L[lang]
    pr = progress_all(ts)
    kids = _kids(ts)
    d, n = _leaf_counts(group, kids)
    p = _pct(pr.get(group.id))
    head = f"{L['prog']}: {0 if p is None else p}% ({d}/{n})"
    gdir = str(PurePosixPath(group.path).parent)
    rows = ["| " + " | ".join(L["head"]) + " |", "|---|---|---|---|---|---|---|"]
    for t in kids.get(group.id, []):
        tp = _pct(pr.get(t.id))
        title = t.title.replace("|", "\\|")
        rows.append(f"| [{t.id}]({_rel(t.path, gdir)}) | {title} | {status_text(t)} | "
                    f"{'' if tp is None else f'{tp}%'} | {t.size or ''} | {', '.join(t.deps)} | "
                    f"{(t.pr or '').replace('|', chr(92) + '|')} |")
    return head + "\n\n" + "\n".join(rows)


def has_markers(text: str) -> bool:
    s, e = text.find(START), text.find(END)
    return s != -1 and e > s


def replace_generated_text(text: str, body: str) -> str:
    block = f"{START}\n{body}\n{END}"
    s, e = text.find(START), text.find(END)
    if s != -1 and e > s:
        return text[:s] + block + text[e + len(END):]
    return f"{text.rstrip()}\n\n{block}\n"


def replace_generated(path: Path, body: str) -> bool:
    """Записує блок між маркерами (створює файл/блок, якщо нема). True, якщо файл змінився."""
    path = Path(path)
    old = read_text(path) if path.exists() else ""
    new = replace_generated_text(old, body)
    if new == old:
        return False
    write_atomic(path, new)
    return True


# ---------- Новий тікет ----------

BUILTIN_TEMPLATE = """---
id: E0-00
title: Коротка назва
kind: ticket
status: ⬜
parent: E0
size: S
deps: []
blocks: []
branch:
pr:
summary:
updated: 2026-11-09
---

# E0-00 Коротка назва

## За один захід

Що робимо в цьому тікеті й чого свідомо НЕ робимо.

## Контекст

Навіщо; посилання на код, ADR.

## Рішення — кроки

1. Крок

## Володіння файлами

- `path` — що змінюється

## Acceptance criteria

- [ ] Перевірний критерій

## Перевірка наживо

Сценарії або «не потрібна».

## Log

<!-- агенти дописують: ### YYYY-MM-DD агент -->
"""


def new_ticket_text(base_dir: Path, tid: str, title: str, parent: str | None, size: str,
                    deps: list[str], kind: str = "ticket", today: str | None = None) -> str:
    today = today or datetime.date.today().isoformat()
    for name, val in (("title", title), ("id", tid), ("size", size), ("parent", parent or ""),
                      *[("deps", d) for d in deps]):
        if "\n" in val or "\r" in val:
            raise TicketOpError(f"{name} має бути в один рядок")
    if kind == "group":
        text = (f"---\nid: {tid}\ntitle: {_yaml_scalar(title)}\nkind: group\nstatus: ⬜\n"
                f"parent:{' ' + parent if parent else ''}\ndeps: {_yaml_list(deps)}\nblocks: []\nupdated: {today}\n---\n\n# {tid} {title}\n\n"
                f"{START}\n{END}\n")
        return text
    tpl = Path(base_dir) / "ticket-template.md"
    text = read_text(tpl) if tpl.is_file() else BUILTIN_TEMPLATE
    for k, v in (("id", tid), ("title", _yaml_scalar(title)), ("kind", "ticket"), ("status", "⬜"),
                 ("parent", parent), ("size", size), ("deps", _yaml_list(deps)), ("blocks", "[]"),
                 ("updated", today)):
        text = set_fm_line(text, k, v)
    out, done = [], False
    for line in text.splitlines(keepends=True):
        if not done and line.startswith("# "):
            line = f"# {tid} {title}" + line[len(line.rstrip("\r\n")):]
            done = True
        out.append(line)
    return "".join(out)


# ---------- Log ----------

def append_log(text: str, date: str, agent: str, body: str) -> str:
    if not DATE_RE.match(date):
        raise TicketOpError(f"дата має бути YYYY-MM-DD: {date}")
    if not agent.strip() or "\n" in agent or "\r" in agent:
        raise TicketOpError("порожнє ім'я агента")
    body = body.strip()
    if not body:
        raise TicketOpError("порожній текст запису")
    if filters.has_hard_secret(body):
        raise TicketOpError("текст містить приватний ключ або токен: запис відхилено, секрети в git тікета не потрапляють")
    body = filters.mask_secrets(body)[0]
    nl = _nl(text)
    entry = nl.join([f"### {date} {agent.strip()}", "", *body.splitlines()]) + nl
    lines = text.splitlines(keepends=True)
    fence, start, end = None, None, len(lines)
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(```|~~~)", line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif fence == m.group(1):
                fence = None
            continue
        if fence:
            continue
        if start is None:
            if re.match(r"^##\s+Log\s*$", line.rstrip("\r\n")):
                start = i
        elif re.match(r"^##\s", line):
            end = i
            break
    if start is None:
        return text.rstrip("\r\n") + nl + nl + "## Log" + nl + nl + entry
    head = "".join(lines[:end]).rstrip("\r\n")
    rest = "".join(lines[end:])
    return head + nl + nl + entry + (nl + rest if rest else "")


# ---------- claim (ADR-004) ----------

def claims_path() -> Path:
    return config.home() / "state" / "ticket_claims.jsonl"


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


_URL_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")


def sanitize_remote(url: str | None) -> str | None:
    """Прибирає userinfo (user:password@, token@) з URL; scp-стиль git@host:path лишається."""
    return _URL_USERINFO.sub(r"\1", url) if url else url


def record_claim(session_id: str, ticket_id: str | None, project_root: Path, cwd: Path | None = None) -> dict:
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "ticket_id": ticket_id,
        "project_root": str(project_root),
        "git_remote": sanitize_remote(_git(project_root, "remote", "get-url", "origin")),
        "branch": _git(project_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "cwd": str(cwd or Path.cwd()),
    }
    p = claims_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf8"))
    finally:
        os.close(fd)
    return rec


def read_claims() -> dict[str, dict]:
    """Останній запис по session_id (включно зі звільненими, `ticket_id` = None)."""
    out: dict[str, dict] = {}
    p = claims_path()
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("session_id"), str) and rec["session_id"]:
            out[rec["session_id"]] = rec
    return out


def current_claim(session_id: str) -> str | None:
    rec = read_claims().get(session_id)
    return rec.get("ticket_id") if rec else None
