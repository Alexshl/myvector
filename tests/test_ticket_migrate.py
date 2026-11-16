"""`myvec ticket migrate` (E5-01, ADR-024): детектор, мапи, бриф, --check, --fix. Фікстури синтетичні, у tmp_path."""
import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from myvec import ticketmigrate as M
from myvec import ticketops as ops
from myvec import tickets as T
from myvec.ticketcli import ticket_app

runner = CliRunner()
SECRET = "AKIAABCDEFGHIJKLMNOP"


def w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf8")
    return p


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


GOAL_RU = "## За один заход\n\nSomething.\n\n## Контекст\n\nX\n\n## Acceptance criteria\n\n- [ ] a\n\n## Log\n"


def near_canon(root: Path) -> Path:
    """project-a-подібна: epic замість parent, групи лише з title, емодзі, розділи російською."""
    w(root, "docs/todo/ticket-template.md", "---\nid:\n---\n\n## За один заход\n")
    w(root, "docs/todo/E1-alpha/index.md", "---\ntitle: Alpha\n---\n\n# Alpha\n\nГрупа.\n")
    w(root, "docs/todo/E2-beta/index.md", "# Beta without a header\n")
    for i in (1, 2):
        w(root, f"docs/todo/E1-alpha/E1-0{i}-t.md",
          f"---\nid: E1-0{i}\ntitle: T{i}\nstatus: ⬜\nepic: E1\nsize: S\ndeps: []\nblocks: []\nupdated: 2026-11-09\n---\n\n"
          + GOAL_RU)
    return root


def single_file(root: Path) -> Path:
    w(root, "site/docs/todo.md",
      "# TODO\n\n## Сделано\n\n- [x] old\n\n## Backlog\n\n### T1. First task\n\n- [ ] a\n- [x] b\n\n### T2. Second\n\n- [ ] c\n")
    w(root, "site-cloud/docs/todo.md", "### T1. Copy\n- [ ] z\n")
    w(root, ".myvector.toml", 'name = "inv"\nexclude = ["site-cloud"]\n')
    subprocess.run(["git", "init", "-q", str(root / "site")], check=True)
    return root


def frontmatter_no_id(root: Path) -> Path:
    base = "site/docs/todo"
    stat = ["done", "tested", "wont-do", "planning", "done (see PR)", "weird-state"]
    for i, st in enumerate(stat):
        w(root, f"{base}/{'admin' if i % 2 else 'services'}/{i:02d}-slug.md",
          f"---\nstatus: {st}\nblocked_by: —\nwave: 1\nestimate: M\n---\n\n## Критерии приёмки\n\n- [ ] a\n")
    w(root, f"{base}/00-no-header.md", "# plain notes\n")
    w(root, f"{base}/node_modules/x/01.md", "---\nstatus: done\n---\n")
    w(root, f"{base}/../vendor/todo.md", "- [ ] vendored\n")
    return root


def canon(root: Path) -> Path:
    w(root, "docs/todo/E1-a/index.md",
      "---\nid: E1\ntitle: A\nkind: group\nstatus: todo\n---\n\n# A\n")
    w(root, "docs/todo/E1-a/E1-01-x.md",
      "---\nid: E1-01\ntitle: X\nkind: ticket\nstatus: todo\nparent: E1\nsize: S\ndeps: []\nblocks: []\nupdated: 2026-11-09\n---\n\n"
      "## In one pass\n\nGoal.\n\n## Acceptance criteria\n\n- [ ] a\n\n## Log\n")
    return root


# ---------- detect ----------

def test_detect_near_canon(tmp_path):
    near_canon(tmp_path)
    d = M.detect(tmp_path, "docs/todo")
    assert d.kind == "near_canon" and d.status_form == "emoji" and d.lang == "ru"
    assert (d.n_md, d.n_with_id, d.n_legacy_epic, d.n_legacy_groups) == (4, 2, 2, 2)
    assert d.keys["epic"] == 2 and "## За один заход" in d.headings
    assert d.git_root is None


