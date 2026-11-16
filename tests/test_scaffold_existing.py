"""E5-06: scaffold into a folder that already has a process: keep/append/no-tickets/diff and the transactional apply."""
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec import scaffold as S
from myvec.commands import app

runner = CliRunner()
BASE = {"project": "demo", "description": "A demo app.", "stack": "Python 3.12", "test_cmd": "uv run pytest -q"}
FOREIGN = ("---\nid: E1-01\ntitle: A\nstatus: ⬜\nepic: E1\n---\n\n# E1-01 A\n\n## За один заход\n\nтекст\n")
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END RSA PRIVATE KEY-----\n"


def answers(tmp_path, **extra):
    p = tmp_path / "answers.json"
    p.write_text(json.dumps({**BASE, **extra}), encoding="utf8")
    return p


def run(*args):
    return runner.invoke(app, ["scaffold", *args])


def snap(root: Path):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


def names(root: Path):
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.fixture
def proj(tmp_path):
    """A scaffolded project with the owner's own CLAUDE.md."""
    root = tmp_path / "proj"
    root.mkdir()
    a = answers(tmp_path)
    assert run("--answers", str(a), "--root", str(root)).exit_code == 0
    (root / "CLAUDE.md").write_text("# Mine\n\nhand written rules\n", encoding="utf8")
    return root, a


# ------------------------------------------------------------ --keep

def test_keep_closes_conflict_and_does_not_touch_the_file(proj):
    root, a = proj
    before = snap(root)
    time.sleep(0.01)
    r = run("--answers", str(a), "--root", str(root), "--keep", "CLAUDE.md")
    assert r.exit_code == 0, r.output
    after = snap(root)
    state = ".myvec/scaffold.json"  # the only file that legitimately changes: CLAUDE.md is now recorded as `keep`
    own = lambda d: {k: v for k, v in d.items() if not k.startswith(state)}  # the state and its .bak-<ts>
    assert own(after) == own(before)
    assert json.loads((root / state).read_text(encoding="utf8"))["files"]["CLAUDE.md"]["mode"] == "keep"
    assert not list(root.glob("CLAUDE.md.bak-*"))


def test_keep_shows_up_as_keep_action_in_json_plan(proj):
    root, a = proj
    r = run("--answers", str(a), "--root", str(root), "--keep", "CLAUDE.md", "--dry-run", "--json")
    assert r.exit_code == 0
    assert {"path": "CLAUDE.md", "action": "keep"} in json.loads(r.stdout)


