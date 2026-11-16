"""`myvec scaffold --update` (E0-09, ADR-035): 3-way merge of a new render into a project with a scaffold state.
Everything runs in tmp_path; an "old template" is simulated by rewriting `files[rel].base` and the disk file."""
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec import filters
from myvec import scaffold as S
from myvec.commands import app
from myvec.scaffold import state as ST
from myvec.scaffold import update as UP

runner = CliRunner()
FIXDIR = Path(__file__).parent / "fixtures" / "scaffold"
MIN = FIXDIR / "minimal.answers.json"
STATE = ".myvec/scaffold.json"
WF = ".claude/rules/workflow.md"
LS = "docs/lessons.md"


def run(*args):
    return runner.invoke(app, ["scaffold", *args])


def snap(root: Path):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def load_state(root):
    return json.loads((root / STATE).read_text(encoding="utf8"))


def save_state(root, st):
    (root / STATE).write_text(json.dumps(st, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    r = run("--answers", str(MIN), "--root", str(root))
    assert r.exit_code == 0, r.output
    return root


def age(root, rel, old_first_line="# OLD HEADER", tail=None, version_back=1):
    """Make `rel` look as rendered by an older template: the state base and the disk text get an old first line."""
    st = load_state(root)
    cur = st["files"][rel]["base"]
    old = old_first_line + "\n" + cur.split("\n", 1)[1]
    st["files"][rel]["base"] = old
    st["version"] = S.PROCESS_VERSION - version_back
    save_state(root, st)
    (root / rel).write_text(old, encoding="utf8")
    return cur, old


# ---------------------------------------------------------------- no state / refusals

def test_no_state_refuses_with_exit_2(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    before = snap(root)
    r = run("--update", "--root", str(root))
    assert r.exit_code == 2 and "no scaffold state" in r.output
    assert snap(root) == before


def test_invalid_state_refuses_with_exit_2(proj):
    (proj / STATE).write_text("{not json", encoding="utf8")
    r = run("--update", "--root", str(proj))
    assert r.exit_code == 2 and "invalid" in r.output


def test_newer_state_refuses_with_exit_2(proj):
    st = load_state(proj)
    st["version"] = S.PROCESS_VERSION + 5
    save_state(proj, st)
    r = run("--update", "--root", str(proj))
    assert r.exit_code == 2 and "newer" in r.output


def test_malformed_files_entry_is_invalid(proj):
    st = load_state(proj)
    st["files"]["CLAUDE.md"]["mode"] = "bogus"
    save_state(proj, st)
    rep, data = ST.load(proj)
    assert data is None and rep["status"] == "invalid"
    assert run("--update", "--root", str(proj)).exit_code == 2


def test_write_without_update_is_refused(proj):
    r = run("--write", "--root", str(proj))
    assert r.exit_code == 2 and "--update" in r.output


@pytest.mark.parametrize("flag", ["--append", "--no-tickets", "--dry-run", "--diff", "--detect", "--state"])
def test_update_rejects_incompatible_flags(proj, flag):
    before = snap(proj)
    r = run("--update", flag, "--root", str(proj))
    assert r.exit_code == 2
    assert snap(proj) == before


def test_missing_git_gives_exit_2_and_writes_nothing(proj, monkeypatch):
    age(proj, WF)
    before = snap(proj)
    monkeypatch.setattr(UP.shutil, "which", lambda name: None)
    for args in (("--update",), ("--update", "--write")):
        r = run(*args, "--root", str(proj))
        assert r.exit_code == 2 and "git" in r.output
    assert snap(proj) == before


def test_merge3_without_git_raises(monkeypatch):
    monkeypatch.setattr(UP.shutil, "which", lambda name: None)
    with pytest.raises(S.ScaffoldError):
        UP.merge3("a\n", "a\n", "b\n")


def test_saved_answers_invalid_asks_for_answers_file(proj):
    st = load_state(proj)
    st["answers"] = {"project": "demo"}
    save_state(proj, st)
    r = run("--update", "--root", str(proj))
    assert r.exit_code == 2 and "--answers" in r.output


def test_answers_flag_overrides_saved_answers(proj, tmp_path):
    raw = json.loads(MIN.read_text(encoding="utf8"))
    raw["stack"] = "Rust"
    f = tmp_path / "a.json"
    f.write_text(json.dumps(raw), encoding="utf8")
    r = run("--update", "--root", str(proj), "--answers", str(f), "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert next(x for x in out["files"] if x["path"] == "CLAUDE.md")["action"] == "update"
    assert "Rust" in next(x for x in out["files"] if x["path"] == "CLAUDE.md")["diff"]


# ---------------------------------------------------------------- current / preview

def test_current_project_is_up_to_date_and_untouched(proj):
    before = snap(proj)
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["status"] == "current" and out["write"] == [] and out["conflicts"] == []
    assert snap(proj) == before


def test_preview_without_write_shows_diff_exit_0_writes_nothing(proj):
    age(proj, WF)
    before = snap(proj)
    r = run("--update", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert "update" in r.output and WF in r.output and "# OLD HEADER" in r.output
    assert "preview only" in r.output
    assert snap(proj) == before


def test_preview_json_reports_status_versions_and_diff(proj):
    age(proj, WF)
    before = snap(proj)
    r = run("--update", "--root", str(proj), "--json")
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert out["status"] == "preview"
    assert out["from_version"] == S.PROCESS_VERSION - 1 and out["to_version"] == S.PROCESS_VERSION
    row = next(x for x in out["files"] if x["path"] == WF)
    assert row["action"] == "update" and "-# OLD HEADER" in row["diff"]
    assert WF in out["write"] and STATE in out["write"] and out["conflicts"] == []
    assert "backups" not in out
    assert snap(proj) == before


# ---------------------------------------------------------------- write / merge

def test_write_updates_untouched_file_and_state_version(proj):
    cur, _ = age(proj, WF)
    r = run("--update", "--write", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert (proj / WF).read_text(encoding="utf8") == cur
    st = load_state(proj)
    assert st["version"] == S.PROCESS_VERSION and st["files"][WF]["base"] == cur
    assert run("--state", "--root", str(proj), "--json").exit_code == 0
    again = run("--update", "--root", str(proj), "--json")
    assert json.loads(again.stdout)["status"] == "current"


def test_write_merges_and_keeps_project_edits_where_template_unchanged(proj):
    cur, old = age(proj, WF)
    mine = old.rstrip("\n") + "\n\n## Our project rule\n\nAlways squash.\n"
    (proj / WF).write_text(mine, encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["status"] == "written"
    assert next(x for x in out["files"] if x["path"] == WF)["action"] == "merge"
    text = (proj / WF).read_text(encoding="utf8")
    assert text.startswith(cur.split("\n", 1)[0] + "\n")      # template change taken
    assert "# OLD HEADER" not in text
    assert "## Our project rule\n\nAlways squash.\n" in text  # project edit kept
    assert "<<<<<<<" not in text
    st = load_state(proj)
    assert st["version"] == S.PROCESS_VERSION and st["files"][WF]["base"] == cur


def test_project_only_edit_with_unchanged_template_is_left_alone(proj):
    (proj / LS).write_text((proj / LS).read_text(encoding="utf8") + "\n- our lesson\n", encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert next(x for x in out["files"] if x["path"] == LS)["action"] == "unchanged"
    assert "- our lesson" in (proj / LS).read_text(encoding="utf8")


def keepify(proj, rel=WF):
    """Make `rel` a keep file whose base is an older render (disk = that old text)."""
    cur, old = age(proj, rel)
    st = load_state(proj)
    st["files"][rel]["mode"] = "keep"
    save_state(proj, st)
    return cur, old


def row(r, rel=WF):
    return next(x for x in json.loads(r.stdout)["files"] if x["path"] == rel)


def test_keep_mode_missing_file_is_never_created(proj):
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    st["files"][WF]["base"] = "# old\n"
    save_state(proj, st)
    (proj / WF).unlink()
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert not (proj / WF).exists()
    assert row(r)["action"] == "keep"
    assert load_state(proj)["files"][WF]["mode"] == "keep"


def test_keep_file_disk_equals_base_updates_and_stays_keep(proj):
    cur, _ = keepify(proj)
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row(r)["action"] == "update"
    assert (proj / WF).read_text(encoding="utf8") == cur
    f = load_state(proj)["files"][WF]
    assert f["mode"] == "keep" and f["base"] == cur


def test_keep_file_clean_merge_keeps_both_changes(proj):
    cur, old = keepify(proj)
    (proj / WF).write_text(old + "\nMY TAIL\n", encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row(r)["action"] == "merge"
    text = (proj / WF).read_text(encoding="utf8")
    assert "MY TAIL" in text and not text.startswith("# OLD HEADER")
    assert load_state(proj)["files"][WF]["mode"] == "keep"


def test_keep_file_unchanged_template_is_not_touched(proj):
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    save_state(proj, st)
    (proj / WF).write_text("# mine\n", encoding="utf8")
    before = snap(proj)
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0 and row(r)["action"] == "unchanged"
    assert snap(proj) == before


def test_keep_file_conflict_blocks_and_reports_mode(proj):
    conflict(proj)
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    save_state(proj, st)
    before = snap(proj)
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 3
    assert snap(proj) == before
    x = row(r)
    assert x["action"] == "conflict" and x["mode"] == "keep" and "template_diff" in x
    assert "<<<<<<<" not in (proj / WF).read_text(encoding="utf8")


def test_keep_file_conflict_with_keep_flag_then_current(proj):
    conflict(proj)
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    save_state(proj, st)
    disk = (proj / WF).read_bytes()
    r = run("--update", "--write", "--keep", WF, "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert (proj / WF).read_bytes() == disk
    assert load_state(proj)["files"][WF]["mode"] == "keep"
    r2 = run("--update", "--root", str(proj), "--json")
    assert json.loads(r2.stdout)["status"] == "current"


def test_keep_file_conflict_with_overwrite_flag_makes_it_owned(proj):
    cur, _ = conflict(proj)
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    save_state(proj, st)
    mine = (proj / WF).read_text(encoding="utf8")
    r = run("--update", "--write", "--overwrite", WF, "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert (proj / WF).read_text(encoding="utf8") == cur
    baks = list(proj.glob(WF + ".bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf8") == mine
    assert load_state(proj)["files"][WF]["mode"] == "owned"


def test_keep_flag_on_clean_merge_of_keep_file_declines_it(proj):
    cur, old = keepify(proj)
    r = run("--update", "--write", "--keep", WF, "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row(r)["action"] == "keep"
    assert (proj / WF).read_text(encoding="utf8") == old
    assert load_state(proj)["files"][WF]["base"] == cur
    assert json.loads(run("--update", "--root", str(proj), "--json").stdout)["status"] == "current"


def test_overwrite_flag_on_same_keep_file_only_changes_mode(proj):
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    save_state(proj, st)
    before = (proj / WF).read_bytes()
    r = run("--update", "--write", "--overwrite", WF, "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row(r)["action"] == "same"
    assert (proj / WF).read_bytes() == before and not list(proj.glob(WF + ".bak-*"))
    assert load_state(proj)["files"][WF]["mode"] == "owned"


def test_keep_file_not_utf8_is_keep_not_conflict(proj):
    st = load_state(proj)
    st["files"][WF]["mode"] = "keep"
    save_state(proj, st)
    (proj / WF).write_bytes(b"\xff\xfe not utf8\n")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row(r)["action"] == "keep"
    assert (proj / WF).read_bytes() == b"\xff\xfe not utf8\n"


def test_keep_file_symlink_refused_nothing_written(proj, tmp_path):
    cur, old = keepify(proj)
    outside = tmp_path / "outside.md"
    outside.write_text("not ours\n", encoding="utf8")
    (proj / WF).unlink()
    (proj / WF).symlink_to(outside)
    state_before = (proj / STATE).read_bytes()
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 2, r.output
    assert outside.read_bytes() == b"not ours\n"
    assert (proj / WF).is_symlink()
    assert (proj / STATE).read_bytes() == state_before


def test_keep_file_template_diff_withheld_for_hard_secret(proj):
    cur, old = keepify(proj)
    tok = "AKIA" + "ABCDEFGHIJKLMNOP"
    base = old.rstrip("\n") + f"\nAWS_KEY={tok}\n"
    st = load_state(proj)
    st["files"][WF]["base"] = base
    save_state(proj, st)
    (proj / WF).write_text(base, encoding="utf8")
    r = run("--update", "--root", str(proj), "--keep", WF, "--json")
    assert r.exit_code == 0, r.output
    assert tok not in r.output
    x = row(r)
    assert x["action"] == "keep" and x["mode"] == "keep"
    assert x.get("diff_withheld") is True and "template_diff" not in x


# ---------------------------------------------------------------- conflicts

def conflict(proj):
    cur, old = age(proj, WF)
    (proj / WF).write_text("# MY OWN HEADER\n" + old.split("\n", 1)[1], encoding="utf8")
    return cur, old


def test_conflict_writes_nothing_exits_nonzero_and_lists_files(proj):
    conflict(proj)
    # another file would merge cleanly: it must not be written either
    st = load_state(proj)
    st["files"][LS]["base"] = "# OLD LESSONS\n" + st["files"][LS]["base"].split("\n", 1)[1]
    save_state(proj, st)
    (proj / LS).write_text(st["files"][LS]["base"], encoding="utf8")
    before = snap(proj)
    r = run("--update", "--write", "--root", str(proj))
    assert r.exit_code == 3
    assert WF in r.output and "nothing written" in r.output
    assert snap(proj) == before
    assert not list(proj.rglob("*.bak-*"))


def test_conflict_json_lists_conflicts_and_template_diff(proj):
    conflict(proj)
    r = run("--update", "--root", str(proj), "--json")
    assert r.exit_code == 3
    out = json.loads(r.stdout)
    assert out["status"] == "conflict" and out["conflicts"] == [WF]
    row = next(x for x in out["files"] if x["path"] == WF)
    assert row["action"] == "conflict" and "-# OLD HEADER" in row["template_diff"]
    assert "diff" not in row and "<<<<<<<" not in r.stdout
    assert WF not in out["write"]


def test_conflict_preview_also_exits_3(proj):
    conflict(proj)
    assert run("--update", "--root", str(proj)).exit_code == 3


def test_conflict_resolved_with_keep(proj):
    cur, _ = conflict(proj)
    r = run("--update", "--write", "--root", str(proj), "--keep", WF)
    assert r.exit_code == 0, r.output
    assert (proj / WF).read_text(encoding="utf8").startswith("# MY OWN HEADER")
    f = load_state(proj)["files"][WF]
    assert f["mode"] == "owned" and f["base"] == cur   # ADR-035: mode unchanged, base becomes the new render
    assert load_state(proj)["version"] == S.PROCESS_VERSION
    assert json.loads(run("--update", "--root", str(proj), "--json").stdout)["status"] == "current"


def test_conflict_resolved_with_overwrite_writes_backup(proj):
    cur, _ = conflict(proj)
    mine = (proj / WF).read_text(encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--overwrite", WF, "--json")
    assert r.exit_code == 0, r.output
    assert (proj / WF).read_text(encoding="utf8") == cur
    out = json.loads(r.stdout)
    assert any(b.startswith(WF + ".bak-") for b in out["backups"])
    bak = next(b for b in out["backups"] if b.startswith(WF + ".bak-"))
    assert (proj / bak).read_text(encoding="utf8") == mine


def test_overwrite_and_keep_for_same_path_rejected(proj):
    conflict(proj)
    assert run("--update", "--root", str(proj), "--keep", WF, "--overwrite", WF).exit_code == 2


def test_overwrite_of_non_conflict_rejected(proj):
    r = run("--update", "--root", str(proj), "--overwrite", "CLAUDE.md")
    assert r.exit_code == 2 and "not a conflict" in r.output


def test_choice_for_state_file_rejected(proj):
    assert run("--update", "--root", str(proj), "--overwrite", STATE).exit_code == 2


def test_vanished_file_is_a_conflict(proj):
    age(proj, WF)
    (proj / WF).unlink()
    r = run("--update", "--root", str(proj), "--json")
    assert r.exit_code == 3 and json.loads(r.stdout)["conflicts"] == [WF]


# ---------------------------------------------------------------- backups and filter

def test_bak_files_are_excluded_by_filter():
    assert filters.is_denied_path(WF + ".bak-20261112T100000")
    assert filters.is_denied_path("docs/todo/INDEX.md.bak-1")
    assert not filters.is_denied_path("docs/lessons.md")


def test_overwrite_backup_name_matches_filter(proj):
    conflict(proj)
    r = run("--update", "--write", "--root", str(proj), "--overwrite", WF, "--json")
    for b in json.loads(r.stdout)["backups"]:
        assert filters.is_denied_path(b)


# ---------------------------------------------------------------- ADR-023 guarantees

def test_symlink_target_in_project_refused_nothing_written(proj, tmp_path):
    age(proj, WF)
    outside = tmp_path / "outside.md"
    outside.write_text("secret-ish\n", encoding="utf8")
    (proj / WF).unlink()
    (proj / WF).symlink_to(outside)
    before_out = outside.read_bytes()
    r = run("--update", "--write", "--root", str(proj))
    assert r.exit_code in (2, 3)
    assert outside.read_bytes() == before_out
    assert load_state(proj)["version"] == S.PROCESS_VERSION - 1


def test_symlinked_parent_dir_refused(proj, tmp_path):
    age(proj, WF)
    real = tmp_path / "elsewhere"
    (proj / ".claude" / "rules").rename(real)
    (proj / ".claude" / "rules").symlink_to(real)
    before = {p.name: p.read_bytes() for p in real.iterdir()}
    r = run("--update", "--write", "--root", str(proj))
    assert r.exit_code in (2, 3)
    assert {p.name: p.read_bytes() for p in real.iterdir()} == before


def test_folder_changed_after_plan_exits_3_and_writes_nothing(proj, monkeypatch):
    age(proj, WF)
    real = UP.execute

    def racing(root, up):
        (Path(root) / WF).write_text("# changed meanwhile\n", encoding="utf8")
        return real(root, up)

    monkeypatch.setattr(UP, "execute", racing)
    r = run("--update", "--write", "--root", str(proj))
    assert r.exit_code == 3
    assert (proj / WF).read_text(encoding="utf8") == "# changed meanwhile\n"
    assert load_state(proj)["version"] == S.PROCESS_VERSION - 1
    assert not list(proj.rglob("*.bak-*"))
    monkeypatch.setattr(UP, "execute", real)


def test_state_symlink_refused(proj, tmp_path):
    real = tmp_path / "st.json"
    real.write_bytes((proj / STATE).read_bytes())
    (proj / STATE).unlink()
    (proj / STATE).symlink_to(real)
    assert run("--update", "--root", str(proj)).exit_code == 2


# ---------------------------------------------------------------- merge3 / secrets / env

def test_merge3_clean_and_conflict():
    text, clean = UP.merge3("a\nB\nc\n", "a\nb\nc\n", "a\nb\nc\nd\n")
    assert clean and text == "a\nB\nc\nd\n"
    text, clean = UP.merge3("a\nX\nc\n", "a\nb\nc\n", "a\nY\nc\n")
    assert not clean and "<<<<<<<" in text


def test_merge3_ignores_hostile_git_env(monkeypatch, tmp_path):
    """A malformed GIT_CONFIG_COUNT makes any git call exit 128 unless merge3 drops `GIT_*` from the environment."""
    monkeypatch.setattr(UP.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "not-a-number")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nope"))
    text, clean = UP.merge3("a\nX\nc\n", "a\nb\nc\n", "a\nY\nc\n")
    assert not clean and "<<<<<<<" in text
    assert not list(tmp_path.glob("myvec-merge-*"))


def test_merge3_leaves_no_temp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(UP.tempfile, "tempdir", str(tmp_path))
    before = set(tmp_path.iterdir())
    UP.merge3("a\n", "a\n", "b\n")
    assert set(tmp_path.iterdir()) == before


def test_diff_withheld_when_disk_file_has_hard_secret(proj):
    cur, old = age(proj, WF)
    key = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"
    (proj / WF).write_text(old + "\n" + key, encoding="utf8")
    r = run("--update", "--root", str(proj), "--json")
    assert "BEGIN RSA" not in r.output
    row = next(x for x in json.loads(r.stdout)["files"] if x["path"] == WF)
    assert row.get("diff_withheld") is True and "diff" not in row


def test_diff_masks_literal_secret_values(proj):
    """A soft secret (not a hard-secret format) is masked with `•••` in the diff and the diff is not withheld."""
    cur, old = age(proj, WF)
    lit = "hunter2longvalue"
    # the secret sits 3 lines under the changed first line: inside the diff context, merge stays clean
    lines = old.split("\n")
    lines.insert(3, f'password = "{lit}"')
    (proj / WF).write_text("\n".join(lines), encoding="utf8")
    r = run("--update", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert lit not in r.output
    row = next(x for x in json.loads(r.stdout)["files"] if x["path"] == WF)
    assert "diff_withheld" not in row and "•••" in row["diff"]


def test_diff_withheld_for_hard_secret_token(proj):
    cur, old = age(proj, WF)
    tok = "AKIA" + "ABCDEFGHIJKLMNOP"
    (proj / WF).write_text(old.rstrip("\n") + f"\nAWS_KEY={tok}\n", encoding="utf8")
    r = run("--update", "--root", str(proj), "--json")
    assert tok not in r.output
    row = next(x for x in json.loads(r.stdout)["files"] if x["path"] == WF)
    assert row.get("diff_withheld") is True and "diff" not in row


def test_edit_during_planning_is_not_lost(proj, monkeypatch):
    """The fingerprint comes from the bytes that were merged: an edit made while the plan is built exits 3."""
    _, old = age(proj, WF)
    mine = old.rstrip("\n") + "\n\n## Our project rule\n\nAlways squash.\n"
    (proj / WF).write_text(mine, encoding="utf8")
    real = UP.merge3

    def racing(ours, base, theirs):
        out = real(ours, base, theirs)
        (proj / WF).write_text(mine + "\nedited meanwhile\n", encoding="utf8")
        return out

    monkeypatch.setattr(UP, "merge3", racing)
    r = run("--update", "--write", "--root", str(proj))
    monkeypatch.setattr(UP, "merge3", real)
    assert r.exit_code == 3
    assert (proj / WF).read_text(encoding="utf8").endswith("edited meanwhile\n")
    assert not list(proj.rglob("*.bak-*"))


# ---------------------------------------------------------------- plan step 4: file kinds and state stability

def row_of(r, rel):
    return next(x for x in json.loads(r.stdout)["files"] if x["path"] == rel)


def test_new_template_file_absent_from_state_and_disk_is_created(proj):
    st = load_state(proj)
    del st["files"][LS]
    save_state(proj, st)
    (proj / LS).unlink()
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row_of(r, LS)["action"] == "create" and LS in json.loads(r.stdout)["write"]
    assert (proj / LS).is_file() and LS in load_state(proj)["files"]


def test_new_template_file_present_and_equal_is_same(proj):
    st = load_state(proj)
    del st["files"][LS]
    save_state(proj, st)
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row_of(r, LS)["action"] == "same" and LS not in json.loads(r.stdout)["write"]
    assert LS in load_state(proj)["files"]


def test_new_template_file_present_but_different_conflicts(proj):
    st = load_state(proj)
    del st["files"][LS]
    save_state(proj, st)
    (proj / LS).write_text("# own lessons\n", encoding="utf8")
    before = snap(proj)
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 3 and json.loads(r.stdout)["conflicts"] == [LS]
    assert snap(proj) == before
    assert run("--update", "--write", "--root", str(proj), "--keep", LS).exit_code == 0
    assert (proj / LS).read_text(encoding="utf8") == "# own lessons\n"


def test_docs_todo_file_absent_from_state_is_skipped_not_conflict(proj):
    rel = "docs/todo/ticket-template.md"
    st = load_state(proj)
    del st["files"][rel]
    save_state(proj, st)
    (proj / rel).write_text("# my own template\n", encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row_of(r, rel)["action"] == "skip"
    assert (proj / rel).read_text(encoding="utf8") == "# my own template\n"
    assert rel not in load_state(proj)["files"]


def test_orphan_state_entry_is_dropped_and_file_stays_on_disk(proj):
    st = load_state(proj)
    st["files"]["docs/gone.md"] = {"mode": "owned", "base": "# gone\n"}
    save_state(proj, st)
    (proj / "docs" / "gone.md").write_text("# gone\n", encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert row_of(r, "docs/gone.md")["action"] == "orphan" and "docs/gone.md" not in out["write"]
    assert "docs/gone.md" not in load_state(proj)["files"]
    assert (proj / "docs" / "gone.md").read_text(encoding="utf8") == "# gone\n"


def test_repeat_update_leaves_state_byte_identical(proj):
    age(proj, WF)
    assert run("--update", "--write", "--root", str(proj)).exit_code == 0
    before = snap(proj)
    state_bytes = (proj / STATE).read_bytes()
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert out["status"] == "current" and out["write"] == []
    assert all(x["action"] in ("same", "unchanged") for x in out["files"])
    assert (proj / STATE).read_bytes() == state_bytes
    assert snap(proj) == before


def test_index_md_template_change_merges_and_generated_block_survives(proj):
    rel = "docs/todo/INDEX.md"
    cur, old = age(proj, rel)
    disk = (proj / rel).read_text(encoding="utf8")
    disk = disk.replace("<!-- generated:end -->", "- **[E9 Own](E9/index.md)** - 5%\n<!-- generated:end -->")
    disk += "\n## Our notes\n\nkeep me\n"
    (proj / rel).write_text(disk, encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert row_of(r, rel)["action"] in ("update", "merge")
    text = (proj / rel).read_text(encoding="utf8")
    assert text.startswith(cur.split("\n", 1)[0] + "\n") and "# OLD HEADER" not in text
    assert "- **[E9 Own](E9/index.md)** - 5%" in text and "keep me" in text
    assert "<<<<<<<" not in text
    assert json.loads(run("--update", "--root", str(proj), "--json").stdout)["status"] == "current"


def test_appended_claude_md_takes_process_change_and_keeps_project_header(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# My project\n\nOwn header text.\n", encoding="utf8")
    assert run("--answers", str(MIN), "--root", str(root), "--append", "CLAUDE.md").exit_code == 0
    st = load_state(root)
    assert st["files"]["CLAUDE.md"]["mode"] == "append"
    # simulate an older template: one line of the process section differs in base and on disk
    wf_line = next(l for l in st["files"]["CLAUDE.md"]["base"].splitlines() if l.startswith("- ") and "Tests" in l)
    old_line = wf_line + " (old wording)"
    st["files"]["CLAUDE.md"]["base"] = st["files"]["CLAUDE.md"]["base"].replace(wf_line, old_line)
    st["version"] = S.PROCESS_VERSION - 1
    save_state(root, st)
    disk = (root / "CLAUDE.md").read_text(encoding="utf8")
    assert wf_line in disk
    (root / "CLAUDE.md").write_text(disk.replace(wf_line, old_line), encoding="utf8")
    r = run("--update", "--write", "--root", str(root), "--json")
    assert r.exit_code == 0, r.output
    text = (root / "CLAUDE.md").read_text(encoding="utf8")
    assert text.startswith("# My project\n\nOwn header text.\n")
    assert wf_line in text and "(old wording)" not in text and "<<<<<<<" not in text
    assert load_state(root)["files"]["CLAUDE.md"]["mode"] == "append"
    assert json.loads(run("--update", "--root", str(root), "--json").stdout)["status"] == "current"


# ------------------------------------------------------------ search rule on a v2 state (E8-12, ADR-041)

def _v2(proj):
    """A project rendered before the search rule existed: no entry in the state, no file, version 2."""
    st = load_state(proj)
    st["files"].pop(S.SEARCH_RULE)
    st["version"] = S.PROCESS_VERSION - 1
    save_state(proj, st)
    (proj / S.SEARCH_RULE).unlink()


def test_update_on_v2_state_plans_create_for_the_search_rule(proj):
    _v2(proj)
    r = run("--update", "--root", str(proj), "--json")
    assert r.exit_code == 0, r.output
    assert next(x for x in json.loads(r.stdout)["files"] if x["path"] == S.SEARCH_RULE)["action"] == "create"
    assert not (proj / S.SEARCH_RULE).exists()


def test_update_write_on_v2_state_creates_the_search_rule(proj):
    _v2(proj)
    r = run("--update", "--write", "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert (proj / S.SEARCH_RULE).read_text(encoding="utf8") == S._read_template(S.SEARCH_RULE)
    assert S.SEARCH_RULE in load_state(proj)["files"]


def test_update_with_own_search_rule_is_conflict_and_keep_resolves(proj):
    _v2(proj)
    (proj / S.SEARCH_RULE).write_text("# our own search rule\n", encoding="utf8")
    r = run("--update", "--write", "--root", str(proj), "--json")
    assert r.exit_code == 3, r.output
    assert (proj / S.SEARCH_RULE).read_text(encoding="utf8") == "# our own search rule\n"
    r = run("--update", "--write", "--keep", S.SEARCH_RULE, "--root", str(proj))
    assert r.exit_code == 0, r.output
    assert (proj / S.SEARCH_RULE).read_text(encoding="utf8") == "# our own search rule\n"
    assert load_state(proj)["version"] == S.PROCESS_VERSION
