"""Словник проблем тікетів (E3-04): код -> важливість, назва, пояснення й «Що робити» українською.

Єдине місце текстів для вкладки «Проблеми» (UI/API). Коди видають `tickets.parse_ticket`/`check`/`health`,
`ticketdb.refresh_project` і `ticketmigrate`. Невідомий код не ламає вигляд: `describe` дає фолбек із самим кодом.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess

from myvec import gitutil

MIGRATION_BRANCH = "myvec/process-migration"
SEVERITIES = ("error", "warning", "health")   # порядок показу: помилки, попередження, поради
LEGACY_CODES = ("legacy_epic", "legacy_group", "legacy_script", "mixed_status_form", "no_goal", "not_ticket",
                "leftover_todo")   # лікуються міграцією (гілка myvec/process-migration)

MIGRATE_FIX = "myvec ticket migrate --fix --write"
UPDATE_CMD = "myvec scaffold --update"   # single source: the hint here and the SessionStart line (hooks.py)
UPDATE_WRITE = UPDATE_CMD + " --write"   # writes; without --write it only shows the plan and diff
UPDATE_PHRASE = "онови конвенцію"
PROCESS_STATE = ".myvec/scaffold.json"
ADOPT_CMD = "myvec scaffold --adopt"      # plan only; adds just the search rule to a project without a scaffold state
ADOPT_WRITE = ADOPT_CMD + " --write"
SEARCH_RULE = ".claude/rules/search.md"   # equals scaffold.SEARCH_RULE (a test checks; no scaffold import on the fast path)
# sha256 of the raw bytes of every earlier render of search.md -> first PROCESS_VERSION with that text (E8-17, ADR-046).
# Hashes only: the texts live in tests/fixtures/scaffold/search_rule/vN.md; a guard test keeps both in step.
SEARCH_RULE_OLD: dict[str, int] = {
    "d789af02cf26ec18d261852b02534a44eaa3963ef5fafdca9b4f006d976b06fd": 3,
}

# code -> (severity, title, explain, fix text, command or None)
_P = {
    "bad_frontmatter": ("error", "Шапка не розібрана",
                        "Шапка файла зіпсована або має непідтриману конструкцію, тікет не читається.",
                        "Виправити YAML-шапку: поля id, title, status - скалярами, списки лише в deps/blocks.",
                        "myvec ticket check"),
    "missing_field": ("error", "Немає обов'язкового поля", "У шапці бракує id, title або status.",
                      "Дописати поле в шапку тікета.", "myvec ticket check"),
    "bad_kind": ("error", "Невідомий kind", "Поле kind має бути ticket або group.", "Виправити kind у шапці.", None),
    "bad_status": ("error", "Невідомий статус", "Статус не з канону (todo, in_progress, review, done тощо).",
                   "Замінити статус на канонічний (див. skill тікетів).", None),
    "bad_size": ("error", "Невідомий розмір", "Поле size має бути S, M або L.", "Виправити size у шапці.", None),
    "dup_id": ("error", "Дубль id", "Два файли мають однаковий id; другий не береться до уваги.",
               "Змінити id в одному з файлів.", None),
    "read_error": ("error", "Файл не прочитано", "Файл тікета недоступний для читання.",
                   "Перевірити права доступу й що файл не пошкоджений.", None),
    "dangling_parent": ("error", "parent не існує", "Поле parent вказує на тікет, якого немає.",
                        "Виправити parent або створити групу.", None),
    "parent_not_group": ("error", "parent не є групою", "Батьківський тікет має kind: ticket, а не group.",
                         "Зробити батька групою (kind: group) або змінити parent.", None),
    "dangling_dep": ("error", "Висяча залежність", "У deps вказано тікет, якого немає.",
                     "Виправити або прибрати id у deps.", None),
    "dangling_block": ("error", "Висяче блокування", "У blocks вказано тікет, якого немає.",
                       "Виправити або прибрати id у blocks.", None),
    "asym_dep": ("error", "Несиметрична залежність", "Тікет залежить від іншого, але той не містить його в blocks.",
                 "Додати зворотне посилання в blocks у залежного тікета.", None),
    "asym_block": ("error", "Несиметричне блокування", "Тікет блокує інший, але той не містить його в deps.",
                   "Додати зворотне посилання в deps у заблокованого тікета.", None),
    "dep_cycle": ("error", "Цикл залежностей", "Тікети залежать одне від одного по колу.",
                  "Розірвати цикл у deps.", None),
    "parent_cycle": ("error", "Цикл parent", "Ланцюжок parent замикається сам на себе.",
                     "Розірвати цикл у полі parent.", None),
    "legacy_epic": ("warning", "Старий формат тікетів (epic:)", "Шапка має epic замість parent.",
                    "Виправляється автоматично: запустіть команду в теці проєкту.", MIGRATE_FIX),
    "legacy_group": ("warning", "Синтетична група без шапки",
                     "index.md теки епіка не має канонічної шапки (id, kind: group, status).",
                     "Виправляється автоматично: запустіть команду в теці проєкту.", MIGRATE_FIX),
    "id_filename_mismatch": ("warning", "Ім'я файла не за id", "Ім'я файла не починається з id тікета.",
                             "Перейменувати файл (git mv) за id.", None),
    "symlink_skipped": ("warning", "Символічне посилання пропущено", "Симлінки в теці тікетів не читаються.",
                        "Замінити симлінк справжнім файлом.", None),
    "bad_tickets_dir": ("warning", "Тека тікетів у маркері некоректна",
                        "tickets_dir у .myvector.toml виходить за межі проєкту або не існує.",
                        "Виправити tickets_dir у .myvector.toml.", None),
    "not_ticket": ("warning", "Файл не тікет", "Файл у теці тікетів без шапки або id.",
                   "Додати шапку за каноном або винести файл із теки.", "myvec ticket migrate --brief"),
    "no_goal": ("warning", "Немає розділу «Мета»", "Не знайдено розпізнаного заголовка мети тікета.",
                "Додати розділ «За один захід».", None),
    "mixed_status_form": ("warning", "Змішані форми статусу", "Статуси частково емодзі, частково ключі.",
                          "Звести до однієї форми.", None),
    "leftover_todo": ("warning", "Завдання поза каноном", "Поза текою тікетів лишилися файли із завданнями.",
                      "Перенести завдання в тікети й залишити файл-посилання.", "myvec ticket migrate --check"),
    "legacy_script": ("warning", "Скрипт зі старими полями", "Скрипт проєкту згадує тікети й поля epic/estimate.",
                      "Замінити на myvec ticket check / index.", None),
    "process_outdated": ("warning", "Процес проєкту застарів",
                         "Конвенцію (агенти, правила, шаблони) розгорнуто старішою версією myvec, ніж встановлена.",
                         f"Оновити процес за новими шаблонами: у сесії проєкту сказати «{UPDATE_PHRASE}» або "
                         f"у теці проєкту переглянути план і diff командою, а записати - {UPDATE_WRITE}.", UPDATE_CMD),
    "search_rule_missing": ("warning", "Немає правила пошуку",
                            "У проєкті є .claude/agents, але немає .claude/rules/search.md і в CLAUDE.md не згадано myvec search: "
                            "агенти шукатимуть grep-ом.",
                            f"Переглянути план командою в теці проєкту, а додати лише файл правила - {ADOPT_WRITE}.", ADOPT_CMD),
    "search_rule_outdated": ("warning", "Правило пошуку застаріло",
                             "Файл .claude/rules/search.md збігається зі старою версією шаблону myvec (без правок власника), "
                             "а нова вже є.",
                             f"Переглянути план командою в теці проєкту, а оновити лише файл правила - {ADOPT_WRITE}.", ADOPT_CMD),
    "foreign_search_hook": ("warning", "Чужий пошуковий хук",
                            "У налаштуваннях Claude Code є хук PreToolUse зі сторонньою пошуковою утилітою (graphify): "
                            "він суперечить правилу «спершу myvec search».",
                            "Прибрати хук із .claude/settings.json (settings.local.json) вручну.", None),
    "graphify_leftover": ("health", "Залишок graphify-out",
                          "Тека graphify-out/ є, а команди graphify немає: граф застарів, Graphify не використовується.",
                          "Рішення власника: видалити теку (або встановити graphify, якщо він потрібен).", None),
    "too_big": ("health", "Завеликі тікети (розмір L)", "Тікет розміру L надто великий для одного заходу.",
                "Розбити на підтікети.", None),
    "no_ac": ("health", "Нема Acceptance criteria", "У тікеті немає чекбоксів критеріїв приймання.",
              "Дописати Acceptance criteria чекбоксами.", None),
    "stale": ("health", "Давно не оновлювався", "Тікет у роботі чи до роботи давно не змінювався.",
              "Оновити, заморозити або скасувати.", None),
    "no_session": ("health", "В роботі без сесії", "Статус in_progress, але живої сесії немає.",
                   "Запустити сесію coder або повернути статус.", None),
    "blocked_ready": ("health", "Можна розблокувати", "Усі залежності виконані, а тікет досі заблоковано.",
                      "Оркестратор змінює статус і запускає myvec ticket index.", None),
    "dep_cancelled": ("health", "Залежить від скасованого", "Одна із залежностей скасована.",
                      "Виправити deps або скасувати/перепланувати тікет.", None),
}


_PROCESS_CACHE: dict = {}
_PROCESS_CACHE_MAX = 256


def _state_issues(root: str) -> list[dict]:
    path = os.path.join(root, PROCESS_STATE)
    if not os.path.lexists(path):   # before importing myvec.scaffold (~27 ms)
        return []
    from myvec import scaffold
    from myvec.scaffold import state

    st = os.lstat(path)
    key = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size, scaffold.PROCESS_VERSION)
    hit = _PROCESS_CACHE.get(root)
    if hit is not None and hit[0] == key:
        return [dict(i) for i in hit[1]]
    r = state.read(root)
    issues: list[dict] = []
    if r.get("status") == "outdated" and isinstance(r.get("version"), int) and isinstance(r.get("current"), int):
        issues.append({"severity": "warning", "code": "process_outdated", "ticket_id": None, "path": PROCESS_STATE,
                       "message": f"процес застарів v{r['version']} → v{r['current']}"})
    if len(_PROCESS_CACHE) >= _PROCESS_CACHE_MAX:
        _PROCESS_CACHE.clear()
    _PROCESS_CACHE[root] = (key, issues)
    return [dict(i) for i in issues]


_ADOPT_CACHE: dict = {}
_ADOPT_MAX_BYTES = 256 * 1024
_ADOPT_MAX_SUBDIRS = 64


def _sig(path: str):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _bases(root: str) -> list[tuple[str, str]]:
    """[(relative prefix, absolute dir)]: the root and its direct git sub-repos (the marker may sit a level above the repo)."""
    out = [("", root)]
    n = 0
    try:
        with os.scandir(root) as it:
            names = sorted(e.name for e in it if not e.name.startswith(".") and e.is_dir(follow_symlinks=False))
    except OSError:
        return out
    for name in names:
        if n >= _ADOPT_MAX_SUBDIRS:
            break
        n += 1
        d = os.path.join(root, name)
        if os.path.lexists(os.path.join(d, ".git")):
            out.append((name + "/", d))
    return out


def _read_small(path: str) -> str | None:
    try:
        if os.lstat(path).st_size > _ADOPT_MAX_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return f.read(_ADOPT_MAX_BYTES + 1).decode("utf8", "replace")
    except OSError:
        return None


def _sha_small(path: str) -> str | None:
    """sha256 of the raw bytes of a small regular file (no symlink), else None."""
    try:
        if os.lstat(path).st_size > _ADOPT_MAX_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return hashlib.sha256(f.read(_ADOPT_MAX_BYTES + 1)).hexdigest()
    except OSError:
        return None


def _is_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _graphify_hook(settings_text: str) -> bool:
    try:
        data = json.loads(settings_text)
    except (ValueError, RecursionError):
        return False
    pre = data.get("hooks", {}).get("PreToolUse") if isinstance(data, dict) and isinstance(data.get("hooks"), dict) else None
    for entry in pre if isinstance(pre, list) else []:
        for h in (entry.get("hooks") if isinstance(entry, dict) and isinstance(entry.get("hooks"), list) else []):
            cmd = h.get("command") if isinstance(h, dict) else None
            if isinstance(cmd, str) and "graphify" in cmd.lower():
                return True
    return False


def _adoption_for(prefix: str, base: str, has_graphify: bool) -> list[dict]:
    out: list[dict] = []
    claude = os.path.join(base, ".claude")
    if _is_dir(os.path.join(claude, "agents")) and not os.path.lexists(os.path.join(base, PROCESS_STATE)) \
            and not os.path.lexists(os.path.join(base, SEARCH_RULE)):
        mentions = False
        for name in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md")):
            p = os.path.join(base, name)
            if os.path.lexists(p):
                t = _read_small(p)
                if t is None or "myvec search" in t:   # unreadable or too big: do not guess
                    mentions = True
        if not mentions:
            out.append({"severity": "warning", "code": "search_rule_missing", "ticket_id": None, "path": prefix + ".claude",
                        "message": "агенти є, правила пошуку немає" + (
                            f"; команда для цього репо: {ADOPT_WRITE} --root {shlex.quote(prefix.rstrip('/'))}" if prefix else "")})
    elif not os.path.lexists(os.path.join(base, PROCESS_STATE)) and _is_dir(claude) \
            and _is_dir(os.path.join(claude, "rules")) and _sha_small(os.path.join(base, SEARCH_RULE)) in SEARCH_RULE_OLD:
        out.append({"severity": "warning", "code": "search_rule_outdated", "ticket_id": None,
                    "path": prefix + SEARCH_RULE,
                    "message": "правило пошуку збігається зі старою версією myvec" + (
                        f"; команда для цього репо: {ADOPT_WRITE} --root {shlex.quote(prefix.rstrip('/'))}" if prefix else "")})
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(claude, name)
        t = _read_small(p) if os.path.lexists(p) else None
        if t is not None and _graphify_hook(t):
            out.append({"severity": "warning", "code": "foreign_search_hook", "ticket_id": None,
                        "path": f"{prefix}.claude/{name}", "message": "хук PreToolUse зі сторонньою пошуковою утилітою"})
    if _is_dir(os.path.join(base, "graphify-out")) and not has_graphify:
        out.append({"severity": "health", "code": "graphify_leftover", "ticket_id": None, "path": prefix + "graphify-out",
                    "message": "graphify-out/ без CLI: застарілий граф, Graphify не використовується"})
    return out


def _adoption_issues(root: str) -> list[dict]:
    """search_rule_missing / foreign_search_hook / graphify_leftover (E8-12, ADR-041): stdlib only, no scaffold import."""
    bases = _bases(root)
    has_graphify = shutil.which("graphify") is not None
    sig = []
    for prefix, base in bases:
        for rel in (".claude/agents", SEARCH_RULE, PROCESS_STATE, "CLAUDE.md", ".claude/CLAUDE.md",
                    ".claude/settings.json", ".claude/settings.local.json", "graphify-out"):
            sig.append(_sig(os.path.join(base, rel)))
    key = (tuple(b for _, b in bases), tuple(sig), has_graphify)
    hit = _ADOPT_CACHE.get(root)
    if hit is not None and hit[0] == key:
        return [dict(i) for i in hit[1]]
    issues = [i for prefix, base in bases for i in _adoption_for(prefix, base, has_graphify)]
    if len(_ADOPT_CACHE) >= _PROCESS_CACHE_MAX:
        _ADOPT_CACHE.clear()
    _ADOPT_CACHE[root] = (key, issues)
    return [dict(i) for i in issues]


def process_issues(root: str | None) -> list[dict]:
    """Проблеми процесу проєкту: «процес застарів vN → vM» (E0-10, ADR-036; лише при `scaffold.state.read(root).status ==
    outdated`) і правило пошуку (E8-12, ADR-041). Обчислюється при читанні, без схеми; кеш за відбитками прочитаних
    шляхів. Будь-яка помилка -> [] (по кожній частині окремо)."""
    if not root:
        return []
    root = str(root)
    out: list[dict] = []
    for part in (_state_issues, _adoption_issues):
        try:
            out.extend(part(root))
        except Exception:  # noqa: BLE001 - a broken file is never a reason to fail the problems view
            pass
    return out


def codes() -> set[str]:
    return set(_P)


def describe(code: str, severity: str | None = None) -> dict:
    """Опис коду; невідомий код -> фолбек із самим кодом (важливість береться з даних, якщо передана)."""
    if code in _P:
        sev, title, explain, fix, cmd = _P[code]
    else:
        sev, title, explain, fix, cmd = (severity if severity in SEVERITIES else "warning"), code, "", "", None
    return {"code": code, "severity": sev, "title": title, "explain": explain, "fix": fix, "cmd": cmd,
            "legacy": code in LEGACY_CODES}


def group(issues: list[dict], root: str | None = None) -> dict:
    """Проблеми, згруповані за кодом: {groups: [...], counts: {error, warning, health}, total, migration_branch, merge_cmd}.

    `issues` - dict-и {severity, code, ticket_id, path, message}. Тіла тікетів не потрібні: лише id, шляхи й
    повідомлення про проблеми рівня проєкту (без ticket_id). `root` - корінь проєкту (для `attach_branch`)."""
    by: dict[str, dict] = {}
    for i in issues:
        g = by.get(i["code"])
        if g is None:
            g = by[i["code"]] = {**describe(i["code"], i.get("severity")), "n": 0, "tickets": [], "project_items": []}
        g["n"] += 1
        if i.get("ticket_id"):
            if i["ticket_id"] not in g["tickets"]:
                g["tickets"].append(i["ticket_id"])
        else:
            g["project_items"].append({"path": i.get("path") or "", "message": i.get("message") or ""})
    order = {s: k for k, s in enumerate(SEVERITIES)}
    groups = sorted(by.values(), key=lambda g: (order[g["severity"]], -g["n"], g["code"]))
    counts = {s: sum(g["n"] for g in groups if g["severity"] == s) for s in SEVERITIES}
    return {"groups": groups, "counts": counts, "total": sum(counts.values()), "migration_branch": None, "merge_cmd": None, "root": root}


GIT_TIMEOUT = 2.0


def attach_branch(d: dict) -> dict:
    """Додає `migration_branch` і `merge_cmd`, якщо є legacy-проблеми й гілка міграції існує (лише читання refs).
    Викликати поза db_lock: git із таймаутом; помилка чи таймаут - без підказки."""
    root = d.get("root")
    if not root or not any(g["legacy"] for g in d["groups"]):
        return d
    try:
        r = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{MIGRATION_BRANCH}"],
                           capture_output=True, env=gitutil._env(), timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return d
    if r.returncode == 0:
        d["migration_branch"] = MIGRATION_BRANCH
        d["merge_cmd"] = f"git -C {shlex.quote(str(root))} merge {MIGRATION_BRANCH}"
    return d
