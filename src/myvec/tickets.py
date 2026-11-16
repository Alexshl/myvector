"""Канон тікета v1: розбір frontmatter, статуси, дерево, відсоток, перевірки (ADR-002).

Чистий модуль: без БД, CLI і мережі; файлового вводу-виводу лише в `load_dir`.
YAML — власна підмножина (PyYAML у проєкті немає)."""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

STATUS_KEYS = ("todo", "in_progress", "review", "waiting", "blocked", "partial", "done", "cancelled", "frozen")
STATUS_EMOJI = {
    "⬜": "todo", "🟦": "in_progress", "🟨": "review", "⏳": "waiting", "⛔": "blocked",
    "🟧": "partial", "✅": "done", "🚫": "cancelled", "❄": "frozen",
}
SIZES = ("S", "M", "L")
KINDS = ("ticket", "group")
KNOWN_KEYS = ("id", "title", "kind", "status", "parent", "size", "deps", "blocks", "branch", "pr", "summary", "updated")
SKIP_FILES = ("INDEX.md", "ticket-template.md")
SKIP_DIRS = ("inbox",)

_FM = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.S)
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*)|\s*)$")
_EPIC_DIR = re.compile(r"^(E[0-9a-z]+)-([a-z0-9-]+)$")
_DONE = re.compile(r"^(✅|done)(?:\s+(\d{4}-\d{2}-\d{2}))?$")
_AC_HEAD = re.compile(r"^##\s+(?:Acceptance criteria|Критерии\s+(?:приёмки|приемки|готовности)"
                      r"|Критерії\s+(?:приймання|прийняття|готовності))\b", re.I)
_BOX = re.compile(r"^\s*[-*]\s+\[([ xX])\]")


class FrontmatterError(ValueError):
    """Непідтримана або зламана конструкція у шапці."""


@dataclass
class Issue:
    severity: str  # error | warning | health
    code: str
    ticket_id: str | None
    path: str
    message: str


@dataclass
class Ticket:
    id: str
    title: str = ""
    kind: str = "ticket"
    status: str = "todo"
    status_raw: str | None = None
    done_date: str | None = None
    parent: str | None = None
    size: str | None = None
    deps: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    branch: str | None = None
    pr: str | None = None
    summary: str | None = None
    updated: str | None = None
    path: str = ""
    body: str = ""
    ac_done: int = 0
    ac_total: int = 0
    extra: dict = field(default_factory=dict)
    legacy: bool = False


@dataclass
class TicketSet:
    tickets: dict[str, Ticket] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    def children(self, ticket_id: str) -> list[Ticket]:
        return [t for t in self.tickets.values() if t.parent == ticket_id]

    def roots(self) -> list[Ticket]:
        return [t for t in self.tickets.values() if not t.parent or t.parent not in self.tickets]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


# ---------- YAML-підмножина ----------