def test_keep_does_not_resolve_other_conflicts(proj):
    root, a = proj
    (root / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    before = snap(root)
    r = run("--answers", str(a), "--root", str(root), "--keep", "CLAUDE.md")
    assert r.exit_code == 3 and "coder.md" in r.output
    assert snap(root) == before


# ------------------------------------------------------------ --append

def test_append_adds_process_section_keeps_text_as_prefix_and_makes_bak(proj):
    root, a = proj
    old = (root / "CLAUDE.md").read_bytes()
    r = run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md")
    assert r.exit_code == 0, r.output
    new = (root / "CLAUDE.md").read_bytes()
    assert new.startswith(old) and len(new) > len(old)
    assert S.PROCESS_HEADING in new.decode("utf8")
    baks = list(root.glob("CLAUDE.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == old
    # only the process section, not the whole template head
    assert new.decode("utf8").count("# demo") == 0


def test_append_to_file_without_trailing_newline_keeps_prefix(proj):
    root, a = proj
    (root / "CLAUDE.md").write_bytes(b"# Mine\nno newline at the end")
    assert run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md").exit_code == 0
    new = (root / "CLAUDE.md").read_bytes()
    assert new.startswith(b"# Mine\nno newline at the end\n") and S.PROCESS_HEADING.encode() in new


def test_append_twice_is_refused_and_writes_nothing(proj):
    root, a = proj
    assert run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md").exit_code == 0
    before = snap(root)
    r = run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md")
    assert r.exit_code == 2 and "Development process" in r.output
    assert snap(root) == before
    assert len(list(root.glob("CLAUDE.md.bak-*"))) == 1


def test_append_is_refused_when_file_already_has_the_heading_even_with_other_text(proj):
    root, a = proj
    (root / "CLAUDE.md").write_text("# Mine\n\n## Development process\n\nmy own\n", encoding="utf8")
    before = snap(root)
    r = run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md")
    assert r.exit_code == 2 and snap(root) == before


def test_append_for_other_file_is_exit_2(proj):
    root, a = proj
    (root / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    before = snap(root)
    r = run("--answers", str(a), "--root", str(root), "--append", ".claude/agents/coder.md")
    assert r.exit_code == 2 and snap(root) == before


def test_append_dry_run_json_marks_append_and_writes_nothing(proj):
    root, a = proj
    before = snap(root)
    r = run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md", "--dry-run", "--json")
    assert r.exit_code == 0 and {"path": "CLAUDE.md", "action": "append"} in json.loads(r.stdout)
    assert snap(root) == before


# ------------------------------------------------------------ flag combinations

@pytest.mark.parametrize("flags", [("--keep", "--overwrite"), ("--keep", "--append"), ("--overwrite", "--append")])
def test_one_path_in_two_flags_is_exit_2(proj, flags):
    root, a = proj
    before = snap(root)
    args = []
    for f in flags:
        args += [f, "CLAUDE.md"]
    r = run("--answers", str(a), "--root", str(root), *args)
    assert r.exit_code == 2 and snap(root) == before


@pytest.mark.parametrize("flag", ["--keep", "--overwrite", "--append"])
def test_unknown_path_in_any_flag_is_exit_2(proj, flag):
    root, a = proj
    before = snap(root)
    assert run("--answers", str(a), "--root", str(root), flag, "README.md").exit_code == 2
    assert snap(root) == before


def test_dot_slash_prefix_is_accepted(proj):
    root, a = proj
    assert run("--answers", str(a), "--root", str(root), "--keep", "./CLAUDE.md").exit_code == 0


def test_overwrite_still_makes_bak(proj):
    root, a = proj
    r = run("--answers", str(a), "--root", str(root), "--overwrite", "CLAUDE.md")
    assert r.exit_code == 0
    assert list(root.glob("CLAUDE.md.bak-*"))[0].read_text(encoding="utf8").startswith("# Mine")


# ------------------------------------------------------------ tickets: foreign / --no-tickets

def foreign_tree(root: Path):
    f = root / "docs/todo/E1-x/E1-01-a.md"
    f.parent.mkdir(parents=True)
    f.write_text(FOREIGN, encoding="utf8")


def test_foreign_tickets_are_skipped_not_created_or_conflicted(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    foreign_tree(root)

    def boom(*a, **k):
        raise AssertionError("regenerate_index must not run for a foreign tree")

    monkeypatch.setattr(S, "regenerate_index", boom)
    before = snap(root)
    r = run("--answers", str(answers(tmp_path)), "--root", str(root), "--json")
    assert r.exit_code == 0, r.output
    acts = {x["path"]: x["action"] for x in json.loads(r.stdout)}
    todo = {p: a for p, a in acts.items() if p.startswith("docs/todo/")}
    assert todo and set(todo.values()) == {"skip-foreign"}
    after = snap(root)
    assert {k: v for k, v in after.items() if k.startswith("docs/todo")} == {k: v for k, v in before.items() if k.startswith("docs/todo")}
    assert not (root / "docs/todo/INDEX.md").exists() and not (root / "docs/todo/ticket-template.md").exists()
    assert (root / "CLAUDE.md").is_file()  # the rest of the process is still written


def test_foreign_detected_by_legacy_warning_alone(tmp_path):
    # `epic:` instead of `parent:` is only a `legacy_*` warning in `ticket check`, still foreign
    root = tmp_path / "p"
    root.mkdir()
    foreign_tree(root)
    r = run("--answers", str(answers(tmp_path)), "--root", str(root), "--dry-run", "--json")
    assert any(x["action"] == "skip-foreign" for x in json.loads(r.stdout))


def test_no_tickets_flag_skips_docs_todo_in_empty_dir(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    r = run("--answers", str(answers(tmp_path)), "--root", str(root), "--no-tickets", "--json")
    assert r.exit_code == 0, r.output
    acts = {x["path"]: x["action"] for x in json.loads(r.stdout)}
    assert all(a == "skip" for p, a in acts.items() if p.startswith("docs/todo/"))
    assert not (root / "docs/todo").exists()
    assert (root / "CLAUDE.md").is_file() and (root / ".claude/agents/coder.md").is_file()


def test_no_tickets_leaves_existing_canonical_tree_alone(proj):
    root, a = proj
    idx = root / "docs/todo/INDEX.md"
    idx.write_text(idx.read_text(encoding="utf8") + "\nmy note\n", encoding="utf8")
    (root / "docs/todo/ticket-template.md").write_text("changed\n", encoding="utf8")
    before = {k: v for k, v in snap(root).items() if k.startswith("docs/todo")}
    r = run("--answers", str(a), "--root", str(root), "--keep", "CLAUDE.md", "--no-tickets")
    assert r.exit_code == 0, r.output
    assert {k: v for k, v in snap(root).items() if k.startswith("docs/todo")} == before


# ------------------------------------------------------------ tickets_language

@pytest.mark.parametrize("lang", ["en", "uk", "ru"])
def test_tickets_language_valid(tmp_path, lang):
    root = tmp_path / "p"
    root.mkdir()
    assert run("--answers", str(answers(tmp_path, tickets_language=lang)), "--root", str(root)).exit_code == 0


@pytest.mark.parametrize("bad", ["de", "", "EN", None, 1, ["en"]])
def test_tickets_language_invalid_exit_2_and_nothing_written(tmp_path, bad):
    root = tmp_path / "p"
    root.mkdir()
    r = run("--answers", str(answers(tmp_path, tickets_language=bad)), "--root", str(root))
    assert r.exit_code == 2 and not any(root.iterdir())


# ------------------------------------------------------------ --diff

def test_diff_for_conflict_and_merge_in_json(proj):
    root, a = proj
    (root / ".gitignore").write_text("dist/\n", encoding="utf8")
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff", "--json")
    assert r.exit_code == 3
    rows = {x["path"]: x for x in json.loads(r.stdout)}
    d = rows["CLAUDE.md"]["diff"]
    assert d.startswith("--- a/CLAUDE.md") and "-hand written rules" in d and "+# demo" in d
    assert "+.worktrees/" in rows[".gitignore"]["diff"]
    assert "diff" not in rows[".claude/agents/coder.md"]  # same rows carry no diff


def test_diff_dry_run_writes_nothing(proj):
    root, a = proj
    before = snap(root)
    run("--answers", str(a), "--root", str(root), "--dry-run", "--diff", "--json")
    assert snap(root) == before


def test_diff_masks_secret_literals(proj):
    root, a = proj
    (root / "CLAUDE.md").write_text('# Mine\n\napi_key = "supersecretvalue12345"\npassword: hunter2hunter2\n', encoding="utf8")
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff", "--json")
    d = {x["path"]: x for x in json.loads(r.stdout)}["CLAUDE.md"]["diff"]
    assert "supersecretvalue12345" not in d and "hunter2hunter2" not in d and "•••" in d
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff")
    assert "supersecretvalue12345" not in r.output and "hunter2hunter2" not in r.output


def test_diff_withheld_for_file_with_private_key(proj):
    root, a = proj
    (root / "CLAUDE.md").write_text("# Mine\n" + PEM, encoding="utf8")
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff", "--json")
    row = {x["path"]: x for x in json.loads(r.stdout)}["CLAUDE.md"]
    assert row.get("diff_withheld") is True and "diff" not in row
    assert "MIIBOgIBAAJBAKj34" not in r.output and "BEGIN RSA" not in r.output
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff")
    assert "withheld" in r.output and "MIIBOgIBAAJBAKj34" not in r.output


def test_diff_withheld_for_known_token_format(proj):
    root, a = proj
    (root / "CLAUDE.md").write_text("# Mine\nkey AKIAIOSFODNN7EXAMPLE\n", encoding="utf8")
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff", "--json")
    assert "AKIAIOSFODNN7EXAMPLE" not in r.output
    assert {x["path"]: x for x in json.loads(r.stdout)}["CLAUDE.md"].get("diff_withheld") is True


def test_diff_for_append_shows_only_added_section(proj):
    root, a = proj
    r = run("--answers", str(a), "--root", str(root), "--dry-run", "--diff", "--append", "CLAUDE.md", "--json")
    d = {x["path"]: x for x in json.loads(r.stdout)}["CLAUDE.md"]["diff"]
    assert "+## Development process" in d and "-hand written rules" not in d


# ------------------------------------------------------------ transactional apply: TOCTOU

def prep(root: Path, tmp_path):
    a = S.load_answers(answers(tmp_path))
    rendered = S.render(a)
    return a, rendered


def test_plan_records_fingerprints(proj, tmp_path):
    root, _ = proj
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    assert set(pl.fp) >= {r for r, _ in pl}
    assert pl.fp["CLAUDE.md"] != pl.fp[".claude/agents/coder.md"]


def test_file_changed_between_plan_and_apply_is_exit_3_and_nothing_written(proj, tmp_path, monkeypatch):
    root, a = proj
    real_plan = S.plan

    def plan_then_edit(*args, **kw):
        pl = real_plan(*args, **kw)
        (root / ".claude/agents/coder.md").write_text("edited meanwhile\n", encoding="utf8")
        return pl

    (root / ".claude/agents/tester.md").unlink()  # something to create, so a partial write would show
    before_names = names(root)
    monkeypatch.setattr(S, "plan", plan_then_edit)
    r = run("--answers", str(a), "--root", str(root), "--overwrite", "CLAUDE.md")
    monkeypatch.setattr(S, "plan", real_plan)
    assert r.exit_code == 3, r.output
    assert (root / "CLAUDE.md").read_text(encoding="utf8").startswith("# Mine")
    assert (root / ".claude/agents/coder.md").read_text(encoding="utf8") == "edited meanwhile\n"
    assert names(root) == before_names  # no tester.md, no .bak, no tmp


def test_overwrite_target_edited_after_plan_raises_scaffold_changed(proj, tmp_path):
    root, _ = proj
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    (root / "CLAUDE.md").write_text("edited again\n", encoding="utf8")
    before = snap(root)
    with pytest.raises(S.ScaffoldChanged):
        S.apply(root, pl, rendered, {"CLAUDE.md"}, a)
    assert snap(root) == before


def test_file_appearing_at_a_create_path_after_plan_is_not_overwritten(proj, tmp_path):
    root, _ = proj
    (root / ".claude/agents/tester.md").unlink()
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    (root / ".claude/agents/tester.md").write_text("appeared\n", encoding="utf8")
    with pytest.raises(S.ScaffoldChanged):
        S.apply(root, pl, rendered, {"CLAUDE.md"}, a)
    assert (root / ".claude/agents/tester.md").read_text(encoding="utf8") == "appeared\n"
    assert (root / "CLAUDE.md").read_text(encoding="utf8").startswith("# Mine")


def test_create_never_overwrites_a_file_that_wins_the_race_in_the_link_phase(proj, tmp_path, monkeypatch):
    root, _ = proj
    victim = root / ".claude/agents/tester.md"
    victim.unlink()
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    real_link = os.link

    def racing_link(src, dst, *args, **kw):
        if str(dst).endswith("tester.md") and not victim.exists():
            victim.write_text("racer\n", encoding="utf8")
        return real_link(src, dst, *args, **kw)

    monkeypatch.setattr(S.os, "link", racing_link)
    try:
        with pytest.raises(S.ScaffoldChanged):
            S.apply(root, pl, rendered, {"CLAUDE.md"}, a)
    finally:
        monkeypatch.setattr(S.os, "link", real_link)
    assert victim.read_text(encoding="utf8") == "racer\n"
    assert (root / "CLAUDE.md").read_text(encoding="utf8").startswith("# Mine")  # rolled back


def test_symlink_swapped_into_the_path_after_plan_writes_nothing_outside(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    (root / "docs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(S.ScaffoldError):
        S.apply(root, pl, rendered, set(), a)
    assert not any(outside.iterdir())
    assert not (root / "CLAUDE.md").exists() and not (root / ".claude").exists()


def test_symlinked_target_file_after_plan_is_rejected(proj, tmp_path):
    root, _ = proj
    other = tmp_path / "other.md"
    other.write_text("outside\n", encoding="utf8")
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    (root / "CLAUDE.md").unlink()
    (root / "CLAUDE.md").symlink_to(other)
    with pytest.raises(S.ScaffoldError):
        S.apply(root, pl, rendered, {"CLAUDE.md"}, a)
    assert other.read_text(encoding="utf8") == "outside\n"


def test_apply_with_unresolved_conflict_writes_nothing(proj, tmp_path):
    root, _ = proj
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    (root / ".claude/agents/tester.md").unlink()
    pl = S.plan(root, rendered, a)
    before = snap(root)
    assert S.apply(root, pl, rendered, set(), a) is pl
    assert snap(root) == before


# ------------------------------------------------------------ transactional apply: OSError

def leftovers(root: Path):
    return [n for n in names(root) if ".tmp" in n or ".bak-" in n]


def test_oserror_in_prepare_changes_no_target_and_removes_tmp(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    real_open = os.open
    calls = {"n": 0}

    def flaky(path, flags, *args, **kw):
        if ".tmp" in str(path):
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError(28, "No space left on device")
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(S.os, "open", flaky)
    try:
        with pytest.raises(OSError):
            S.apply(root, pl, rendered, set(), a)
    finally:
        monkeypatch.setattr(S.os, "open", real_open)
    assert calls["n"] == 5
    assert names(root) == set()  # no target, no tmp, no directory created along the way


def test_oserror_in_prepare_on_backup_leaves_existing_files_untouched(proj, tmp_path, monkeypatch):
    root, _ = proj
    a, rendered = prep(root, tmp_path)
    (root / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    pl = S.plan(root, rendered, a)
    before = snap(root)
    real_copy2 = S.shutil.copy2
    n = {"c": 0}

    def flaky_copy2(src, dst, *args, **kw):
        n["c"] += 1
        if n["c"] == 2:
            raise OSError(5, "I/O error")
        return real_copy2(src, dst, *args, **kw)

    monkeypatch.setattr(S.shutil, "copy2", flaky_copy2)
    try:
        with pytest.raises(OSError):
            S.apply(root, pl, rendered, {"CLAUDE.md", ".claude/agents/coder.md"}, a)
    finally:
        monkeypatch.setattr(S.shutil, "copy2", real_copy2)
    assert n["c"] == 2
    assert snap(root) == before and not leftovers(root)


def test_cli_prepare_oserror_is_exit_2(proj, monkeypatch):
    root, a = proj
    before = snap(root)
    real_copy2 = S.shutil.copy2

    def bad(*args, **kw):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(S.shutil, "copy2", bad)
    try:
        r = run("--answers", str(a), "--root", str(root), "--overwrite", "CLAUDE.md")
    finally:
        monkeypatch.setattr(S.shutil, "copy2", real_copy2)
    assert r.exit_code == 2
    assert snap(root) == before


def test_oserror_in_replace_phase_rolls_back_everything(proj, tmp_path, monkeypatch):
    root, _ = proj
    (root / ".gitignore").write_text("dist/\n", encoding="utf8")
    (root / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    (root / ".claude/agents/tester.md").unlink()
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    before = {k: v[0] for k, v in snap(root).items()}
    real_replace = os.replace
    n = {"c": 0}

    def flaky(src, dst, *args, **kw):
        n["c"] += 1
        if n["c"] == 2:
            raise OSError(5, "I/O error")
        return real_replace(src, dst, *args, **kw)

    monkeypatch.setattr(S.os, "replace", flaky)
    try:
        with pytest.raises(OSError) as e:
            S.apply(root, pl, rendered, {"CLAUDE.md", ".claude/agents/coder.md"}, a)
    finally:
        monkeypatch.setattr(S.os, "replace", real_replace)
    assert n["c"] == 2 and "rolled back" in str(e.value)
    after = {k: v[0] for k, v in snap(root).items() if ".bak-" not in k}
    assert after == before  # replaced files restored, the created tester.md removed, no tmp left
    assert not [x for x in names(root) if ".tmp" in x]


def test_cli_replace_phase_oserror_is_exit_2_with_rollback_note(proj, monkeypatch):
    root, a = proj
    before = {k: v[0] for k, v in snap(root).items()}
    real_replace = os.replace
    n = {"c": 0}

    def flaky(src, dst, *args, **kw):
        n["c"] += 1
        if n["c"] >= 2:
            raise OSError(5, "I/O error")
        return real_replace(src, dst, *args, **kw)

    (root / ".gitignore").write_text("dist/\n", encoding="utf8")
    before[".gitignore"] = b"dist/\n"
    monkeypatch.setattr(S.os, "replace", flaky)
    try:
        r = run("--answers", str(a), "--root", str(root), "--overwrite", "CLAUDE.md")
    finally:
        monkeypatch.setattr(S.os, "replace", real_replace)
    assert r.exit_code == 2 and "rolled back" in r.output
    assert {k: v[0] for k, v in snap(root).items() if ".bak-" not in k} == before


def test_successful_apply_leaves_no_tmp_files(proj):
    root, a = proj
    assert run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md").exit_code == 0
    assert not [x for x in names(root) if ".tmp" in x]


def test_apply_preserves_file_mode_of_replaced_file(proj):
    root, a = proj
    (root / "CLAUDE.md").chmod(0o640)
    assert run("--answers", str(a), "--root", str(root), "--append", "CLAUDE.md").exit_code == 0
    assert (root / "CLAUDE.md").stat().st_mode & 0o777 == 0o640


# ------------------------------------------------------------ review fixes

def test_keep_on_a_merge_row_leaves_settings_untouched(proj):
    root, a = proj
    (root / ".claude/settings.json").write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}\n', encoding="utf8")
    before = (root / ".claude/settings.json").read_bytes()
    r = run("--answers", str(a), "--root", str(root), "--keep", "CLAUDE.md", "--keep", ".claude/settings.json")
    assert r.exit_code == 0, r.output
    assert (root / ".claude/settings.json").read_bytes() == before
    assert not list((root / ".claude").glob("settings.json.bak-*"))


def test_failed_rollback_keeps_the_backup_and_says_so(proj, tmp_path, monkeypatch):
    root, _ = proj
    (root / ".claude/agents/coder.md").write_text("mine\n", encoding="utf8")
    a, rendered = prep(root, tmp_path)
    pl = S.plan(root, rendered, a)
    real_replace, real_wb = os.replace, Path.write_bytes
    n = {"r": 0}

    def flaky_replace(src, dst, *args, **kw):
        n["r"] += 1
        if n["r"] == 2:
            raise OSError(5, "I/O error")
        return real_replace(src, dst, *args, **kw)

    def failing_write(self, data):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(S.os, "replace", flaky_replace)
    monkeypatch.setattr(Path, "write_bytes", failing_write)
    try:
        with pytest.raises(OSError) as ei:
            S.apply(root, pl, rendered, {"CLAUDE.md", ".claude/agents/coder.md"}, a)
    finally:
        monkeypatch.setattr(S.os, "replace", real_replace)
        monkeypatch.setattr(Path, "write_bytes", real_wb)
    assert "rollback failed" in str(ei.value) and "backup kept" in str(ei.value)
    baks = list(root.glob("CLAUDE.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf8") == "# Mine\n\nhand written rules\n"


def test_symlink_after_plan_is_exit_code_3_in_cli(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    a = answers(tmp_path)
    real_plan = S.plan

    def plan_then_link(*args, **kw):
        pl = real_plan(*args, **kw)
        (root / "docs").symlink_to(outside, target_is_directory=True)
        return pl

    monkeypatch.setattr(S, "plan", plan_then_link)
    r = run("--answers", str(a), "--root", str(root))
    monkeypatch.setattr(S, "plan", real_plan)
    assert r.exit_code == 3, r.output
    assert not any(outside.iterdir())