def test_detect_single_file_in_git_subrepo_and_exclude(tmp_path):
    single_file(tmp_path)
    assert M.find_candidates(tmp_path) == ["site/docs/todo.md"]   # site-cloud is excluded by the marker
    d = M.detect(tmp_path, "site/docs/todo.md")
    assert d.kind == "single_file" and d.n_items == 2 and d.n_boxes == 4
    assert d.git_root == "site"
    assert d.status_form == "none"
    assert "### T<n>." in d.headings
    assert not any("First task" in h for h in d.headings)


def test_detect_frontmatter_skips_node_modules_and_vendor(tmp_path):
    frontmatter_no_id(tmp_path)
    assert M.find_candidates(tmp_path) == ["site/docs/todo"]
    d = M.detect(tmp_path, "site/docs/todo")
    assert d.kind == "frontmatter" and d.n_with_id == 0 and d.status_form == "keys"
    assert d.n_md == 7 and d.n_frontmatter == 6 and d.lang == "ru"
    assert d.estimate_is_size


def test_detect_canon(tmp_path):
    canon(tmp_path)
    d = M.detect(tmp_path, "docs/todo")
    assert d.kind == "canon" and d.lang == "en"
    issues, n = M.migration_check(tmp_path, "docs/todo")
    assert M.left_over(issues) == [] and n == 1


def test_denylist_skipped(tmp_path):
    w(tmp_path, "docs/todo/secrets/x.md", "---\nid: a\n---\n")   # not a candidate by itself
    w(tmp_path, "build/docs/todo/x.md", "---\nstatus: todo\n---\n")
    w(tmp_path, ".worktrees/t/docs/todo/x.md", "---\nstatus: todo\n---\n")
    assert M.find_candidates(tmp_path) == ["docs/todo"]


def test_secret_in_status_and_heading_masked_or_dropped(tmp_path):
    w(tmp_path, "docs/todo/a.md", f"---\nstatus: password = \"hunter2secret\"\n---\n\n## token: {SECRET}\n\n## password = \"hunter2secret\"\n")
    d = M.detect(tmp_path, "docs/todo")
    blob = json.dumps(d.to_json(), ensure_ascii=False)
    assert SECRET not in blob and "hunter2secret" not in blob
    brief = M.render_brief(d, "p")
    assert SECRET not in brief and "hunter2secret" not in brief


# ---------- мапи ----------

def test_field_and_status_maps(tmp_path):
    frontmatter_no_id(tmp_path)
    d = M.detect(tmp_path, "site/docs/todo")
    fm = {f["key"]: f for f in M.field_map(d)}
    assert fm["estimate"]["target"] == "size" and fm["blocked_by"]["target"] == "deps"
    assert fm["wave"]["target"] == "local" and fm["status"]["target"] == "="
    sm = {s["raw"]: s["target"] for s in M.status_map(d)}
    assert sm == {"done": "done", "tested": "review", "wont-do": "cancelled", "planning": "todo",
                  "done (see PR)": "done", "weird-state": "?"}


# ---------- бриф ----------

def test_brief_contents_and_no_body_text(tmp_path):
    frontmatter_no_id(tmp_path)
    w(tmp_path, "site/docs/todo/03-slug.md", "---\nstatus: done\n---\n\nBODYMARK private text\n")
    d = M.detect(tmp_path, "site/docs/todo")
    b = M.render_brief(d, "agp")
    for needle in ("git mv", "`weird-state`", "**?**", "`blocked_by`", "One branch", "`wave`", "migrate --dir site/docs/todo --check",
                   "Do not rewrite `## Log`"):
        assert needle in b, needle
    assert "BODYMARK" not in b
    assert b == M.render_brief(d, "agp")   # deterministic


def test_brief_snapshot_single_file(tmp_path):
    single_file(tmp_path)
    b = M.render_brief(M.detect(tmp_path, "site/docs/todo.md"), "inv")
    assert "format: `single_file`" in b
    assert "task headings (`### T<n>.`): 2; checkboxes: 4" in b
    assert "No statuses found. Use keys" in b
    assert "git root: `site`" in b
    assert "First task" not in b


