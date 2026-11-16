"""Inbox правок з UI (E3-01, ADR-019): inbox.py, ендпоінти демона, comments у body_sections, tracker.js."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import do_sync
from myvec import daemon, inbox, tickets, ticketdb
from test_ticketdb import add_tickets
from test_tracker_js import needs_node, run

NOW = datetime(2026, 11, 10, 15, 31, 0, tzinfo=timezone.utc)
H = {"X-CSRF-Token": "t"}
URL = "/api/tickets/acme/ticket/E9-01/inbox"


def tdir(tmp_path):
    root = tmp_path / "proj"
    (root / "docs/todo").mkdir(parents=True)
    return root


def test_write_format_and_gitignore(tmp_path):
    root = tdir(tmp_path)
    it = inbox.write(root, "docs/todo", "E9-01", "Привіт\nдруга", None, node="devbox", now=NOW)
    p = root / "docs/todo/inbox" / it["file"]
    assert it["file"] == "2026-11-10-E9-01-153100.md"
    fm, body = tickets.parse_frontmatter(p.read_text())
    assert fm == {"ticket": "E9-01", "kind": "comment", "created": "2026-11-10T15:31:00+00:00", "node": "devbox", "source": "ui"}
    assert body.strip() == "Привіт\nдруга"
    assert (root / "docs/todo/inbox/.gitignore").read_text() == "*\n"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert not list((root / "docs/todo/inbox").glob(".*.tmp"))


def test_collision_suffix_and_status_kind(tmp_path):
    root = tdir(tmp_path)
    a = inbox.write(root, "docs/todo", "E9-01", "a", None, now=NOW)
    b = inbox.write(root, "docs/todo", "E9-01", "", "review", now=NOW)
    assert b["file"] == "2026-11-10-E9-01-153100-2.md"
    assert (a["kind"], b["kind"], b["requested_status"]) == ("comment", "status", "review")
    assert [i["file"] for i in inbox.pending(root, "docs/todo", "E9-01")] == [a["file"], b["file"]]


@pytest.mark.parametrize("tid,text,status", [
    ("../x", "t", None), ("a/b", "t", None), ("", "t", None), ("E9-01", "x" * 4001, None),
    ("E9-01", "bad\x00ctl", None), ("E9-01", "t", "nope"), ("E9-01", "  ", None), ("E9-01", 5, None)])
def test_validation(tmp_path, tid, text, status):
    root = tdir(tmp_path)
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", tid, text, status, now=NOW)
    assert e.value.code == "bad_request"
    assert not (root / "docs/todo/inbox").exists()


def test_secrets(tmp_path):
    root = tdir(tmp_path)
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", "E9-01", "key AKIAABCDEFGHIJKLMNOP here", None, now=NOW)
    assert e.value.code == "secret" and not (root / "docs/todo/inbox").exists()
    it = inbox.write(root, "docs/todo", "E9-01", 'password = "hunter2hunter2"', None, now=NOW)
    assert "hunter2" not in it["text"] and "•••" in it["text"]


def test_unsafe_paths(tmp_path):
    root = tdir(tmp_path)
    outside = tmp_path / "out"
    outside.mkdir()
    (root / "docs/todo/inbox").symlink_to(outside)
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", "E9-01", "t", None, now=NOW)
    assert e.value.code == "unsafe_path" and not os.listdir(outside)
    assert inbox.pending(root, "docs/todo") == []
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "../out", "E9-01", "t", None, now=NOW)
    assert e.value.code == "unsafe_path"


def test_queue_limit(tmp_path):
    root = tdir(tmp_path)
    d = root / "docs/todo/inbox"
    d.mkdir()
    for i in range(inbox.QUEUE_LIMIT):
        (d / f"f{i}.md").write_text("x")
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", "E9-01", "t", None, now=NOW)
    assert e.value.code == "full"


def test_pending_filters_by_frontmatter_and_skips_broken(tmp_path):
    root = tdir(tmp_path)
    inbox.write(root, "docs/todo", "E9-01", "one", None, now=NOW)
    d = root / "docs/todo/inbox"
    (d / "2026-11-10-E9-01-other.md").write_text("---\nticket: E9-02\nkind: comment\ncreated: 2026-11-10T16:00:00+00:00\n---\n\ntwo\n")
    (d / "broken.md").write_text("no header")
    (d / "big.md").write_text("---\nticket: E9-01\n---\n" + "x" * 70000)
    (d / "link.md").symlink_to(d / "broken.md")
    assert [i["text"] for i in inbox.pending(root, "docs/todo", "E9-01")] == ["one"]
    assert [i["text"] for i in inbox.pending(root, "docs/todo")] == ["one", "two"]


def test_fingerprint_ignores_inbox(tmp_path):
    root = tdir(tmp_path)
    (root / "docs/todo/T.md").write_text("x")
    a = ticketdb.fingerprint(root, "docs/todo")
    inbox.write(root, "docs/todo", "E9-01", "t", None, now=NOW)
    assert ticketdb.fingerprint(root, "docs/todo") == a


def test_body_sections_comments():
    body = "## Comments\n\n### 2026-11-10 15:31 Ти (ui)\nПерший\nрядок\n<!-- x -->\n\n### 2026-11-09 orch\nДругий\n\n## Log\n\n### 2026-11-10 coder\nok\n"
    s = tickets.body_sections(body)
    assert s["comments"] == [
        {"date": "2026-11-10", "time": "15:31", "author": "Ти", "source": "ui", "text": "Перший\nрядок"},
        {"date": "2026-11-09", "time": None, "author": "orch", "source": "", "text": "Другий"}]
    assert [e["agent"] for e in s["log"]] == ["coder"]
    assert tickets.body_sections("# x")["comments"] == []


# ------------------------------------------------------------------ daemon

@pytest.fixture
def client(ws):
    add_tickets(ws)
    do_sync(ws)
    st = daemon.DaemonState(7878, "t")
    st.refresh()
    yield TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878"), ws, st
    st.ticket_executor.shutdown(wait=True)
    st.executor.shutdown(wait=True)
    st.close()


def test_api_post_get_roundtrip_and_no_project_edits(client):
    c, ws, st = client
    before = {p: p.read_bytes() for p in (ws / "docs/todo").rglob("*.md")}
    r = c.post(URL, json={"text": "qa-marker-quokka"}, headers=H)
    assert r.status_code == 201 and r.json()["item"]["kind"] == "comment"
    r = c.post(URL, json={"text": "", "status": "review"}, headers=H)
    assert r.status_code == 201 and r.json()["item"]["requested_status"] == "review"
    items = c.get(URL).json()["items"]
    assert [i["kind"] for i in items] == ["comment", "status"]
    assert c.get("/api/tickets/acme/ticket/E9-02/inbox").json() == {"items": []}
    after = {p: p.read_bytes() for p in (ws / "docs/todo").rglob("*.md") if "inbox" not in p.parts}
    assert after == before
    assert (ws / "docs/todo/inbox/.gitignore").read_text() == "*\n"
    ev = [e for e in st.bus.recent() if e["type"] == "inbox_write"]
    assert len(ev) == 2 and "qa-marker" not in json.dumps(ev) and ev[0]["text_len"] == len("qa-marker-quokka")


def test_api_errors(client):
    c, ws, _ = client
    assert c.post(URL, json={"text": "x"}).status_code == 403
    assert c.post(URL, json={"text": "x"}, headers={**H, "X-CSRF-Token": "bad"}).status_code == 403
    assert c.post(URL, json={"text": "x"}, headers={**H, "Origin": "http://evil.com"}).status_code == 403
    assert c.post("/api/tickets/acme/ticket/NOPE/inbox", json={"text": "x"}, headers=H).status_code == 404
    assert c.post("/api/tickets/zzz/ticket/E9-01/inbox", json={"text": "x"}, headers=H).status_code == 404
    assert c.get("/api/tickets/zzz/ticket/E9-01/inbox").status_code == 404
    assert c.post(URL, json={"text": "x" * 4001}, headers=H).status_code == 400
    assert c.post(URL, json={"text": "x", "status": "zzz"}, headers=H).status_code == 400
    assert c.post(URL, json={"text": "token AKIAABCDEFGHIJKLMNOP"}, headers=H).status_code == 400
    assert c.post(URL, content=b"x" * 40000, headers=H).status_code == 413
    assert not (ws / "docs/todo/inbox").exists()


def test_api_queue_full_409(client):
    c, ws, _ = client
    for i in range(inbox.QUEUE_LIMIT):
        assert c.post(URL, json={"text": f"n{i}"}, headers=H).status_code == 201
    assert c.post(URL, json={"text": "more"}, headers=H).status_code == 409


def test_fs_event_in_inbox_does_not_trigger(client, monkeypatch):
    _, ws, st = client
    notified, dirty = [], []
    monkeypatch.setattr(st.sched, "notify", lambda k: notified.append(k))
    st._on_fs_event("acme", str(ws / "docs/todo/inbox/2026-x.md"))
    assert notified == [] and "acme" not in st._tickets_dirty
    st._on_fs_event("acme", str(ws / "docs/todo/E9-x/E9-01-zebra.md"))
    assert notified == ["acme"] and "acme" in st._tickets_dirty


# ------------------------------------------------------------------ tracker.js

@needs_node
def test_comments_block():
    r = run("""
      const ui = {project: 'acme', ticket: 'E1-01', busy: false, draft: {text: 'чернетка', status: 'review'}};
      const done = [{date: '2026-11-10', time: '18:05', author: 'Ти', source: 'ui', text: 'оброблений <b>x</b>'}];
      const local = [{kind: 'comment', text: 'локальний', created: '2026-11-11T15:31:00+02:00'},
                     {kind: 'comment', text: 'вже в тілі', created: '2026-11-11T15:00:00+02:00'}];
      const sent = [{kind: 'status', requested_status: 'review', text: '', created: '2026-11-11T15:31:00+02:00'}];
      const h = t.commentsBlock(done.concat([{date: '2026-11-11', time: null, author: 'orch', text: 'вже в тілі'}]), sent, local, ui).toString();
      const empty = t.commentsBlock([], [], [], {project: 'p', ticket: 'x', busy: true}).toString();
      return {h, empty};""")
    h = r["h"]
    assert "&lt;b&gt;x&lt;/b&gt;" in h and "<b>x</b>" not in h
    assert "10.11 18:05" in h and "11.11 15:31" in h and "11.11" in h
    assert h.count('class="bdg t-success"') == 3        # 2 з тіла + 1 локальний; «вже в тілі» сховано
    assert 'class="cmt pending"' in h and "надіслано" in h and "запит статусу" in h and "На ревю" in h
    assert 'data-act="inboxsend"' in h and "не змінювати" in h and "йдуть оркестратору через inbox" in h
    assert "чернетка" in h and "selected" in h
    assert "disabled" in r["empty"] and "оброблено" not in r["empty"] and "Коментар до тікета" in r["empty"]


# ------------------------------------------------------------------ review fixes (безпека)

def test_tickets_dir_symlink_outside_root(tmp_path):
    root = tmp_path / "proj"
    out = tmp_path / "out"
    root.mkdir()
    out.mkdir()
    (root / "docs").mkdir()
    (root / "docs/todo").symlink_to(out)
    assert inbox.inbox_dir(root, "docs/todo") is None
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", "E9-01", "t", None, now=NOW)
    assert e.value.code == "unsafe_path" and not list(out.iterdir())


def test_planted_symlinks_do_not_write_through(tmp_path):
    root = tdir(tmp_path)
    d = root / "docs/todo/inbox"
    d.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    for name in (".gitignore", "2026-11-10-E9-01-153100.md", ".2026-11-10-E9-01-153100.md.tmp"):
        (d / name).symlink_to(victim)
    it = inbox.write(root, "docs/todo", "E9-01", "новий", None, now=NOW)
    assert victim.read_text() == "ORIGINAL"
    assert it["file"] == "2026-11-10-E9-01-153100-2.md" and it["text"] == "новий"


def test_crlf_normalised(tmp_path):
    root = tdir(tmp_path)
    it = inbox.write(root, "docs/todo", "E9-01", "a\r\nb", None, now=NOW)
    assert it["text"] == "a\nb"


def test_api_unsafe_inbox_is_400_and_chunked_413(client):
    c, ws, _ = client
    out = ws.parent / "outside"
    out.mkdir()
    (ws / "docs/todo/inbox").symlink_to(out)
    assert c.post(URL, json={"text": "x"}, headers=H).status_code == 400
    assert not list(out.iterdir())
    (ws / "docs/todo/inbox").unlink()

    def gen():
        for _ in range(5):
            yield b"x" * 10000
    assert c.post(URL, content=gen(), headers=H).status_code == 413


def test_api_get_masks_secret_in_hand_placed_file(client):
    c, ws, _ = client
    d = ws / "docs/todo/inbox"
    d.mkdir()
    (d / "m.md").write_text('---\nticket: E9-01\nkind: comment\ncreated: 2026-11-10T10:00:00+00:00\n---\n\npassword = "hunter2hunter2"\n')
    t = c.get(URL).json()["items"][0]["text"]
    assert "hunter2" not in t and "•••" in t


def test_inbox_not_indexed_and_text_not_in_events_file(ws, tmp_path):
    add_tickets(ws)
    _, db = do_sync(ws)
    st = daemon.DaemonState(7878, "t")
    try:
        st.refresh()
        c = TestClient(daemon.create_app(st), base_url="http://127.0.0.1:7878")
        assert c.post(URL, json={"text": "zxqmarker-unique-word"}, headers=H).status_code == 201
        _, db = do_sync(ws)
        paths = [r[0] for r in db.execute("SELECT path FROM file_state")]
        assert paths and not [p for p in paths if "inbox" in p]
        assert db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'zxqmarker'").fetchone()[0] == 0
        ev = st.bus.path
        text = ev.read_text() if ev and ev.exists() else ""
        assert "inbox_write" in text and "zxqmarker" not in text
    finally:
        st.ticket_executor.shutdown(wait=True)
        st.executor.shutdown(wait=True)
        st.close()


# --- E0-08: kind lesson (ADR-027) ---

def test_lesson_file_fields_name_and_read_item(tmp_path):
    root = tdir(tmp_path)
    it = inbox.write(root, "docs/todo", "lesson", "## T\n\nправило", kind="lesson", source="cli", origin="project-a",
                     node="n", now=NOW)
    assert it["file"] == "2026-11-10-lesson-153100.md"
    fm, _ = tickets.parse_frontmatter((root / "docs/todo/inbox" / it["file"]).read_text())
    assert fm == {"ticket": "lesson", "kind": "lesson", "from": "project-a", "created": "2026-11-10T15:31:00+00:00",
                  "node": "n", "source": "cli"}
    (p,) = inbox.pending(root, "docs/todo")
    assert p["kind"] == "lesson" and p["from"] == "project-a" and p["text"].endswith("правило")
    again = inbox.write(root, "docs/todo", "lesson", "x", kind="lesson", source="cli", origin="a", now=NOW)
    assert again["file"] == "2026-11-10-lesson-153100-2.md"


def test_lesson_from_invalid_in_file_becomes_empty(tmp_path):
    root = tdir(tmp_path)
    d = root / "docs/todo/inbox"
    d.mkdir()
    (d / "a.md").write_text("---\nticket: lesson\nkind: lesson\nfrom: ../evil\n---\ntxt\n")
    assert inbox.pending(root, "docs/todo")[0]["from"] == ""


@pytest.mark.parametrize("kw", [
    dict(requested_status="done", origin="project-a"), dict(origin=None), dict(origin="bad/x"), dict(origin=""),
    dict(origin="project-a", ticket="E9-01")])
def test_lesson_validation(tmp_path, kw):
    root = tdir(tmp_path)
    tid = kw.pop("ticket", "lesson")
    status = kw.pop("requested_status", None)
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", tid, "t", status, kind="lesson", source="cli", **kw)
    assert e.value.code == "bad_request" and not (root / "docs/todo/inbox").exists()


def test_unknown_kind_and_source_rejected(tmp_path):
    root = tdir(tmp_path)
    for kw in (dict(kind="nope"), dict(source="web")):
        with pytest.raises(inbox.InboxError) as e:
            inbox.write(root, "docs/todo", "E9-01", "t", **kw)
        assert e.value.code == "bad_request"


@pytest.mark.parametrize("text", ["see /home/bob/x", "see /Users/al/x", "~/MyVector/foo", "a MyVector/vault/b",
    "див. /home/bob", "це /Users/Bob.", "~/myvector/vault/notes", "myvector/vault/x", "$HOME/MyVector",
    "${HOME}/MyVector/a", "MyVector\\vault\\x"])
def test_lesson_private_paths_refused(tmp_path, text):
    root = tdir(tmp_path)
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", "lesson", text, kind="lesson", source="cli", origin="project-a")
    assert e.value.code == "private" and not (root / "docs/todo/inbox").exists()


def test_comment_may_still_contain_home_path(tmp_path):
    root = tdir(tmp_path)
    inbox.write(root, "docs/todo", "E9-01", "see /home/bob/x", now=NOW)


def test_lesson_hard_secret_and_literal_mask(tmp_path):
    root = tdir(tmp_path)
    with pytest.raises(inbox.InboxError) as e:
        inbox.write(root, "docs/todo", "lesson", "k AKIAIOSFODNN7EXAMPLE", kind="lesson", source="cli", origin="f")
    assert e.value.code == "secret"
    it = inbox.write(root, "docs/todo", "lesson", 'api_key = "sup3rS3cretValue99xyz"', kind="lesson",
                     source="cli", origin="f", now=NOW)
    txt = (root / "docs/todo/inbox" / it["file"]).read_text()
    assert "sup3rS3cretValue99xyz" not in txt and "•••" in txt


def test_comment_and_status_format_byte_identical(tmp_path):
    root = tdir(tmp_path)
    a = inbox.write(root, "docs/todo", "E9-01", "Привіт", None, node="n", now=NOW)
    b = inbox.write(root, "docs/todo", "E9-01", "", "review", node="n", now=NOW)
    rd = lambda it: (root / "docs/todo/inbox" / it["file"]).read_text()
    assert rd(a) == ("---\nticket: E9-01\nkind: comment\ncreated: 2026-11-10T15:31:00+00:00\nnode: n\nsource: ui\n"
                     "---\n\nПривіт\n")
    assert rd(b) == ("---\nticket: E9-01\nkind: status\nrequested_status: review\ncreated: 2026-11-10T15:31:00+00:00\n"
                     "node: n\nsource: ui\n---\n\n\n")
