"""`myvec scaffold --adopt` (E8-12, ADR-041): only the search rule goes into a project without a scaffold state.
Everything runs in tmp_path."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec import scaffold as S
from myvec.commands import app
from myvec.scaffold import adopt as AD

runner = CliRunner()
FIXDIR = Path(__file__).parent / "fixtures" / "scaffold"
MIN = FIXDIR / "minimal.answers.json"
RULE = S.SEARCH_RULE
STATE = ".myvec/scaffold.json"


def run(*args):
    return runner.invoke(app, ["scaffold", *args])


def snap(root: Path):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "coder.md").write_text("# my coder\n", encoding="utf8")
    (root / "CLAUDE.md").write_text("# mine\n", encoding="utf8")
    return root


def test_preview_plans_create_and_writes_nothing(proj):
    before = snap(proj)
    r = run("--adopt", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert "create" in r.output and RULE in r.output and "nothing written" in r.output
    assert snap(proj) == before
    assert not (proj / ".myvec").exists()


def test_write_creates_only_the_rule_and_no_state(proj):
    before = snap(proj)
    r = run("--adopt", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out == {"status": "written", "files": [{"path": RULE, "action": "create"}], "write": [RULE]}
    after = snap(proj)
    assert set(after) - set(before) == {RULE}
    assert {k: v for k, v in after.items() if k != RULE} == before
    assert after[RULE].decode("utf8") == S._read_template(RULE)
    assert not (proj / ".myvec").exists()


def test_repeat_is_current_and_changes_nothing(proj):
    assert run("--adopt", "--write", "--root", str(proj)).exit_code == 0
    before = snap(proj)
    r = run("--adopt", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert out["status"] == "current" and out["files"] == [{"path": RULE, "action": "same"}] and out["write"] == []
    assert snap(proj) == before
    assert "already in place" in run("--adopt", "--root", str(proj)).output


def test_foreign_own_search_rule_is_left_alone_exit_0(proj):
    (proj / ".claude" / "rules").mkdir()
    (proj / RULE).write_text("# our own search rule\n", encoding="utf8")
    before = snap(proj)
    for args in (("--adopt",), ("--adopt", "--write")):
        r = run(*args, "--root", str(proj), "--json")
        assert r.exit_code == 0, r.output
        out = json.loads(r.stdout)
        assert out["status"] == "foreign" and out["files"] == [{"path": RULE, "action": "foreign"}] and out["write"] == []
        assert snap(proj) == before


def test_foreign_ignores_overwrite_flag_as_incompatible(proj):
    (proj / ".claude" / "rules").mkdir()
    (proj / RULE).write_text("# ours\n", encoding="utf8")
    before = snap(proj)
    assert run("--adopt", "--write", "--overwrite", RULE, "--root", str(proj)).exit_code == 2
    assert snap(proj) == before


@pytest.fixture
def with_state(tmp_path):
    root = tmp_path / "scaffolded"
    root.mkdir()
    assert run("--answers", str(MIN), "--root", str(root)).exit_code == 0
    return root


def test_current_state_refuses_with_exit_2_and_hint(with_state):
    before = snap(with_state)
    r = run("--adopt", "--write", "--root", str(with_state))
    assert r.exit_code == 2 and "--update" in r.output
    assert snap(with_state) == before


def test_outdated_state_refuses_with_exit_2(with_state):
    st = json.loads((with_state / STATE).read_text())
    st["version"] = S.PROCESS_VERSION - 1
    (with_state / STATE).write_text(json.dumps(st))
    r = run("--adopt", "--root", str(with_state))
    assert r.exit_code == 2 and "--update" in r.output


def test_invalid_state_refuses_with_exit_2(proj):
    (proj / ".myvec").mkdir()
    (proj / STATE).write_text("{broken", encoding="utf8")
    before = snap(proj)
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 2 and "invalid" in r.output
    assert snap(proj) == before


def test_newer_state_refuses_with_exit_2(with_state):
    st = json.loads((with_state / STATE).read_text())
    st["version"] = S.PROCESS_VERSION + 4
    (with_state / STATE).write_text(json.dumps(st))
    r = run("--adopt", "--root", str(with_state))
    assert r.exit_code == 2 and "newer" in r.output


def test_no_claude_dir_refuses_with_exit_2(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    r = run("--adopt", "--write", "--root", str(root))
    assert r.exit_code == 2 and ".claude" in r.output
    assert snap(root) == {}


def test_claude_dir_is_a_file_refuses(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / ".claude").write_text("x")
    assert run("--adopt", "--write", "--root", str(root)).exit_code == 2


def test_claude_dir_symlink_refuses_and_writes_nothing_outside(tmp_path):
    real = tmp_path / "elsewhere"
    (real / "rules").mkdir(parents=True)
    root = tmp_path / "p"
    root.mkdir()
    (root / ".claude").symlink_to(real)
    r = run("--adopt", "--write", "--root", str(root))
    assert r.exit_code == 2
    assert not (real / "rules" / "search.md").exists()


def test_rules_dir_symlink_refuses_and_writes_nothing_outside(proj, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (proj / ".claude" / "rules").symlink_to(real)
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 2, r.output
    assert list(real.iterdir()) == []


def test_case_twin_of_the_rule_does_not_get_a_second_file(proj):
    (proj / ".claude" / "rules").mkdir()
    (proj / ".claude" / "rules" / "Search.md").write_text("# ours\n", encoding="utf8")
    before = snap(proj)
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert snap(proj) == before
    assert AD.run(proj, True)["status"] == "foreign"


@pytest.mark.parametrize("flag", [["--update"], ["--answers", str(MIN)], ["--overwrite", RULE], ["--keep", RULE],
                                  ["--append"], ["--no-tickets"], ["--dry-run"], ["--diff"], ["--detect"], ["--state"]])
def test_adopt_rejects_incompatible_flags(proj, flag):
    before = snap(proj)
    r = run("--adopt", *flag, "--root", str(proj))
    assert r.exit_code == 2, r.output
    assert snap(proj) == before


def test_change_between_plan_and_write_is_exit_3_and_nothing_written(proj, monkeypatch):
    real_plan = S.plan

    def plan_then_race(*a, **k):
        pl = real_plan(*a, **k)
        (proj / ".claude" / "rules").mkdir(exist_ok=True)
        (proj / RULE).write_text("# appeared after the plan\n", encoding="utf8")
        return pl
    monkeypatch.setattr(S, "plan", plan_then_race)
    r = run("--adopt", "--write", "--root", str(proj))
    monkeypatch.setattr(S, "plan", real_plan)
    assert r.exit_code == 3, r.output
    assert (proj / RULE).read_text(encoding="utf8") == "# appeared after the plan\n"
    assert not (proj / ".myvec").exists()


@pytest.mark.parametrize("text", ["x {{slot}}\n", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"])
def test_template_with_secret_or_slot_is_refused(proj, monkeypatch, text):
    monkeypatch.setattr(S, "_read_template", lambda rel: text)
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 2
    assert not (proj / RULE).exists()


def test_run_returns_preview_text(proj):
    rep = AD.run(proj, False)
    assert rep["status"] == "preview" and rep["text"] == S._read_template(RULE) and rep["write"] == []


def test_write_without_update_or_adopt_message_mentions_both(proj):
    r = run("--write", "--root", str(proj))
    assert r.exit_code == 2 and "--adopt" in r.output


# ------------------------------------------------------------------ refresh of an older render (E8-17, ADR-046)
import hashlib  # noqa: E402

from myvec import problems  # noqa: E402

RULE_DIR = FIXDIR / "search_rule"
CURRENT_SHA = "275515839e5a24869ae2047f3bea2b74a7d6b8d4db68a7517c8b8aad0cf74167"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_guard_fixtures_and_known_hashes_agree():
    by_fix = {}
    for f in sorted(RULE_DIR.glob("v*.md")):
        by_fix[_sha(f.read_bytes())] = int(f.stem[1:])
    assert by_fix == problems.SEARCH_RULE_OLD


def test_guard_current_template_is_pinned_and_not_in_history():
    cur = _sha(S._read_template(S.SEARCH_RULE).encode("utf8"))
    msg = ("шаблон search.md змінився: збережи попередній текст у fixtures/scaffold/search_rule/vN.md, "
           "додай його хеш у problems.SEARCH_RULE_OLD, онови пін")
    assert cur == CURRENT_SHA, msg
    assert cur not in problems.SEARCH_RULE_OLD, msg


def _old(proj, data=None):
    (proj / ".claude" / "rules").mkdir(parents=True, exist_ok=True)
    (proj / RULE).write_bytes((RULE_DIR / "v3.md").read_bytes() if data is None else data)


def test_old_render_previews_update_and_writes_nothing(proj):
    _old(proj)
    before = snap(proj)
    r = run("--adopt", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"status": "preview", "files": [{"path": RULE, "action": "update"}], "write": []}
    assert snap(proj) == before
    assert "rerun with --write to update it" in run("--adopt", "--root", str(proj)).output


def test_old_render_is_updated_with_write_then_current(proj):
    _old(proj)
    before = snap(proj)
    r = run("--adopt", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"status": "written", "files": [{"path": RULE, "action": "update"}], "write": [RULE]}
    assert (proj / RULE).read_text(encoding="utf8") == S._read_template(S.SEARCH_RULE)
    assert not (proj / ".myvec").exists()
    after = snap(proj)
    baks = [k for k in after if ".bak-" in k]
    assert len(baks) == 1 and after[baks[0]] == (RULE_DIR / "v3.md").read_bytes()
    assert {k: v for k, v in after.items() if k not in baks and k != RULE} == {k: v for k, v in before.items() if k != RULE}
    assert json.loads(run("--adopt", "--root", str(proj), "--json").output)["status"] == "current"


@pytest.mark.parametrize("edit", [lambda b: b + b"\n", lambda b: b.replace(b"\n", b"\r\n"), lambda b: b[:-2] + b"x\n"])
def test_edited_old_render_is_foreign_and_untouched(proj, edit):
    data = edit((RULE_DIR / "v3.md").read_bytes())
    _old(proj, data)
    before = snap(proj)
    for args in (("--adopt",), ("--adopt", "--write")):
        r = run(*args, "--root", str(proj))
        assert r.exit_code == 0, r.output
        assert "merge the new text by hand" in r.output
    assert snap(proj) == before


def test_old_render_changed_between_plan_and_write_is_exit_3(proj, monkeypatch):
    _old(proj)
    real_plan = S.plan

    def plan_then_edit(*a, **k):
        pl = real_plan(*a, **k)
        (proj / RULE).write_text("# owner edit\n", encoding="utf8")
        return pl
    monkeypatch.setattr(S, "plan", plan_then_edit)
    r = run("--adopt", "--write", "--root", str(proj))
    monkeypatch.setattr(S, "plan", real_plan)
    assert r.exit_code == 3, r.output
    assert (proj / RULE).read_text(encoding="utf8") == "# owner edit\n"
    assert not list((proj / ".claude" / "rules").glob("*.bak-*"))


def test_old_render_symlink_is_refused(proj, tmp_path):
    (tmp_path / "real.md").write_bytes((RULE_DIR / "v3.md").read_bytes())
    (proj / ".claude" / "rules").mkdir()
    (proj / RULE).symlink_to(tmp_path / "real.md")
    r = run("--adopt", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 2, r.output   # a symlink on the path is refused, never followed
    assert (proj / RULE).is_symlink() and not list((proj / ".claude" / "rules").glob("*.bak-*"))


def test_case_twin_holding_the_old_render_is_foreign_not_updated(proj):
    (proj / ".claude" / "rules").mkdir()
    (proj / ".claude" / "rules" / "Search.md").write_bytes((RULE_DIR / "v3.md").read_bytes())
    before = snap(proj)
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert snap(proj) == before


def test_old_render_with_scaffold_state_is_not_touched_by_adopt(with_state):
    _old(with_state)
    before = snap(with_state)
    r = run("--adopt", "--write", "--root", str(with_state))
    assert r.exit_code == 2, r.output
    assert snap(with_state) == before


def test_write_output_names_the_backup_and_hides_owner_text(proj):
    _old(proj)
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert ".bak-" in r.output and "is kept as" in r.output


def test_foreign_output_does_not_print_owner_file_text(proj):
    _old(proj, b"SECRET-OWNER-LINE\n")
    r = run("--adopt", "--root", str(proj))
    assert "SECRET-OWNER-LINE" not in r.output


def test_registry_hashes_are_sha256_hex():
    for h, v in problems.SEARCH_RULE_OLD.items():
        assert len(h) == 64 and int(h, 16) >= 0 and isinstance(v, int) and v >= 1


def test_backup_line_names_only_the_new_backup(proj):
    _old(proj)
    stale = proj / ".claude" / "rules" / "search.md.bak-19990101000000"
    stale.write_text("old\n", encoding="utf8")
    r = run("--adopt", "--write", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert "19990101000000" not in r.output and ".bak-" in r.output
    assert stale.exists()