def _strip_comment(s: str) -> str:
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or s[i - 1] in " \t[,"):
            quote = ch
        elif ch == "#" and i > 0 and s[i - 1] in " \t":
            return s[:i].rstrip()
    return s.rstrip()


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        inner = s[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if s[0] == '"' else inner.replace("''", "'")
    return s


def _scalar(s: str):
    s = s.strip()
    if s == "":
        return None
    if s[0] in "\"'" and (len(s) < 2 or s[-1] != s[0]):
        raise FrontmatterError(f"незакриті лапки: {s}")
    return _unquote(s)


def _inline_list(s: str) -> list:
    inner = s[1:-1]
    items, cur, quote = [], "", None
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    if quote:
        raise FrontmatterError(f"незакриті лапки у списку: {s}")
    items.append(cur)
    for it in items:
        if it.strip()[:1] in ("[", "{"):
            raise FrontmatterError(f"вкладений список не підтримується: {s}")
    return [v for v in (_scalar(i) for i in items if i.strip()) if v is not None]


def _continue_value(raw: str, lines: list[str], i: int) -> tuple[str, int]:
    """A header value written over several lines: an unclosed quote or `[...]` list runs to its closing character,
    a plain scalar takes the indented lines that follow (folded with a space, as YAML does)."""
    first = raw[0]
    if first in "\"'":
        while (len(_strip_comment(raw)) < 2 or _strip_comment(raw)[-1] != first) and i < len(lines):
            raw += " " + lines[i].strip()
            i += 1
    elif first == "[":
        while not _strip_comment(raw).endswith("]") and i < len(lines):
            raw += " " + _strip_comment(lines[i].strip())
            i += 1
    elif first not in "{&*!":
        while i < len(lines) and lines[i].strip() and lines[i][:1] in " \t":
            raw += " " + lines[i].strip()
            i += 1
    return raw, i


def parse_frontmatter(text: str):
    """(dict, body) або None, якщо шапки нема. Кидає FrontmatterError на непідтримане."""
    m = _FM.match(text)
    if not m:
        return None
    lines = m.group(1).splitlines()
    data: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        km = _KEY.match(line)
        if not km:
            raise FrontmatterError(f"непідтриманий рядок шапки: {line.strip()[:60]}")
        key, raw = km.group(1), km.group(2) or ""
        if key in data:
            raise FrontmatterError(f"ключ повторюється: {key}")
        raw = raw.strip()
        if raw and raw[:1] not in (">", "|"):
            raw, i = _continue_value(raw, lines, i)
        if raw[:1] in (">", "|") and re.fullmatch(r"[>|][+-]?", _strip_comment(raw)):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
                block.append(lines[i].strip())
                i += 1
            while block and not block[-1]:
                block.pop()
            data[key] = (" ".join(b for b in block if b) if raw[0] == ">" else "\n".join(block)) or None
            continue
        raw = _strip_comment(raw)
        if raw.startswith("["):
            if not raw.endswith("]"):
                raise FrontmatterError(f"список не закрито: {key}")
            data[key] = _inline_list(raw)
        elif raw == "":
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and re.match(r"^\s*-(\s|$)", lines[j]):
                items = []
                while j < len(lines):
                    lm = re.match(r"^\s*-(?:\s+(.*))?$", lines[j])
                    if not lm:
                        if lines[j].strip() and lines[j][:1] in " \t":
                            raise FrontmatterError(f"вкладена структура не підтримується: {key}")
                        if lines[j].strip():
                            break
                        j += 1
                        continue
                    item = _scalar(_strip_comment(lm.group(1) or ""))
                    if item is not None:
                        items.append(item)
                    j += 1
                data[key] = items
                i = j
            elif j < len(lines) and lines[j][:1] in " \t" and lines[j].strip():
                raise FrontmatterError(f"вкладена структура не підтримується: {key}")
            else:
                data[key] = None
        elif raw[0] in "{&*!|>":
            raise FrontmatterError(f"непідтримане значення ключа {key}: {raw[:30]}")
        else:
            data[key] = _scalar(raw)
    return data, text[m.end():]


# ---------- Статуси, чекбокси ----------

def normalize_status(raw) -> tuple[str, str | None] | None:
    """Ключ або емодзі → (ключ, дата для done). Невідоме → None."""
    if raw is None:
        return None
    s = str(raw).replace("️", "").strip().lower()
    dm = _DONE.match(s)
    if dm:
        if dm.group(2):
            try:
                datetime.date.fromisoformat(dm.group(2))
            except ValueError:
                return None
        return "done", dm.group(2)
    s = s.replace("-", "_").replace(" ", "_")
    if s in STATUS_KEYS:
        return s, None
    if s in STATUS_EMOJI:
        return STATUS_EMOJI[s], None
    return None


_BOX_TEXT = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s*(.*)$")
_GOAL_HEAD = re.compile(r"^##\s+(?:За один захід|За один заход|In one pass)\b", re.I)
_LOG_HEAD = re.compile(r"^##\s+Log\b", re.I)
_LOG_ENTRY = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})\s*(.*)$")
_CMT_HEAD = re.compile(r"^##\s+Comments\b", re.I)
_CMT_ENTRY = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?\s*(.*)$")
_CMT_WHO = re.compile(r"^(.*?)\s*(?:\(([^()]*)\))?\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def body_sections(body: str) -> dict:
    """Розбір тіла для UI: `ac` (чекбокси першої секції Acceptance criteria), `log` (записи `### дата агент`
    у `## Log`, HTML-коментарі відкинуто), `goal` (текст «За один захід» / «За один заход» / «In one pass» або None),
    `comments` (записи `### YYYY-MM-DD[ HH:MM] author (source)` у `## Comments`: {date, time, author, source, text})."""
    ac: list[dict] = []
    log: list[dict] = []
    cmts: list[dict] = []
    goal: list[str] = []
    ac_state = 0           # 0 - ще не було, 1 - всередині, 2 - завершена
    mode = None            # "goal" | "log" | "comments" | None
    cur: dict | None = None
    fenced = False
    for line in (body or "").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            if mode in ("log", "comments") and cur is not None:
                cur["lines"].append(line)
            elif mode == "goal":
                goal.append(line)
            continue
        if fenced:
            if mode in ("log", "comments") and cur is not None:
                cur["lines"].append(line)
            elif mode == "goal":
                goal.append(line)
            continue
        is_h2 = bool(re.match(r"^##\s", line))
        if _AC_HEAD.match(line):
            ac_state = 1 if ac_state == 0 else 2
            mode = None
            continue
        if is_h2:
            if ac_state == 1:
                ac_state = 2
            mode = ("goal" if _GOAL_HEAD.match(line) else "log" if _LOG_HEAD.match(line)
                    else "comments" if _CMT_HEAD.match(line) else None)
            cur = None
            continue
        if ac_state == 1:
            bm = _BOX_TEXT.match(line)
            if bm:
                ac.append({"text": bm.group(2).strip(), "done": bm.group(1) != " "})
            continue
        if mode == "goal":
            goal.append(line)
        elif mode == "log":
            lm = _LOG_ENTRY.match(line)
            if lm:
                cur = {"date": lm.group(1), "agent": lm.group(2).strip(), "lines": []}
                log.append(cur)
            elif cur is not None:
                cur["lines"].append(line)
        elif mode == "comments":
            cm = _CMT_ENTRY.match(line)
            if cm:
                who = _CMT_WHO.match(cm.group(3).strip())
                cur = {"date": cm.group(1), "time": cm.group(2), "author": who.group(1).strip(),
                       "source": (who.group(2) or "").strip(), "lines": []}
                cmts.append(cur)
            elif cur is not None:
                cur["lines"].append(line)
    comments = [{"date": e["date"], "time": e["time"], "author": e["author"], "source": e["source"],
                 "text": _COMMENT.sub("", "\n".join(e["lines"])).strip()} for e in cmts]
    entries = [{"date": e["date"], "agent": e["agent"], "text": _COMMENT.sub("", "\n".join(e["lines"])).strip()} for e in log]
    g = _COMMENT.sub("", "\n".join(goal)).strip()
    return {"ac": ac, "log": entries, "comments": comments, "goal": g or None}


def acceptance_checkboxes(body: str) -> tuple[int, int]:
    """(відмічені, усього) чекбоксів у першій секції `## Acceptance criteria`."""
    ac = body_sections(body)["ac"]
    return sum(1 for x in ac if x["done"]), len(ac)


# ---------- Розбір одного файлу ----------

def _str(v):
    return None if v is None else str(v)


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def parse_ticket(text: str, path: str, folder: str | None = None) -> tuple[Ticket | None, list[Issue]]:
    """Тікет з тексту файла. None, якщо файл без шапки (нотатка) або шапка непридатна."""
    issues: list[Issue] = []

    def add(sev, code, tid, msg):
        issues.append(Issue(sev, code, tid, path, msg))

    try:
        parsed = parse_frontmatter(text)
    except FrontmatterError as e:
        add("error", "bad_frontmatter", None, f"Шапка не розібрана: {e}")
        return None, issues
    fname = Path(path).name
    is_index = fname == "index.md"
    if parsed is None:
        em = _EPIC_DIR.match(folder or "")
        if is_index and em:
            t = Ticket(id=em.group(1), title=em.group(2), kind="group", status="todo",
                       path=path, body=text, legacy=True)
            t.ac_done, t.ac_total = acceptance_checkboxes(text)
            return t, issues
        return None, issues
    fm, body = parsed

    def sc(key):
        v = fm.get(key)
        if isinstance(v, list):
            add("error", "bad_frontmatter", _str(fm.get("id")) if key != "id" else None,
                f"Поле {key} має бути скаляром, а не списком")
            return None
        return _str(v)

    tid = sc("id")
    legacy = False
    if not tid:
        em = _EPIC_DIR.match(folder or "")
        if is_index and em:
            slug = em.group(2)
            t = Ticket(id=em.group(1), title=sc("title") or slug, kind="group", status="todo",
                       path=path, body=body, legacy=True)
            t.ac_done, t.ac_total = acceptance_checkboxes(body)
            return t, issues
        add("error", "missing_field", None, "У шапці немає id")
        return None, issues

    t = Ticket(id=tid, path=path, body=body)
    t.ac_done, t.ac_total = acceptance_checkboxes(body)
    t.title = sc("title") or ""
    if not t.title and not isinstance(fm.get("title"), list):
        add("error", "missing_field", tid, "У шапці немає title")
    kind = sc("kind") or "ticket"
    if kind not in KINDS:
        add("error", "bad_kind", tid, f"Невідомий kind «{kind}» (ticket або group)")
        kind = "ticket"
    t.kind = kind
    status_val = sc("status")
    if fm.get("status") is None:
        add("error", "missing_field", tid, "У шапці немає status")
    elif status_val is not None:
        t.status_raw = status_val
        ns = normalize_status(t.status_raw)
        if ns is None:
            add("error", "bad_status", tid, f"Невідомий статус «{t.status_raw}»")
        else:
            t.status, t.done_date = ns
    t.parent = sc("parent")
    epic = _str(fm.get("epic"))
    extra = {k: v for k, v in fm.items() if k not in KNOWN_KEYS}
    if epic and not t.parent:
        t.parent, t.legacy = epic, True
        extra.pop("epic", None)
        add("warning", "legacy_epic", tid, f"Застаріле поле epic: {epic} читається як parent")
    t.extra = extra
    size = sc("size")
    if size is not None:
        if size.upper() in SIZES:
            size = size.upper()
        else:
            add("error", "bad_size", tid, f"Невідомий size «{size}» (S, M або L)")
        t.size = size
    t.deps, t.blocks = _as_list(fm.get("deps")), _as_list(fm.get("blocks"))
    t.branch, t.pr, t.summary, t.updated = (_str(fm.get(k)) for k in ("branch", "pr", "summary", "updated"))
    stem = Path(path).stem
    if not is_index and not (stem == tid or stem.startswith(tid + "-")):
        add("warning", "id_filename_mismatch", tid, f"Ім'я файла «{fname}» не починається з id {tid}")
    return t, issues


def load_dir(root, tickets_dir: str = "docs/todo") -> TicketSet:
    """Читає всі тікети теки `root/tickets_dir`."""
    base = Path(root) / tickets_dir
    ts = TicketSet()
    if not base.is_dir():
        return ts
    for f in sorted(base.rglob("*.md")):
        rel = f.relative_to(base)
        if f.name in SKIP_FILES or any(p in SKIP_DIRS for p in rel.parts[:-1]):
            continue
        shown = f"{tickets_dir}/{rel.as_posix()}"
        if f.is_symlink():
            ts.issues.append(Issue("warning", "symlink_skipped", None, shown, "Символічне посилання пропущено"))
            continue
        try:
            text = f.read_text(encoding="utf8", errors="replace")
        except OSError as e:
            ts.issues.append(Issue("error", "read_error", None, shown, f"Файл не прочитано: {e.strerror or e}"))
            continue
        t, issues = parse_ticket(text, shown, f.parent.name)
        ts.issues.extend(issues)
        if t is None:
            continue
        if t.id in ts.tickets:
            ts.issues.append(Issue("error", "dup_id", t.id, shown,
                                   f"Дубль id {t.id} (також {ts.tickets[t.id].path})"))
            continue
        ts.tickets[t.id] = t
    return ts


# ---------- Перевірки ----------

def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    cycles, color = [], {}
    for start in graph:
        if color.get(start):
            continue
        stack = [(start, iter(graph[start]))]
        path = [start]
        color[start] = 1
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if nxt not in graph:
                    continue
                if color.get(nxt) == 1:
                    cycles.append(path[path.index(nxt):] + [nxt])
                elif not color.get(nxt):
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    break
            else:
                color[node] = 2
                path.pop()
                stack.pop()
    return cycles


def check(ts: TicketSet) -> list[Issue]:
    """Issues розбору + перевірки зв'язків (parent, deps/blocks, цикли)."""
    out = list(ts.issues)
    T = ts.tickets

    def add(code, t, msg):
        out.append(Issue("error", code, t.id, t.path, msg))

    for t in T.values():
        if t.parent:
            p = T.get(t.parent)
            if p is None:
                add("dangling_parent", t, f"parent {t.parent} не існує")
            elif p.kind != "group":
                add("parent_not_group", t, f"parent {t.parent} не є kind: group")
        for d in t.deps:
            o = T.get(d)
            if o is None:
                add("dangling_dep", t, f"deps: {d} не існує")
            elif t.id not in o.blocks:
                add("asym_dep", t, f"{t.id} залежить від {d}, але {d}.blocks не містить {t.id}")
        for b in t.blocks:
            o = T.get(b)
            if o is None:
                add("dangling_block", t, f"blocks: {b} не існує")
            elif t.id not in o.deps:
                add("asym_block", t, f"{t.id} блокує {b}, але {b}.deps не містить {t.id}")
    for cyc in _find_cycles({i: list(t.deps) for i, t in T.items()}):
        add("dep_cycle", T[cyc[0]], "Цикл deps: " + " → ".join(cyc))
    for cyc in _find_cycles({i: [t.parent] if t.parent else [] for i, t in T.items()}):
        add("parent_cycle", T[cyc[0]], "Цикл parent: " + " → ".join(cyc))
    return out


# ---------- Здоров'я (ADR-020) ----------

HEALTH_STATUSES = ("todo", "in_progress", "review", "waiting", "blocked", "partial")
STALE_DAYS = {"in_progress": 7, "review": 7, "todo": 30}
NO_SESSION_GRACE_DAYS = 1


def _f(n, key):
    return n.get(key) if isinstance(n, dict) else getattr(n, key, None)


def _days(n: int, lang: str = "uk") -> str:
    """«1 день», «2 дні», «5 днів», «11 днів», «21 день» (uk); ru «1 день/2 дня/5 дней»; en «1 day/2 days»."""
    if lang == "en":
        return f"{n} day" + ("" if n == 1 else "s")
    one = n % 10 == 1 and n % 100 != 11
    few = n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)
    if lang == "ru":
        w = "день" if one else "дня" if few else "дней"
    else:
        w = "день" if one else "дні" if few else "днів"
    return f"{n} {w}"