# ---------- CLI ----------

def run(root, *args):
    return runner.invoke(ticket_app, ["migrate", "--root", str(root), *args])


def test_cli_json_contract(tmp_path):
    near_canon(tmp_path)
    r = run(tmp_path, "--detect", "--json")
    assert r.exit_code == 0
    d = json.loads(r.stdout)
    assert d["recommended_dir"] == "docs/todo"
    c = d["candidates"][0]
    assert {"dir", "kind", "n_md", "n_frontmatter", "n_with_id", "n_canon", "status_form", "lang", "git_root",
            "keys", "statuses", "headings"} <= set(c)


def test_cli_detect_human_and_default(tmp_path):
    near_canon(tmp_path)
    r = run(tmp_path)
    assert r.exit_code == 0 and "near_canon" in r.stdout and "(recommended)" in r.stdout


def test_cli_brief_out_and_ambiguity(tmp_path):
    single_file(tmp_path)
    w(tmp_path, "docs/todo-x.md", "# x\n- [ ] y\n")
    r = run(tmp_path, "--brief")
    assert r.exit_code == 2 and "site/docs/todo.md" in r.output
    out = tmp_path.parent / "brief-out.md"
    r = run(tmp_path, "--brief", "--dir", "site/docs/todo.md", "--out", str(out))
    assert r.exit_code == 0 and out.read_text().startswith("# Migration brief")
    out.unlink()


def test_cli_check_exit_codes(tmp_path):
    near_canon(tmp_path)
    r = run(tmp_path, "--check")
    assert r.exit_code == 1 and "legacy_epic" in r.output and "legacy_group" in r.output
    assert "осталось" in r.stdout   # the fixture tickets are Russian (E5-07: output follows the ticket language)
    c = tmp_path / "c"
    c.mkdir()
    canon(c)
    r = run(c, "--check")
    assert r.exit_code == 0 and "0 left" in r.stdout   # E5-08: no template, the tickets' headings are English


def test_check_reports_not_ticket_no_goal_mixed_and_leftover(tmp_path):
    canon(tmp_path)
    w(tmp_path, "docs/todo/notes.md", "# just notes\n")
    w(tmp_path, "docs/todo/E1-a/E1-02-y.md",
      "---\nid: E1-02\ntitle: Y\nkind: ticket\nstatus: done 2026-11-09\nparent: E1\nsize: S\ndeps: []\nblocks: []\n---\n\n## Log\n")
    w(tmp_path, "docs/todo/E1-a/E1-03-z.md",
      "---\nid: E1-03\ntitle: Z\nkind: ticket\nstatus: ✅ 2026-11-09\nparent: E1\nsize: S\ndeps: []\nblocks: []\n---\n\n## Log\n")
    w(tmp_path, "docs/todo.md", "- [ ] leftover\n")
    issues, _ = M.migration_check(tmp_path, "docs/todo")
    codes = {i.code for i in issues}
    assert {"not_ticket", "no_goal", "mixed_status_form", "leftover_todo"} <= codes


def test_cli_single_file_check_not_done(tmp_path):
    single_file(tmp_path)
    r = run(tmp_path, "--check", "--dir", "site/docs/todo.md")
    assert r.exit_code == 1 and "not_ticket" in r.output


# ---------- fix ----------

def test_fix_dry_run_writes_nothing(tmp_path):
    near_canon(tmp_path)
    before = snapshot(tmp_path)
    r = run(tmp_path, "--fix")
    assert r.exit_code == 0 and "dry-run" in r.stdout and "4 files to fix" in r.stdout
    assert snapshot(tmp_path) == before


