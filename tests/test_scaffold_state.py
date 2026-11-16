"""Scaffold state in the project: `.myvec/scaffold.json` (ADR-028). Everything runs in tmp_path."""
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec import scaffold as S
from myvec.commands import app
from myvec.scaffold import state as ST

runner = CliRunner()
FIXDIR = Path(__file__).parent / "fixtures" / "scaffold"
STATE = ".myvec/scaffold.json"
MIN = FIXDIR / "minimal.answers.json"


def run(*args):
    return runner.invoke(app, ["scaffold", *args])


def snap(root: Path):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def load(root):
    return json.loads((root / STATE).read_text(encoding="utf8"))


# ---------------------------------------------------------------- version guard

def test_render_digest_guard():
    fx = json.loads((FIXDIR / "process_version.json").read_text(encoding="utf8"))
    got = {"myvec": S.render_digest(S.load_answers(FIXDIR / "myvec.answers.json")),
           "minimal": S.render_digest(S.load_answers(MIN))}
    assert S.PROCESS_VERSION == fx["version"] and got == fx["digests"], (
        "рендер змінився: підніми PROCESS_VERSION і онови process_version.json")


# ---------------------------------------------------------------- write

def test_first_run_creates_state_with_answers_version_and_bases(proj):
    r = run("--answers", str(MIN), "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert {"path": STATE, "action": "create"} in json.loads(r.stdout)
    st = load(proj)
    assert st["format"] == 1 and st["version"] == S.PROCESS_VERSION
    assert st["answers"] == json.loads(MIN.read_text(encoding="utf8"))
    f = st["files"]["CLAUDE.md"]
    assert f["mode"] == "owned" and f["base"] == (proj / "CLAUDE.md").read_text(encoding="utf8")
    assert len(f["sha256"]) == 64
    assert STATE not in st["files"] and ".gitignore" not in st["files"] and ".claude/settings.json" not in st["files"]
    assert not any(k.startswith("docs/todo/E1-") for k in st["files"])  # the seed group is not process


def test_rerun_is_all_same_and_state_untouched(proj):
    assert run("--answers", str(MIN), "--root", str(proj)).exit_code == 0
    before = snap(proj)
    time.sleep(0.01)
    r = run("--answers", str(MIN), "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert {x["action"] for x in json.loads(r.stdout)} == {"same"}
    assert snap(proj) == before
    assert not list(proj.rglob("*.bak-*"))


def test_changed_state_is_merged_with_backup(proj):
    assert run("--answers", str(MIN), "--root", str(proj)).exit_code == 0
    (proj / STATE).write_text("{}\n", encoding="utf8")
    r = run("--answers", str(MIN), "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert {"path": STATE, "action": "merge"} in json.loads(r.stdout)
    assert len(list((proj / ".myvec").glob("scaffold.json.bak-*"))) == 1
    assert ST.read(proj)["status"] == "current"


def test_keep_and_append_set_modes(proj):
    assert run("--answers", str(MIN), "--root", str(proj)).exit_code == 0
    (proj / "CLAUDE.md").write_text("# Mine\n", encoding="utf8")
    (proj / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    r = run("--answers", str(MIN), "--root", str(proj), "--append", "CLAUDE.md", "--keep", ".claude/agents/coder.md")
    assert r.exit_code == 0, r.output
    files = load(proj)["files"]
    assert files["CLAUDE.md"]["mode"] == "append" and files[".claude/agents/coder.md"]["mode"] == "keep"
    assert files[".claude/agents/coder.md"]["base"] != "mine\n"


def test_no_tickets_and_foreign_have_no_ticket_files(proj):
    r = run("--answers", str(MIN), "--root", str(proj), "--no-tickets")
    assert r.exit_code == 0, r.output
    assert not any(k.startswith("docs/todo/") for k in load(proj)["files"])


def test_dry_run_shows_state_row_and_writes_nothing(proj):
    r = run("--answers", str(MIN), "--root", str(proj), "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    assert {"path": STATE, "action": "create"} in json.loads(r.stdout)
    assert not list(proj.iterdir())


def test_unresolved_conflict_writes_no_state(proj):
    (proj / "CLAUDE.md").write_text("# Mine\n", encoding="utf8")
    r = run("--answers", str(MIN), "--root", str(proj))
    assert r.exit_code == 3
    assert not (proj / ".myvec").exists()


def test_state_cannot_take_choices(proj):
    for flag in ("--overwrite", "--keep", "--append"):
        r = run("--answers", str(MIN), "--root", str(proj), flag, STATE)
        assert r.exit_code == 2, flag
    assert not list(proj.iterdir())


def test_hard_secret_in_state_is_refused(proj, monkeypatch):
    key = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    real = S.render

    def bad(a, **kw):
        out = real(a, **kw)
        out["CLAUDE.md"] += key
        return out

    monkeypatch.setattr(S, "render", bad)
    r = run("--answers", str(MIN), "--root", str(proj))
    assert r.exit_code == 2
    assert not list(proj.iterdir())


def test_symlinked_myvec_dir_is_refused(proj, tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (proj / ".myvec").symlink_to(out)
    r = run("--answers", str(MIN), "--root", str(proj))
    assert r.exit_code == 2
    assert not list(out.iterdir())


def test_failure_on_writing_state_rolls_everything_back(proj, monkeypatch):
    real = os.link

    def boom(src, dst, *a, **kw):
        if str(dst).endswith("scaffold.json"):
            raise OSError("disk full")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", boom)
    r = run("--answers", str(MIN), "--root", str(proj))
    assert r.exit_code == 2
    assert not [p for p in proj.rglob("*") if p.is_file()]
    assert not (proj / ".myvec").exists()


# ---------------------------------------------------------------- read

def test_read_none_when_no_state(proj):
    r = ST.read(proj)
    assert r["status"] == "none" and r["version"] is None and r["current"] == S.PROCESS_VERSION


def write_state(root, text):
    (root / ".myvec").mkdir(exist_ok=True)
    (root / STATE).write_text(text, encoding="utf8")


def test_read_current_outdated_newer(proj, monkeypatch):
    assert run("--answers", str(MIN), "--root", str(proj)).exit_code == 0
    assert ST.read(proj)["status"] == "current"
    written = S.PROCESS_VERSION
    monkeypatch.setattr(S, "PROCESS_VERSION", written + 1)
    r = ST.read(proj)
    assert r["status"] == "outdated" and r["version"] == written
    monkeypatch.setattr(S, "PROCESS_VERSION", 1)
    write_state(proj, json.dumps({"format": 1, "version": 7, "files": {}}))
    assert ST.read(proj)["status"] == "newer"
    write_state(proj, json.dumps({"format": 2, "version": 1, "files": {}}))
    assert ST.read(proj)["status"] == "newer"


@pytest.mark.parametrize("text", ["{broken", "[]", '{"format": 1, "version": "1"}', '{"format": 1, "version": 0}',
                                  '{"format": true, "version": 1}', '{"version": 1}', "\xff", "[" * 200000])
def test_read_invalid(proj, text):
    write_state(proj, text)
    r = ST.read(proj)
    assert r["status"] == "invalid" and r["reason"]


def test_read_invalid_for_symlinks_and_huge_file(proj, tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"format": 1, "version": 1}', encoding="utf8")
    (proj / ".myvec").mkdir()
    (proj / STATE).symlink_to(real)
    assert ST.read(proj)["status"] == "invalid"
    (proj / STATE).unlink()
    (proj / STATE).write_text(" " * (ST.MAX_BYTES + 1), encoding="utf8")
    assert ST.read(proj)["status"] == "invalid"
    (proj / STATE).unlink()
    (proj / ".myvec").rmdir()
    other = tmp_path / "dir"
    other.mkdir()
    (other / "scaffold.json").write_text('{"format": 1, "version": 1}', encoding="utf8")
    (proj / ".myvec").symlink_to(other)
    assert ST.read(proj)["status"] == "invalid"


# ---------------------------------------------------------------- CLI --state

def test_state_json_has_no_answers_or_bases(proj):
    assert run("--answers", str(MIN), "--root", str(proj)).exit_code == 0
    r = run("--state", "--json", "--root", str(proj))
    assert r.exit_code == 0, r.output
    d = json.loads(r.stdout)
    assert set(d) == {"status", "version", "current", "files", "reason"}
    assert d["status"] == "current" and d["version"] == S.PROCESS_VERSION
    assert "demo" not in r.stdout and "base" not in r.stdout and "sha256" not in r.stdout


def test_state_without_state_is_none_exit_0(proj):
    r = run("--state", "--json", "--root", str(proj))
    assert r.exit_code == 0
    assert json.loads(r.stdout)["status"] == "none"
    assert not list(proj.iterdir())


def test_state_invalid_still_exit_0(proj):
    write_state(proj, "{broken")
    r = run("--state", "--root", str(proj))
    assert r.exit_code == 0 and "status: invalid" in r.output


@pytest.mark.parametrize("extra", [["--answers", str(MIN)], ["--dry-run"], ["--detect"], ["--keep", "CLAUDE.md"],
                                   ["--no-tickets"], ["--diff"]])
def test_state_flag_conflicts(proj, extra):
    assert run("--state", "--root", str(proj), *extra).exit_code == 2


def test_read_filters_unknown_mode(proj):
    write_state(proj, json.dumps({"format": 1, "version": 1, "files": {
        "a": {"mode": "owned"}, "b": {"mode": {"x": 1}}, "c": {"mode": "zzz"}}}))
    assert ST.read(proj)["files"] == {"a": "owned"}