# Тексти порад за мовою тікетів проєкту (uk - за замовчуванням, ADR-020; ru/en - E5-07).
_HEALTH_MSG = {
    "uk": {"too_big": "Завеликий (розмір L): розбити на тікети", "no_ac": "Нема Acceptance criteria",
           "stale": "Не оновлювався {d}", "no_session": "В роботі без сесії {d}",
           "blocked_ready": "Усі залежності виконані: можна розблокувати",
           "dep_cancelled": "Залежить від скасованого: {ids}"},
    "ru": {"too_big": "Слишком большой (размер L): разбить на тикеты", "no_ac": "Нет Acceptance criteria",
           "stale": "Не обновлялся {d}", "no_session": "В работе без сессии {d}",
           "blocked_ready": "Все зависимости выполнены: можно разблокировать",
           "dep_cancelled": "Зависит от отменённого: {ids}"},
    "en": {"too_big": "Too big (size L): split into tickets", "no_ac": "No Acceptance criteria",
           "stale": "Not updated for {d}", "no_session": "In progress without a session for {d}",
           "blocked_ready": "All dependencies are done: can be unblocked",
           "dep_cancelled": "Depends on a cancelled ticket: {ids}"},
}


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def health(nodes, today: datetime.date, sessions: dict | None = None, now: float | None = None,
           lang: str = "uk") -> list[Issue]:
    """Поради здоров'я листів у роботі (severity `health`, не валять `--strict`). Групи й ✅/🚫/❄️ пропускаються.
    `nodes` - Ticket-и або dict-вузли; `lang` - мова тікетів проєкту (uk/ru/en); `sessions` - {id: [{effective_state, last_seen}]} (без нього `no_session` не рахується)."""
    nodes = list(nodes)
    M = _HEALTH_MSG.get(lang, _HEALTH_MSG["uk"])
    status = {_f(n, "id"): _f(n, "status") for n in nodes}
    out: list[Issue] = []
    for n in nodes:
        st = _f(n, "status")
        if _f(n, "kind") == "group" or st not in HEALTH_STATUSES:
            continue
        tid, path = _f(n, "id"), _f(n, "path") or ""

        def add(code, msg):
            out.append(Issue("health", code, tid, path, msg))

        if _f(n, "size") == "L":
            add("too_big", M["too_big"])
        if not _f(n, "ac_total"):
            add("no_ac", M["no_ac"])
        upd = _date(_f(n, "updated"))
        if st in STALE_DAYS and upd is not None and (today - upd).days >= STALE_DAYS[st]:
            add("stale", M["stale"].format(d=_days((today - upd).days, lang)))
        if sessions is not None and st == "in_progress":
            ss = sessions.get(tid) or []
            if not any(x.get("effective_state") != "ended" for x in ss):
                last = upd
                for x in ss:
                    ls = x.get("last_seen")
                    if ls:
                        d = datetime.datetime.fromtimestamp(float(ls)).date()
                        last = d if last is None or d > last else last
                if last is not None and (today - last).days >= NO_SESSION_GRACE_DAYS:
                    add("no_session", M["no_session"].format(d=_days((today - last).days, lang)))
        deps = list(_f(n, "deps") or [])
        if st == "blocked" and deps and all(status.get(d) == "done" for d in deps):
            add("blocked_ready", M["blocked_ready"])
        gone = [d for d in deps if status.get(d) == "cancelled"]
        if gone:
            add("dep_cancelled", M["dep_cancelled"].format(ids=", ".join(gone)))
    return out