def test_fix_write_makes_check_clean_idempotent_and_keeps_bodies(tmp_path):
    near_canon(tmp_path)
    before = snapshot(tmp_path)
    r = run(tmp_path, "--fix", "--write")
    assert r.exit_code == 0 and "fixed: 4 files" in r.stdout
    after = snapshot(tmp_path)

    def body(b: bytes) -> bytes:
        t = b.decode()
        return t.split("\n---\n", 1)[1].encode() if t.startswith("---\n") and "\n---\n" in t else b

    assert body(after["docs/todo/E1-alpha/E1-01-t.md"]) == body(before["docs/todo/E1-alpha/E1-01-t.md"])
    assert body(after["docs/todo/E1-alpha/index.md"]) == body(before["docs/todo/E1-alpha/index.md"])
    assert after["docs/todo/E2-beta/index.md"].endswith(before["docs/todo/E2-beta/index.md"])
    assert after["docs/todo/ticket-template.md"] == before["docs/todo/ticket-template.md"]
    ts = T.load_dir(tmp_path, "docs/todo")
    assert not [i for i in ts.issues if i.code == "legacy_epic"] and not any(t.legacy for t in ts.tickets.values())
    assert ts.tickets["E1"].kind == "group" and ts.tickets["E1"].status_raw == "⬜"
    assert ts.tickets["E2"].title == "beta"
    assert ts.tickets["E1-01"].parent == "E1"
    issues, _ = M.migration_check(tmp_path, "docs/todo")
    assert not [i for i in issues if i.code.startswith("legacy")]
    r = run(tmp_path, "--fix", "--write")
    assert "nothing to fix" in r.stdout
    assert snapshot(tmp_path) == after


def test_fix_group_status_follows_project_form(tmp_path):
    w(tmp_path, "docs/todo/E1-a/index.md", "---\ntitle: A\n---\n")
    w(tmp_path, "docs/todo/E1-a/E1-01-x.md",
      "---\nid: E1-01\ntitle: X\nstatus: todo\nepic: E1\n---\n\n## Log\n")
    M.apply_fixes(tmp_path, M.fix_plan(tmp_path, "docs/todo"))
    assert T.load_dir(tmp_path, "docs/todo").tickets["E1"].status_raw == "todo"


def test_fix_keeps_crlf(tmp_path):
    p = w(tmp_path, "docs/todo/E1-a/E1-01-x.md", "")
    p.write_bytes(b"---\r\nid: E1-01\r\ntitle: X\r\nstatus: todo\r\nepic: E1\r\n---\r\n\r\nbody\r\n")
    M.apply_fixes(tmp_path, M.fix_plan(tmp_path, "docs/todo"))
    assert p.read_bytes() == b"---\r\nid: E1-01\r\ntitle: X\r\nstatus: todo\r\nparent: E1\r\n---\r\n\r\nbody\r\n"


def test_read_only_commands_change_nothing(tmp_path):
    near_canon(tmp_path)
    before = snapshot(tmp_path)
    for args in (("--detect",), ("--detect", "--json"), ("--fix",), ("--check",), ("--brief",)):
        run(tmp_path, *args)
    assert snapshot(tmp_path) == before


# ---------- ru ----------

def test_ru_goal_and_index_language(tmp_path):
    near_canon(tmp_path)
    ts = T.load_dir(tmp_path, "docs/todo")
    assert T.body_sections(ts.tickets["E1-01"].body)["goal"] == "Something."
    assert ops.lang_for(tmp_path / "docs/todo") == "ru"
    M.apply_fixes(tmp_path, M.fix_plan(tmp_path, "docs/todo"))
    ts = T.load_dir(tmp_path, "docs/todo")
    out = ops.render_index(ts, "docs/todo", "ru")
    assert "Общий прогресс" in out


# ---------- review fixes ----------

