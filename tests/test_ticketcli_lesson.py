"""`myvec ticket lesson` (E0-08, ADR-027): lesson proposal into another project's inbox."""
import re

import pytest
from typer.testing import CliRunner

from myvec import config, db as dbmod, inbox, tickets
from myvec.commands import app

runner = CliRunner()
BODY = "Симптом: x\nПричина: y\nПравило: z\n"
SECRET = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "vl"
    (root / "docs/todo").mkdir(parents=True)
    (root / ".myvector.toml").write_text('name = "vector-local"\n')
    conn = dbmod.connect(vec=False)
    conn.executescript(dbmod.SCHEMA)
    conn.execute("INSERT INTO projects(path, root) VALUES('vector-local', ?)", (str(root),))
    conn.close()
    return root


def lesson(*args, text=BODY):
    return runner.invoke(app, ["ticket", "lesson", *args], input=text)


def files(root):
    d = root / "docs/todo/inbox"
    return sorted(p for p in d.glob("*.md")) if d.is_dir() else []


def test_ok_writes_lesson_file_and_prints_only_name(target):
    r = lesson("--title", "Мій урок", "--from", "project-a", "-")
    assert r.exit_code == 0, r.output
    (f,) = files(target)
    assert r.output.strip() == f.name
    assert re.match(r"^\d{4}-\d\d-\d\d-lesson-\d{6}(-\d+)?\.md$", f.name)
    fm, body = tickets.parse_frontmatter(f.read_text())
    assert (fm["ticket"], fm["kind"], fm["from"], fm["source"]) == ("lesson", "lesson", "project-a", "cli")
    assert body.strip().startswith("## Мій урок") and "Правило: z" in body
    assert "Симптом" not in r.output


def test_dash_marker_required(target):
    r = lesson("--title", "t", "--from", "project-a")
    assert r.exit_code == 2 and not files(target)


def test_title_with_newline_rejected(target):
    r = lesson("--title", "a\nb", "--from", "project-a", "-")
    assert r.exit_code == 2 and not files(target)


@pytest.mark.parametrize("title", ["", "   ", "x" * 121])
def test_bad_title_rejected(target, title):
    r = lesson("--title", title, "--from", "project-a", "-")
    assert r.exit_code == 2 and not files(target)


def test_empty_stdin_rejected(target):
    r = lesson("--title", "t", "--from", "project-a", "-", text="  \n")
    assert r.exit_code == 2 and not files(target)


def test_invalid_from_rejected(target):
    r = lesson("--title", "t", "--from", "../x", "-")
    assert r.exit_code == 2 and not files(target)


def test_unknown_target_project(target):
    r = lesson("--title", "t", "--from", "project-a", "--to", "nope", "-")
    assert r.exit_code == 2 and "nope" in r.output and not files(target)


def test_no_database_exit_2(env):
    r = lesson("--title", "t", "--from", "project-a", "-")
    assert r.exit_code == 2


def test_default_target_from_config(target):
    (config.home() / "config.toml").write_text('lessons_project = "other"\n')
    r = lesson("--title", "t", "--from", "project-a", "-")
    assert r.exit_code == 2 and "other" in r.output and not files(target)


def test_hard_secret_exit_2_nothing_written(target):
    r = lesson("--title", "t", "--from", "project-a", "-", text=f"key {SECRET}")
    assert r.exit_code == 2 and not files(target) and SECRET not in r.output


def test_private_home_path_exit_2(target):
    r = lesson("--title", "t", "--from", "project-a", "-", text="see /home/bob/proj/x")
    assert r.exit_code == 2 and "private" in r.output and not files(target)


def test_vault_path_exit_2(target):
    r = lesson("--title", "t", "--from", "project-a", "-", text="in MyVector/vault/notes.md")
    assert r.exit_code == 2 and not files(target)


def test_literal_secret_masked(target):
    r = lesson("--title", "t", "--from", "project-a", "-", text='Правило: api_key = "sup3rS3cretValue99xyz"')
    assert r.exit_code == 0, r.output
    assert "sup3rS3cretValue99xyz" not in files(target)[0].read_text()
    assert "•••" in files(target)[0].read_text()


def test_queue_full_exit_3(target, monkeypatch):
    monkeypatch.setattr(inbox, "QUEUE_LIMIT", 1)
    assert lesson("--title", "t", "--from", "project-a", "-").exit_code == 0
    r = lesson("--title", "t", "--from", "project-a", "-")
    assert r.exit_code == 3 and len(files(target)) == 1


def test_calls_inbox_write_with_lesson_kind(target, monkeypatch):
    seen = {}

    def fake(root, tdir, tid, text, *a, **kw):
        seen.update(root=root, tid=tid, kw=kw)
        return {"file": "fake.md"}

    monkeypatch.setattr(inbox, "write", fake)
    r = lesson("--title", "t", "--from", "project-a", "-")
    assert r.exit_code == 0 and r.output.strip() == "fake.md"
    assert seen["tid"] == "lesson" and seen["kw"]["kind"] == "lesson" and seen["kw"]["origin"] == "project-a"
    assert seen["kw"]["source"] == "cli"


def test_ticketcli_does_not_open_inbox_files_itself():
    import myvec.ticketcli as tc
    src = open(tc.__file__, encoding="utf-8").read()
    body = src[src.index("def lesson("):src.index("def _resolve_migrate_dir")]
    assert "open(" not in body and "write_text" not in body


def test_default_from_is_marker_name_of_cwd(target, tmp_path, monkeypatch):
    other = tmp_path / "fd"
    other.mkdir()
    (other / ".myvector.toml").write_text('name = "project-a"\n')
    monkeypatch.chdir(other)
    assert lesson("--title", "t", "-").exit_code == 0
    fm, _ = tickets.parse_frontmatter(files(target)[0].read_text())
    assert fm["from"] == "project-a"
