import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from myvec import ticketops as ops
from myvec import tickets as T
from myvec.ticketcli import ticket_app

runner = CliRunner()
REPO = Path(__file__).resolve().parent.parent


def fm(**kw):
    base = dict(kind="ticket", size="S", deps="[]", blocks="[]")
    base.update(kw)
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in base.items()) + "\n---\n\n"


def make(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / "docs/todo" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf8")
    return tmp_path


def tree(tmp_path):
    return make(tmp_path, {
        "E1-a/index.md": fm(id="E1", title="Alpha", kind="group", status="🟦") + "# E1\n\n" + ops.START + "\nold\n" + ops.END + "\n",
        "E1-a/E1-01-x.md": fm(id="E1-01", title="Done one", status="✅ 2026-11-09", parent="E1") + "# x\n",
        "E1-a/E1-02-y.md": fm(id="E1-02", title="Half | pipe", status="🟦", parent="E1") + "## Acceptance criteria\n\n- [x] a\n- [ ] b\n",
        "E1-a/sub/index.md": fm(id="E1S", title="Sub", kind="group", status="⬜", parent="E1"),
        "E1-a/sub/E1S-01-z.md": fm(id="E1S-01", title="Z", status="⬜", parent="E1S"),
        "E1-a/sub/E1S-02-c.md": fm(id="E1S-02", title="Cancelled", status="🚫", parent="E1S"),
        "E2-b/index.md": fm(id="E2", title="Beta (no markers)", kind="group", status="⬜") + "# E2\n",
        "E2-b/E2-10-t.md": fm(id="E2-10", title="Ten", status="⬜", parent="E2"),
        "E2-b/E2-02-t.md": fm(id="E2-02", title="Two", status="⬜", parent="E2"),
        "L-1-loose.md": fm(id="L-1", title="Loose", status="todo"),
    })


def run(root, *args, **kw):
    return runner.invoke(ticket_app, [*args, "--root", str(root)], **kw)


def test_natural_key():
    assert sorted(["E1-10", "E1-2", "E1-1"], key=ops.natural_key) == ["E1-1", "E1-2", "E1-10"]


def test_render_index_tree(tmp_path):
    root = tree(tmp_path)
    ts = T.load_dir(root)
    out = ops.render_index(ts)
    lines = out.splitlines()
    assert lines[0].startswith("Загальний прогрес: ")
    assert "- **[E1 Alpha](E1-a/index.md)** — 50% (1/3 готово)" in lines
    assert "  - ✅ 2026-11-09 [E1-01](E1-a/E1-01-x.md) Done one" in lines
    assert "  - 🟦 [E1-02](E1-a/E1-02-y.md) Half | pipe — 50%" in lines
    assert "  - **[E1S Sub](E1-a/sub/index.md)** — 0% (0/1 готово)" in lines
    assert "    - ⬜ [E1S-01](E1-a/sub/E1S-01-z.md) Z" in lines
    assert "    - 🚫 [E1S-02](E1-a/sub/E1S-02-c.md) Cancelled" in lines
    assert lines.index("- **Без групи**") > lines.index(next(l for l in lines if l.startswith("- **[E2")))
    assert "  - ⬜ [L-1](L-1-loose.md) Loose" in lines
    e2 = [l for l in lines if "E2-" in l and "[E2-" in l]
    assert [l.split("]")[0] for l in e2] == ["  - ⬜ [E2-02", "  - ⬜ [E2-10"]


def test_index_idempotent_and_markers(tmp_path):
    root = tree(tmp_path)
    r = run(root, "index")
    assert r.exit_code == 0, r.output
    d = root / "docs/todo"
    assert (d / "INDEX.md").is_file()
    e1 = (d / "E1-a/index.md").read_text(encoding="utf8")
    assert "old" not in e1 and "| [E1-01](E1-01-x.md) | Done one | ✅ 2026-11-09 | 100% |" in e1
    assert "Half \\| pipe" in e1
    assert (d / "E2-b/index.md").read_text(encoding="utf8").endswith("# E2\n")  # no markers: untouched
    assert (d / "E1-a/sub/index.md").read_text(encoding="utf8").count(ops.START) == 0
    before = {p: p.read_bytes() for p in d.rglob("*.md")}
    assert run(root, "index").exit_code == 0
    assert before == {p: p.read_bytes() for p in d.rglob("*.md")}


def test_check_errors_and_strict(tmp_path):
    root = make(tmp_path, {"a/E1-01-x.md": fm(id="E1-01", title="X", status="bogus", parent="NOPE")})
    r = run(root, "check")
    assert r.exit_code == 1
    assert "ERROR docs/todo/a/E1-01-x.md" in r.output and "[bad_status]" in r.output
    r = run(root, "index")
    assert r.exit_code == 1 and not (root / "docs/todo/INDEX.md").exists()
    ok = make(tmp_path / "ok", {"g/index.md": fm(id="E1", title="G", kind="group", status="⬜"),
                                "g/E1-01-x.md": fm(id="E1-01", title="X", status="⬜", epic="E1")})
    assert run(ok, "check").exit_code == 0
    r = run(ok, "check", "--strict")
    assert r.exit_code == 1 and "WARN" in r.output and "[legacy_epic]" in r.output


def test_new_leaf_group_deps(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--title", "Fresh: one", "--slug", "fresh", "--parent", "E2", "--deps", "E2-02")
    assert r.exit_code == 0, r.output
    p = root / "docs/todo/E2-b/E2-11-fresh.md"
    assert p.is_file() and r.output.strip() == str(p)
    text = p.read_text(encoding="utf8")
    assert "id: E2-11" in text and 'title: "Fresh: one"' in text and "parent: E2" in text
    assert "deps: [E2-02]" in text and "# E2-11 Fresh: one" in text
    assert "blocks: [E2-11]" in (root / "docs/todo/E2-b/E2-02-t.md").read_text(encoding="utf8")
    assert run(root, "new", "--title", "G", "--slug", "grp", "--kind", "group", "--id", "E7").exit_code == 0
    g = root / "docs/todo/E7-grp/index.md"
    assert "kind: group" in g.read_text(encoding="utf8")
    assert run(root, "index").exit_code == 0
    assert run(root, "check", "--strict").exit_code == 0, run(root, "check", "--strict").output


def test_new_errors(tmp_path):
    root = tree(tmp_path)
    assert run(root, "new", "--title", "T", "--slug", "Bad_Slug", "--parent", "E2").exit_code == 1
    assert run(root, "new", "--title", "T", "--slug", "ok", "--parent", "NOPE").exit_code == 1
    assert run(root, "new", "--title", "T", "--slug", "ok", "--kind", "group").exit_code == 1
    assert run(root, "new", "--title", "T", "--slug", "ok", "--parent", "E2", "--id", "E2-02").exit_code == 1
    assert run(root, "new", "--title", "T", "--slug", "a", "--parent", "E2", "--id", "E2-50").exit_code == 0
    assert run(root, "new", "--title", "T", "--slug", "a", "--parent", "E2", "--id", "E2-50").exit_code == 1


def test_new_deps_block_format_refuses(tmp_path):
    root = make(tmp_path, {
        "g/index.md": fm(id="E1", title="G", kind="group", status="⬜"),
        "g/E1-01-x.md": "---\nid: E1-01\ntitle: X\nstatus: ⬜\nparent: E1\nblocks:\n  - E1-09\n---\n",
    })
    r = run(root, "new", "--title", "T", "--slug", "n", "--parent", "E1", "--deps", "E1-01")
    assert r.exit_code == 1 and "вручну" in r.output
    assert not list((root / "docs/todo/g").glob("E1-02*"))


def test_log_middle_create_and_secrets(tmp_path):
    root = make(tmp_path, {"E1-01-x.md": fm(id="E1-01", title="X", status="⬜", updated="2020-01-01")
                           + "## Log\n\nold\n\n## Later\n\ntail\n",
                           "E1-02-y.md": fm(id="E1-02", title="Y", status="⬜", updated="2020-01-01") + "# y\n"})
    r = run(root, "log", "E1-01", "did it, token = \"abcdefgh12345\"", "--agent", "coder", "--date", "2026-11-09")
    assert r.exit_code == 0, r.output
    text = (root / "docs/todo/E1-01-x.md").read_text(encoding="utf8")
    assert text.index("### 2026-11-09 coder") < text.index("## Later")
    assert "abcdefgh12345" not in text and "•••" in text and "updated: 2026-11-09" in text
    assert text.endswith("tail\n") and "old\n\n### 2026-11-09 coder\n\ndid it" in text
    r = run(root, "log", "E1-02", "-", "--agent", "tester", "--date", "2026-11-10", input="from stdin\n")
    assert r.exit_code == 0
    t2 = (root / "docs/todo/E1-02-y.md").read_text(encoding="utf8")
    assert t2.endswith("## Log\n\n### 2026-11-10 tester\n\nfrom stdin\n")
    key = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    r = run(root, "log", "E1-02", "-", "--agent", "x", input=key)
    assert r.exit_code == 1 and (root / "docs/todo/E1-02-y.md").read_text(encoding="utf8") == t2
    assert run(root, "log", "NOPE", "t", "--agent", "x").exit_code == 1


def test_append_log_ignores_fenced_heading():
    text = "---\nid: a\n---\n## Log\n\n```\n## Other\n```\n"
    out = ops.append_log(text, "2026-01-01", "a", "hi")
    assert out.endswith("```\n\n### 2026-01-01 a\n\nhi\n")


def test_set_fm_line():
    text = "---\r\nid: a\r\nupdated: 1\r\n---\r\nbody\r\n"
    out = ops.set_fm_line(text, "updated", "2")
    assert out == "---\r\nid: a\r\nupdated: 2\r\n---\r\nbody\r\n"
    assert "branch: x\r\n---" in ops.set_fm_line(text, "branch", "x")


def test_claim(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    r = run(root, "claim", "E1-01")
    assert r.exit_code == 1 and "session" in r.output
    r = run(root, "claim", "E1-01", "--session", "abcdef12-3456")
    assert r.exit_code == 0 and "E1-01 ← abcdef12" in r.output
    assert run(root, "claim", "NOPE", "--session", "s1").exit_code == 1
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "envsess-0001")
    assert run(root, "claim", "E2-02").exit_code == 0
    assert ops.current_claim("envsess-0001") == "E2-02"
    assert ops.current_claim("abcdef12-3456") == "E1-01"
    assert run(root, "claim", "--release").exit_code == 0
    assert ops.current_claim("envsess-0001") is None
    assert ops.read_claims()["envsess-0001"]["project_root"] == str(root.resolve())
    p = ops.claims_path()
    assert p.parent.name == "state" and p.is_file()
    assert len(p.read_text().splitlines()) == 3
    assert json.loads(p.read_text().splitlines()[0])["ticket_id"] == "E1-01"


def test_wrapper_rejects_unknown_command():
    r = subprocess.run([sys.executable, str(REPO / "scripts/tickets.py"), "bogus"], capture_output=True, text=True)
    assert r.returncode == 2


def test_index_creates_index_file_with_header_and_total(tmp_path):
    root = tree(tmp_path)
    assert run(root, "index").exit_code == 0
    idx = (root / "docs/todo/INDEX.md").read_text(encoding="utf8")
    assert idx.startswith("# Тікети") and ops.START in idx and ops.END in idx
    assert "Загальний прогрес: " in idx and "- **Без групи**" in idx


def test_index_preserves_text_outside_markers(tmp_path):
    root = tree(tmp_path)
    idx = root / "docs/todo/INDEX.md"
    idx.write_text("# Мій заголовок\n\nlegend\n\n" + ops.START + "\nstale\n" + ops.END + "\n\nfooter\n", encoding="utf8")
    assert run(root, "index").exit_code == 0
    text = idx.read_text(encoding="utf8")
    assert text.startswith("# Мій заголовок\n\nlegend\n") and text.endswith("footer\n") and "stale" not in text


def test_check_ok_exit_zero_and_summary(tmp_path):
    root = tree(tmp_path)
    r = run(root, "check", "--strict")
    assert r.exit_code == 0, r.output
    assert "0 помилок, 0 попереджень" in r.output


def test_check_missing_dir_fails(tmp_path):
    assert run(tmp_path, "check").exit_code == 1


def test_new_file_name_and_template_fields(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--title", "Third", "--slug", "third-one", "--parent", "E1", "--size", "m")
    assert r.exit_code == 0, r.output
    p = root / "docs/todo/E1-a/E1-03-third-one.md"
    assert p.is_file()
    text = p.read_text(encoding="utf8")
    for frag in ("id: E1-03", "title: Third", "kind: ticket", "status: ⬜", "parent: E1", "size: M",
                 "deps: []", "blocks: []", "## Acceptance criteria", "## Log"):
        assert frag in text
    import datetime
    assert f"updated: {datetime.date.today().isoformat()}" in text
    assert T.load_dir(root).tickets["E1-03"].parent == "E1"


def test_new_uses_dir_template_when_present(tmp_path):
    root = tree(tmp_path)
    (root / "docs/todo/ticket-template.md").write_text(
        "---\nid: X\ntitle: X\nkind: ticket\nstatus: ⬜\nparent: X\nsize: S\ndeps: []\nblocks: []\nupdated: 2000-01-01\n---\n\n# X X\n\nCUSTOM BODY\n",
        encoding="utf8")
    assert run(root, "new", "--title", "T", "--slug", "t", "--parent", "E2").exit_code == 0
    text = (root / "docs/todo/E2-b/E2-11-t.md").read_text(encoding="utf8")
    assert "CUSTOM BODY" in text and "# E2-11 T" in text and "parent: E2" in text


def test_new_group_nested_under_group(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--title", "Nested", "--slug", "nest", "--kind", "group", "--id", "E1N", "--parent", "E1")
    assert r.exit_code == 0, r.output
    p = root / "docs/todo/E1-a/E1N-nest/index.md"
    assert p.is_file() and "parent: E1" in p.read_text(encoding="utf8")
    assert ops.START in p.read_text(encoding="utf8")


def test_new_parent_must_be_group(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--title", "T", "--slug", "t", "--parent", "E1-01")
    assert r.exit_code == 1 and "group" in r.output


def test_new_rejects_bad_size_kind_and_missing_dep(tmp_path):
    root = tree(tmp_path)
    base = ["new", "--title", "T", "--slug", "t", "--parent", "E2"]
    assert run(root, *base, "--size", "XL").exit_code == 1
    assert run(root, *base, "--kind", "epic").exit_code == 1
    assert run(root, *base, "--deps", "NOPE-1").exit_code == 1
    assert run(tmp_path / "x", "new", "--title", "T", "--slug", "t", "--id", "A-1").exit_code == 1
    assert not list((root / "docs/todo/E2-b").glob("E2-11*"))


def test_new_without_parent_requires_id(tmp_path):
    root = tree(tmp_path)
    assert run(root, "new", "--title", "T", "--slug", "t").exit_code == 1
    assert run(root, "new", "--title", "T", "--slug", "t", "--id", "L-2").exit_code == 0
    assert (root / "docs/todo/L-2-t.md").is_file()


def test_new_deps_multiple_symmetric_and_check_green(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--title", "T", "--slug", "t", "--parent", "E2", "--deps", "E2-02, E1-01")
    assert r.exit_code == 0, r.output
    assert "blocks: [E2-11]" in (root / "docs/todo/E2-b/E2-02-t.md").read_text(encoding="utf8")
    assert "E2-11" in (root / "docs/todo/E1-a/E1-01-x.md").read_text(encoding="utf8")
    assert run(root, "check", "--strict").exit_code == 0


def test_log_default_date_today_and_stdin_without_arg(tmp_path):
    import datetime
    root = tree(tmp_path)
    r = run(root, "log", "E2-02", "--agent", "tester", input="piped text\n")
    assert r.exit_code == 0, r.output
    text = (root / "docs/todo/E2-b/E2-02-t.md").read_text(encoding="utf8")
    today = datetime.date.today().isoformat()
    assert f"### {today} tester\n\npiped text\n" in text and f"updated: {today}" in text


def test_log_rejects_bad_date_and_empty_text(tmp_path):
    root = tree(tmp_path)
    p = root / "docs/todo/E2-b/E2-02-t.md"
    before = p.read_text(encoding="utf8")
    assert run(root, "log", "E2-02", "x", "--agent", "a", "--date", "09.11.2026").exit_code == 1
    assert run(root, "log", "E2-02", "   ", "--agent", "a").exit_code == 1
    assert p.read_text(encoding="utf8") == before


def test_log_refuses_known_token_format(tmp_path):
    root = tree(tmp_path)
    p = root / "docs/todo/E2-b/E2-02-t.md"
    before = p.read_text(encoding="utf8")
    r = run(root, "log", "E2-02", "key AKIAABCDEFGHIJKLMNOP here", "--agent", "a")
    assert r.exit_code == 1 and p.read_text(encoding="utf8") == before
    assert "AKIAABCDEFGHIJKLMNOP" not in r.output


def test_claim_record_fields_and_release_without_id(tmp_path, monkeypatch):
    root = tree(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert run(root, "claim", "--session", "s-1").exit_code == 1  # no ID, no --release
    assert run(root, "claim", "E1-01", "--session", "s-1").exit_code == 0
    rec = ops.read_claims()["s-1"]
    assert {"ts", "session_id", "ticket_id", "project_root", "git_remote", "branch", "cwd"} <= set(rec)
    assert rec["ticket_id"] == "E1-01"
    r = run(root, "claim", "--release", "--session", "s-1")
    assert r.exit_code == 0 and ops.current_claim("s-1") is None
    assert ops.read_claims()["s-1"]["ticket_id"] is None
    assert run(root, "claim", "--release").exit_code == 1  # no session


def test_claims_stored_under_myvec_home_and_last_wins(tmp_path):
    from myvec import config
    root = tree(tmp_path)
    assert ops.read_claims() == {}
    assert ops.current_claim("nobody") is None
    run(root, "claim", "E1-01", "--session", "s-9")
    run(root, "claim", "E2-02", "--session", "s-9")
    p = ops.claims_path()
    assert p == config.home() / "state" / "ticket_claims.jsonl" and p.is_file()
    assert ops.current_claim("s-9") == "E2-02"
    with open(p, "a", encoding="utf8") as f:
        f.write("garbage{\n")
    assert ops.current_claim("s-9") == "E2-02"


def test_wrapper_usage_without_args_and_unknown():
    r = subprocess.run([sys.executable, str(REPO / "scripts/tickets.py")], capture_output=True, text=True)
    assert r.returncode == 2 and "check|index" in r.stderr


def test_new_group_with_deps_symmetric(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--kind", "group", "--id", "E9", "--slug", "g", "--title", "G", "--deps", "E1-01")
    assert r.exit_code == 0, r.output
    assert "deps: [E1-01]" in (root / "docs/todo/E9-g/index.md").read_text(encoding="utf8")
    assert "blocks: [E9]" in (root / "docs/todo/E1-a/E1-01-x.md").read_text(encoding="utf8")
    assert run(root, "check", "--strict").exit_code == 0


def test_claim_strips_remote_userinfo(tmp_path):
    assert ops.sanitize_remote("https://user:tok@github.com/x/y.git") == "https://github.com/x/y.git"
    assert ops.sanitize_remote("https://tok@github.com/x/y.git") == "https://github.com/x/y.git"
    assert ops.sanitize_remote("git@github.com:x/y.git") == "git@github.com:x/y.git"
    assert ops.sanitize_remote(None) is None
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://user:tok@github.com/x/y.git"], check=True)
    rec = ops.record_claim("s-r", None, root)
    assert rec["git_remote"] == "https://github.com/x/y.git"
    assert "tok" not in ops.claims_path().read_text(encoding="utf8")


def test_new_rejects_newline_in_title(tmp_path):
    root = tree(tmp_path)
    r = run(root, "new", "--title", "A\nstatus: x", "--slug", "t", "--parent", "E2")
    assert r.exit_code != 0
    assert not (root / "docs/todo/E2-b/E2-11-t.md").exists()


def test_claims_ignore_non_str_session_id(tmp_path):
    p = ops.claims_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"session_id": ["x"], "ticket_id": "A"}\n{"session_id": 5}\n', encoding="utf8")
    assert ops.read_claims() == {}


def test_write_atomic_symlink_and_cleanup(tmp_path, monkeypatch):
    import os
    import pytest
    target = tmp_path / "real.md"
    target.write_text("x", encoding="utf8")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    with pytest.raises(ops.TicketOpError):
        ops.write_atomic(link, "y")
    assert target.read_text(encoding="utf8") == "x"
    f = tmp_path / "f.md"
    real_replace = os.replace

    def boom(*a):
        raise OSError("boom")
    monkeypatch.setattr(ops.os, "replace", boom)
    with pytest.raises(OSError):
        ops.write_atomic(f, "z")
    monkeypatch.setattr(ops.os, "replace", real_replace)
    assert list(tmp_path.glob("*.tmp*")) == []


def test_append_log_ignores_tilde_fence():
    text = "## Log\n\n~~~\n## Not\n~~~\n"
    out = ops.append_log(text, "2026-11-09", "a", "hi")
    assert out.rstrip().endswith("hi") and out.index("~~~") < out.index("### 2026-11-09 a")


def test_index_on_symlinked_index_reports_error_without_traceback(tmp_path):
    root = tree(tmp_path)
    real = tmp_path / "elsewhere.md"
    real.write_text("# x\n\n" + ops.START + "\n" + ops.END + "\n", encoding="utf8")
    idx = root / "docs/todo/INDEX.md"
    idx.unlink(missing_ok=True)
    idx.symlink_to(real)
    r = run(root, "index")
    assert r.exit_code == 1
    assert "error:" in r.output and "Traceback" not in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)
    assert real.read_text(encoding="utf8").count(ops.START) == 1  # target untouched


def test_log_tokens_line_and_conclusion(tmp_path):
    from myvec import config, db as dbmod
    from test_tokens import _two_runs, set_claude_dir

    base = tmp_path / "proj"
    base.mkdir()
    root = make(base, {"E7-01-x.md": fm(id="E7-01", title="X", status="🟦", updated="2020-01-01") + "## Log\n"})
    cr = tmp_path / "claude_projects"
    cr.mkdir()
    idx = dbmod.open_ready(config.load_config())
    idx.execute("INSERT INTO projects(path, root) VALUES(?, ?)", (str(base), str(base)))
    idx.execute("INSERT INTO tickets(project_path, id, title, status) VALUES(?, 'E7-01', 't', 'todo')", (str(base),))
    idx.commit()
    idx.close()
    set_claude_dir(cr)
    p = root / "docs/todo/E7-01-x.md"
    before = p.read_text(encoding="utf8")
    r = run(root, "log", "E7-01", "--agent", "оркестратор", "--tokens", "coder з'їв найбільше")
    assert r.exit_code == 1 and p.read_text(encoding="utf8") == before          # no usage: nothing written
    _two_runs(cr, str(base))
    r = run(root, "log", "E7-01", "--agent", "оркестратор", "--tokens", "coder з'їв найбільше",
            "--date", "2026-11-10")
    assert r.exit_code == 0, r.output
    text = p.read_text(encoding="utf8")
    assert ("### 2026-11-10 оркестратор\n\ntokens: 540 · output 80 · coder 75% · Sonnet/Opus · medium/high · "
            "запусків 3\n\ncoder з'їв найбільше\n") in text and "updated: 2026-11-10" in text


def test_log_tokens_stdin_conclusion_and_no_scan(tmp_path, monkeypatch):
    from myvec import config, db as dbmod, tokencli
    from test_tokens import _two_runs, set_claude_dir

    base = tmp_path / "proj"
    base.mkdir()
    root = make(base, {"E7-01-x.md": fm(id="E7-01", title="X", status="🟦", updated="2020-01-01") + "## Log\n"})
    cr = tmp_path / "claude_projects"
    cr.mkdir()
    idx = dbmod.open_ready(config.load_config())
    idx.execute("INSERT INTO projects(path, root) VALUES(?, ?)", (str(base), str(base)))
    idx.execute("INSERT INTO tickets(project_path, id, title, status) VALUES(?, 'E7-01', 't', 'todo')", (str(base),))
    idx.commit()
    idx.close()
    set_claude_dir(cr)
    _two_runs(cr, str(base))
    tdb = tokencli.T.open_db()
    tokencli.T.scan(tdb, cr)
    tdb.close()
    scans = []
    monkeypatch.setattr(tokencli.T, "scan", lambda *a, **k: scans.append(1))
    r = run(root, "log", "E7-01", "-", "--agent", "оркестратор", "--tokens", "--no-scan", "--date", "2026-11-10",
            input="висновок зі stdin\n")
    assert r.exit_code == 0, r.output
    assert scans == []
    assert "запусків 3\n\nвисновок зі stdin\n" in (root / "docs/todo/E7-01-x.md").read_text(encoding="utf8")


def test_check_prints_health_and_strict_ignores_it(tmp_path):
    root = make(tmp_path, {"g/index.md": fm(id="E1", title="G", kind="group", status="⬜"),
                           "g/E1-01-x.md": fm(id="E1-01", title="X", status="⬜", parent="E1", size="L")})
    r = run(root, "check", "--strict")
    assert r.exit_code == 0
    assert "HEALTH docs/todo/g/E1-01-x.md" in r.output and "[too_big]" in r.output and "[no_ac]" in r.output
    assert "0 помилок, 0 попереджень, 2 порад" in r.output
    r = run(root, "index")
    assert r.exit_code == 0 and (root / "docs/todo/INDEX.md").exists()
    assert "no_session" not in run(root, "check").output