def _victim(tmp_path):
    """A project with a ticket dir outside it that --fix would edit if the path were accepted."""
    outside = tmp_path / "outside"
    w(outside, "docs/todo/E1-a/E1-01-x.md", "---\nid: E1-01\ntitle: X\nstatus: todo\nepic: E1\n---\n\nbody\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj, outside


def test_dir_outside_root_refused_and_nothing_written(tmp_path):
    proj, outside = _victim(tmp_path)
    before = snapshot(outside)
    for d in ("../outside/docs/todo", str(outside / "docs/todo")):
        r = run(proj, "--fix", "--write", "--dir", d)
        assert r.exit_code == 2, d
    assert snapshot(outside) == before


def test_symlinked_tickets_dir_refused(tmp_path):
    proj, outside = _victim(tmp_path)
    (proj / "docs").mkdir()
    (proj / "docs/todo").symlink_to(outside / "docs/todo")
    before = snapshot(outside)
    r = run(proj, "--fix", "--write", "--dir", "docs/todo")
    assert r.exit_code == 2 and snapshot(outside) == before


def test_module_refuses_symlinks_and_outside(tmp_path):
    import pytest

    proj, outside = _victim(tmp_path)
    (proj / "docs").mkdir()
    (proj / "docs/todo").symlink_to(outside / "docs/todo")
    with pytest.raises(ops.TicketOpError):
        M.fix_plan(proj, "docs/todo")
    with pytest.raises(ops.TicketOpError):
        M.fix_plan(proj, "../outside/docs/todo")
    # a symlinked file and a symlinked subdirectory inside a real dir are skipped
    real = tmp_path / "p2"
    w(real, "docs/todo/E1-a/E1-01-x.md", "---\nid: E1-01\ntitle: X\nstatus: todo\nepic: E1\n---\n")
    (real / "docs/todo/E1-a/link.md").symlink_to(outside / "docs/todo/E1-a/E1-01-x.md")
    (real / "docs/todo/sub").symlink_to(outside / "docs/todo/E1-a")
    before = snapshot(outside)
    plan = M.fix_plan(real, "docs/todo")
    assert [f.path for f in plan] == ["docs/todo/E1-a/E1-01-x.md"]
    # a plan whose target became a symlink or changed is refused at write time
    (real / "docs/todo/E1-a/E1-01-x.md").write_text("changed\n")
    with pytest.raises(ops.TicketOpError):
        M.apply_fixes(real, plan)
    assert snapshot(outside) == before


def test_fix_group_with_empty_id_key_has_no_duplicate(tmp_path):
    w(tmp_path, "docs/todo/E1-a/index.md", "---\nid:\ntitle: A\n---\n\nbody\n")
    w(tmp_path, "docs/todo/E1-a/E1-01-x.md", "---\nid: E1-01\ntitle: X\nstatus: todo\nparent: E1\n---\n\n## Log\n")
    r = run(tmp_path, "--fix", "--write")
    assert r.exit_code == 0
    text = (tmp_path / "docs/todo/E1-a/index.md").read_text()
    assert text.count("id:") == 1 and text.endswith("\nbody\n")
    ts = T.load_dir(tmp_path, "docs/todo")
    assert not [i for i in ts.issues if i.code == "bad_frontmatter"] and ts.tickets["E1"].kind == "group"
    chk = runner.invoke(ticket_app, ["check", "--root", str(tmp_path)])
    assert chk.exit_code == 0, chk.output


def test_fix_index_without_id_and_with_epic_converges_in_one_pass(tmp_path):
    w(tmp_path, "docs/todo/E1-a/index.md", "---\ntitle: A\nepic: E0\n---\n\nbody\n")
    r = run(tmp_path, "--fix", "--write")
    assert r.exit_code == 0
    t = (tmp_path / "docs/todo/E1-a/index.md").read_text()
    assert "parent: E0" in t and "epic:" not in t and "id: E1" in t
    assert "nothing to fix" in run(tmp_path, "--fix").stdout


def test_fix_skips_index_with_kind_ticket(tmp_path):
    w(tmp_path, "docs/todo/E1-a/index.md", "---\ntitle: A\nkind: ticket\n---\n")
    assert M.fix_plan(tmp_path, "docs/todo") == []


def test_brief_out_directory_is_clean_error(tmp_path):
    near_canon(tmp_path)
    r = run(tmp_path, "--brief", "--out", str(tmp_path))
    assert r.exit_code == 1 and "Traceback" not in r.output
