"""Inbox правок з UI (ADR-019): `<root>/<tickets_dir>/inbox/<дата>-<id>-<HHMMSS>[-n].md`.

Лише stdlib + `tickets` (його імпортуватиме хук E3-02): без typer, sqlite-vec і httpx.
Нода пише лише файли inbox і `inbox/.gitignore`; тікет, INDEX.md і .gitignore проєкту не чіпає.
Текст коментаря нікуди більше не пишеться (ні в логи, ні в події).
"""
from __future__ import annotations

import os
import re
import socket
from datetime import datetime
from pathlib import Path

from myvec import tickets

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_TEXT = 4000
MAX_FILE = 64 * 1024
MAX_READ = 200
LESSON_TICKET = "lesson"      # псевдо-id тікета для уроків (ADR-027)
KINDS = ("comment", "status", "lesson")
_PRIVATE = re.compile(r"(?i)(?:/home/[^/\s]+|/Users/[^/\s]+|(?:~|\$HOME\b|\$\{HOME\})[/\\]\S*myvector|myvector[/\\]vault)")
QUEUE_LIMIT = 50
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # \t \n \r дозволені


class InboxError(Exception):
    """code: bad_request | secret | private | full | unsafe_path."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def inbox_dir(root, tickets_dir: str) -> Path | None:
    """`root/tickets_dir/inbox`; None, якщо тека тікетів поза root або `inbox` - симлінк."""
    base = Path(root) / tickets_dir
    try:
        if not base.resolve().is_relative_to(Path(root).resolve()):
            return None
    except OSError:
        return None
    d = base / "inbox"
    if d.is_symlink():
        return None
    return d


def _node() -> str:
    return (socket.gethostname() or "node").split(".")[0]


def render(ticket_id: str, text: str, requested_status: str | None, node: str, created: str,
           kind: str | None = None, source: str = "ui", origin: str | None = None) -> str:
    kind = kind or ("status" if requested_status else "comment")
    head = ["---", f"ticket: {ticket_id}", f"kind: {kind}"]
    if requested_status:
        head.append(f"requested_status: {requested_status}")
    if kind == "lesson":
        head.append(f"from: {origin}")
    head += [f"created: {created}", f"node: {node}", f"source: {source}", "---", "", text.rstrip(), ""]
    return "\n".join(head)


def _read_item(path: Path) -> dict | None:
    try:
        st = path.lstat()
        if not path.is_file() or path.is_symlink() or st.st_size > MAX_FILE:
            return None
        fm = tickets.parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tickets.FrontmatterError):
        return None
    if fm is None:
        return None
    data, body = fm
    ticket = data.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        return None
    kind = data.get("kind") if data.get("kind") in KINDS else "comment"
    item = {"file": path.name, "ticket": ticket, "kind": kind, "requested_status": data.get("requested_status"),
            "created": str(data.get("created") or ""), "node": str(data.get("node") or ""), "text": (body or "").strip()}
    if kind == "lesson":
        origin = data.get("from")
        item["from"] = origin if isinstance(origin, str) and ID_RE.match(origin) else ""
    return item


def pending(root, tickets_dir: str, ticket_id: str | None = None) -> list[dict]:
    """Файли inbox (читання теки напряму, без БД); фільтр за `ticket` з frontmatter."""
    d = inbox_dir(root, tickets_dir)
    if d is None or not d.is_dir():
        return []
    out: list[dict] = []
    try:
        with os.scandir(d) as it:
            names = sorted(e.name for e in it if e.name.endswith(".md") and not e.name.startswith(".") and not e.is_symlink())
    except OSError:
        return []
    for name in names[:MAX_READ]:
        item = _read_item(d / name)
        if item is not None and (ticket_id is None or item["ticket"] == ticket_id):
            out.append(item)
    out.sort(key=lambda i: (i["created"], len(i["file"]), i["file"]))
    return out


def _validate(ticket_id, text, requested_status) -> str:
    if not isinstance(ticket_id, str) or not ID_RE.match(ticket_id):
        raise InboxError("bad_request", "некоректний id тікета")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise InboxError("bad_request", "text має бути рядком")
    if len(text) > MAX_TEXT:
        raise InboxError("bad_request", f"текст довший за {MAX_TEXT} символів")
    if _CTRL.search(text):
        raise InboxError("bad_request", "текст містить керівні символи")
    if requested_status is not None and requested_status not in tickets.STATUS_KEYS:
        raise InboxError("bad_request", "невідомий статус")
    if not text.strip() and requested_status is None:
        raise InboxError("bad_request", "порожній коментар")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _create(name: str, data: str, dfd: int) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


def write(root, tickets_dir: str, ticket_id: str, text: str | None, requested_status: str | None = None,
          node: str | None = None, now: datetime | None = None, *, kind: str = "comment",
          source: str = "ui", origin: str | None = None) -> dict:
    if kind not in KINDS:
        raise InboxError("bad_request", "невідомий вид")
    if source not in ("ui", "cli"):
        raise InboxError("bad_request", "невідоме джерело")
    if kind == "lesson":
        if requested_status is not None:
            raise InboxError("bad_request", "урок не може міняти статус")
        if ticket_id != LESSON_TICKET:
            raise InboxError("bad_request", "некоректний id для уроку")
        if not isinstance(origin, str) or not ID_RE.match(origin):
            raise InboxError("bad_request", "некоректне ім'я проєкту-джерела")
    text = _validate(ticket_id, text, requested_status)
    from myvec import filters

    if filters.has_hard_secret(text):
        raise InboxError("secret", "схоже на ключ/токен - не записую")
    if kind == "lesson" and _PRIVATE.search(text):
        raise InboxError("private", "урок містить приватний шлях (домашній каталог чи vault) - узагальни")
    text, _ = filters.mask_secrets(text)
    d = inbox_dir(root, tickets_dir)
    if d is None:
        raise InboxError("unsafe_path", "небезпечний шлях inbox")
    try:
        d.mkdir(exist_ok=True)
    except OSError as e:
        raise InboxError("unsafe_path", f"не вдалося створити inbox: {e.strerror}") from e
    try:   # усі подальші операції - відносно дескриптора теки: підміна inbox симлінком після цього не діє
        dfd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise InboxError("unsafe_path", "небезпечний шлях inbox") from e
    try:
        return _write_in(dfd, d, ticket_id, text, requested_status, node, now, kind, source, origin)
    finally:
        os.close(dfd)


def _write_in(dfd: int, d: Path, ticket_id: str, text: str, requested_status: str | None,
              node: str | None, now: datetime | None, kind: str = "comment", source: str = "ui",
              origin: str | None = None) -> dict:
    try:
        _create(".gitignore", "*\n", dfd)
    except FileExistsError:
        pass
    except OSError as e:
        raise InboxError("unsafe_path", f"не вдалося записати inbox/.gitignore: {e.strerror}") from e
    with os.scandir(dfd) as it:
        if sum(1 for e in it if e.name.endswith(".md") and not e.name.startswith(".")) >= QUEUE_LIMIT:
            raise InboxError("full", f"в inbox уже {QUEUE_LIMIT} файлів - зачекай на оркестратора")
    now = now or datetime.now().astimezone()
    created = now.isoformat(timespec="seconds")
    content = render(ticket_id, text, requested_status, node or _node(), created,
                     kind if kind == "lesson" else None, source, origin)
    stem = f"{now:%Y-%m-%d}-{ticket_id}-{now:%H%M%S}"
    for n in range(1, 1000):
        name = f"{stem}.md" if n == 1 else f"{stem}-{n}.md"
        tmp = f".{name}.tmp"
        try:
            _create(tmp, content, dfd)
        except FileExistsError:
            continue
        except OSError as e:
            raise InboxError("unsafe_path", f"не вдалося записати inbox: {e.strerror}") from e
        try:
            os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as e:
            raise InboxError("unsafe_path", f"не вдалося записати inbox: {e.strerror}") from e
        finally:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        item = _read_item(d / name)
        if item is None:
            raise InboxError("unsafe_path", "файл inbox не прочитався")
        return item
    raise InboxError("full", "не вдалося підібрати ім'я файла")