# ---------- Відсоток ----------

def _leaf_progress(t: Ticket) -> float | None:
    if t.status == "cancelled":
        return None
    if t.status == "done":
        return 100.0
    if t.status == "todo" or not t.ac_total:
        return 0.0
    return 100.0 * t.ac_done / t.ac_total


def progress_all(ts: TicketSet) -> dict[str, float | None]:
    """Відсоток кожного вузла; None — скасований (поза середнім)."""
    kids: dict[str, list[str]] = {}
    for t in ts.tickets.values():
        if t.parent in ts.tickets:
            kids.setdefault(t.parent, []).append(t.id)
    memo: dict[str, float | None] = {}

    def calc(tid: str, seen: tuple) -> float | None:
        if tid in memo:
            return memo[tid]
        t = ts.tickets[tid]
        vals = []
        if t.status != "cancelled":
            for c in kids.get(tid, []):
                if c in seen or c == tid:
                    continue
                v = calc(c, seen + (tid,))
                if v is not None:
                    vals.append(v)
        res = sum(vals) / len(vals) if vals else _leaf_progress(t)
        memo[tid] = res
        return res

    for tid in ts.tickets:
        calc(tid, ())
    return memo


def progress(ts: TicketSet, ticket_id: str) -> float | None:
    """Відсоток вузла; None — для скасованого та для невідомого id."""
    return progress_all(ts).get(ticket_id)
