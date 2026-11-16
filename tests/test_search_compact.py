"""E8-13 (ADR-043): compact search output, `-q` multi-query, identifier total, daemon `compact` echo."""
import json
import os

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import config, daemon, daemon_client, search as S, searchcmd, searchlog
from myvec.commands import app

runner = CliRunner()
PORT = 7878
TOKEN = "test-token"
H = {"X-CSRF-Token": TOKEN}


def mkproj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / "src/pay.py", "def charge_customer(order):\n    \"\"\"Charge the customer for an order.\"\"\"\n"
                               "    return order.amount\n\ndef refund_customer(order):\n    return -order.amount\n")
    write(root / "src/cart.py", "class Cart:\n    def add_item(self, item):\n        self.items.append(item)\n")
    # 200 plain lines: windows of 60 lines overlap by 10 (lines 51-60 live in two chunks)
    lines = [f"value_{i} = {i}  # filler line number {i}" for i in range(1, 201)]
    lines[54] = "zorbleflux_marker = 1  # in the overlap of two chunks"
    write(root / "src/big.py", "\n".join(lines) + "\n")
    write(root / ".myvector.toml", 'name = "proj"\nshare_global = true\ninclude = []\nexclude = []\n')
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = mkproj(tmp_path)
    do_sync(root)[1].close()
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: None)   # direct path
    old = os.getcwd()
    os.chdir(root)
    yield root
    os.chdir(old)


def srch(*args, code=0):
    r = runner.invoke(app, ["search", *args])
    assert r.exit_code == code, r.output
    return r


def block_paths(text):
    return [ln.split(":")[0] for ln in text.splitlines() if ".py:" in ln and not ln.startswith(("total", " "))]


# ------------------------------------------------------------- compact / expand / auto

def test_non_tty_default_is_compact_without_snippets(proj):
    out = srch("charge customer order").stdout
    assert "src/pay.py:" in out and "def charge_customer" not in out and "return order.amount" not in out
    assert len(out.encode()) < 1024


def test_compact_line_format_path_range_symbol(proj):
    first = srch("charge_customer", "--compact").stdout.splitlines()[0]
    path, _, rest = first.partition(" ")
    p, _, rng = path.partition(":")
    a, _, b = rng.partition("-")
    assert p.endswith(".py") and a.isdigit() and b.isdigit()


def test_expand_in_non_tty_equals_legacy_format(proj):
    out = srch("charge_customer", "--expand").stdout
    res = searchcmd.run_search("charge_customer", proj, total=True)
    assert out.rstrip("\n") == res.stdout   # the same text the legacy (expanded) path produces
    assert "def charge_customer" in out


class _TtySys:
    """Stand-in for the `sys` module that `commands.search` imports locally: stdout claims to be a terminal."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def stdout(self):
        real = self._real.stdout

        class W:
            def __getattr__(self, n):
                return getattr(real, n)

            def isatty(self):
                return True
        return W()


@pytest.fixture
def as_tty(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "sys", _TtySys(sys))


def test_compact_flag_in_tty_is_compact(proj, as_tty):
    assert "def charge_customer" not in srch("charge_customer", "--compact").stdout


def test_tty_default_is_expanded(proj, as_tty):
    assert "def charge_customer" in srch("charge_customer").stdout


def test_full_in_non_tty_passes_compact_false(proj, monkeypatch):
    seen = []
    real = searchcmd.run_search
    monkeypatch.setattr(searchcmd, "run_search", lambda *a, **k: (seen.append(k["compact"]), real(*a, **k))[1])
    srch("charge_customer")
    srch("charge_customer", "--full")
    srch("charge_customer", "--expand")
    assert seen == [True, False, False]


def test_compact_and_expand_together_fail(proj):
    r = srch("charge_customer", "--compact", "--expand", code=1)
    assert "cannot be combined" in r.output


def test_full_ignores_auto_compact_and_prints_chunks(proj):
    out = srch("charge_customer", "--full").stdout
    assert "def charge_customer" in out and "Charge the customer" in out


def test_json_single_query_unchanged_by_compact(proj):
    a = srch("charge_customer", "--json").stdout
    b = srch("charge_customer", "--json", "--expand").stdout
    assert json.loads(a) == json.loads(b) and isinstance(json.loads(a), list)
    assert "total in index" not in a


def test_by_project_unchanged_by_compact(proj):
    a = srch("charge_customer", "--by-project").stdout
    b = srch("charge_customer", "--by-project", "--expand").stdout
    assert a == b


def test_compact_order_equals_expanded_order(proj):
    keys = lambda h: [(x["path"], x["start"], x["end"]) for x in h]
    js = json.loads(srch("customer order", "--json").stdout)
    c = srch("customer order").stdout.splitlines()
    got = [ln.split(" ")[0] for ln in c if ".py:" in ln]
    want = [f"{x['path']}:{x['start']}-{x['end']}" for x in js]
    assert got == want[:len(got)] and len(got) >= 2, keys(js)


def test_compact_respects_k(proj):
    out = srch("value filler", "--k", "2").stdout
    assert len([ln for ln in out.splitlines() if ".py:" in ln and not ln.startswith("total")]) <= 2


def test_no_results_text_kept(proj):
    assert srch("qqqqxx wwwwzz nothing here", "--kind", "memory").stdout.strip() == "no results"


# ------------------------------------------------------------- format_compact

def _hit(kind, **d):
    return S.Hit(kind=kind, key=str(d.get("id") or d.get("path")), score=1.0, data=d)


def code_hit(repo="r", path="a.py", start=1, end=5, symbol="f", ticket=None):
    return _hit("code", repo=repo, path=path, start=start, end=end, symbol=symbol, ticket=ticket, text="x")


def test_format_compact_no_repo_prefix_for_cwd_repo():
    out = S.format_compact([code_hit(), code_hit(path="b.py")], 1000, "r")
    assert out.splitlines()[0] == "a.py:1-5 f"


def test_format_compact_prefix_for_foreign_or_multiple_repos():
    out = S.format_compact([code_hit(repo="r"), code_hit(repo="o", path="b.py")], 1000, "r")
    assert out.splitlines() == ["r a.py:1-5 f", "o b.py:1-5 f"]


def test_format_compact_ticket_and_missing_symbol():
    out = S.format_compact([code_hit(symbol=None, ticket={"id": "E1-01", "status": "in_progress"})], 1000, "r")
    assert out == "a.py:1-5 · ticket E1-01 [in_progress]"


def test_format_compact_memory_line_truncated_to_100():
    h = _hit("memory", date="2026-01-02", id="abc", text="word " * 60)
    line = S.format_compact([h], 1000, "r")
    assert line.startswith("[M] 2026-01-02 abc: ") and line.endswith("...")
    assert len(line.split(": ", 1)[1]) <= 103 and "\n" not in line


def test_format_compact_memory_short_has_no_ellipsis():
    h = _hit("memory", date="2026-01-02", id="abc", text="short\nnote")
    assert S.format_compact([h], 1000, "r") == "[M] 2026-01-02 abc: short note"


def test_format_compact_budget_cuts_but_keeps_first_line():
    hits = [code_hit(path=f"file{i}.py") for i in range(50)]
    out = S.format_compact(hits, 20, "r").splitlines()
    assert 1 <= len(out) < 50
    assert len(S.format_compact(hits, 1, "r").splitlines()) == 1


# ------------------------------------------------------------- identifier total

def test_ident_total_counts_unique_lines_in_chunk_overlap(proj):
    out = srch("zorbleflux_marker").stdout
    assert "total in index: 1 lines in 1 files (word/prefix matches)" in out
    assert out.rstrip().splitlines()[-1].endswith("(word/prefix matches) · all: rg -n -F -- zorbleflux_marker .")


def test_ident_total_overlap_is_not_summed(proj):
    from myvec import ctx as ctxmod, db as dbmod
    db = dbmod.open_ready(config.load_config())
    n = db.execute("SELECT count(*) FROM chunks WHERE text LIKE '%zorbleflux_marker%'").fetchone()[0]
    assert n >= 2   # the premise: the line lives in two chunks
    c = ctxmod.current(db, proj)
    t = S.ident_total(db, c, "project", "all", "zorbleflux_marker")
    assert t["lines"] == 1 and t["files"] == 1 and t["capped"] is False
    db.close()


def test_ident_total_line_has_rg_command_with_quote(proj):
    out = srch("charge_customer").stdout
    assert "total in index: " in out and "· all: rg -n -F -- charge_customer ." in out


def test_ident_total_expanded_mode_too(proj):
    assert "total in index: " in srch("charge_customer", "--expand").stdout


def test_ident_total_not_in_json_or_by_project(proj):
    assert "total in index" not in srch("charge_customer", "--json").stdout
    assert "total in index" not in srch("charge_customer", "--by-project").stdout


def test_natural_language_query_has_no_total_line(proj):
    assert "total in index" not in srch("how do we charge the customer").stdout


def test_short_query_has_no_total_line(proj):
    assert "total in index" not in srch("ab").stdout


def test_memory_kind_has_no_total_line(proj):
    assert "total in index" not in srch("charge_customer", "--kind", "memory").stdout


def test_scope_all_has_total_but_no_rg(proj):
    out = srch("charge_customer", "--scope", "all").stdout
    assert "total in index" in out and "rg -n" not in out


def test_format_ident_total_hard_secret_has_no_rg():
    t = {"lines": 3, "files": 2, "capped": False, "root": "/x/y"}
    out = S.format_ident_total(t, "AKIAIOSFODNN7EXAMPLE", "/x/y")
    assert out == "total in index: 3 lines in 2 files (word/prefix matches)"


def test_format_ident_total_capped_and_relative_root():
    t = {"lines": 5000, "files": 7, "capped": True, "root": "/x/y/sub"}
    out = S.format_ident_total(t, "foo_bar", "/x/y")
    assert out == "total in index: ≥5000 lines in 7 files (word/prefix matches) · all: rg -n -F -- foo_bar sub"


def test_format_ident_total_quotes_query():
    t = {"lines": 1, "files": 1, "capped": False, "root": "/x"}
    assert "'it'\"'\"'s'" in S.format_ident_total(t, "it's", "/x")


def test_ident_total_none_for_non_identifiers(proj):
    from myvec import ctx as ctxmod, db as dbmod
    db = dbmod.open_ready(config.load_config())
    c = ctxmod.current(db, proj)
    assert S.ident_total(db, c, "project", "all", "two words") is None
    assert S.ident_total(db, c, "project", "all", "ab") is None
    assert S.ident_total(db, c, "project", "memory", "charge_customer") is None
    assert S.ident_total(db, c, "project", "all", "charge_customer")["lines"] >= 1
    db.close()


def test_ident_total_zero_for_absent_word(proj):
    from myvec import ctx as ctxmod, db as dbmod
    db = dbmod.open_ready(config.load_config())
    c = ctxmod.current(db, proj)
    t = S.ident_total(db, c, "project", "all", "nonexistentword")
    assert t["lines"] == 0 and t["files"] == 0
    db.close()


def _ident_db(proj):
    from myvec import ctx as ctxmod, db as dbmod
    db = dbmod.open_ready(config.load_config())
    return db, ctxmod.current(db, proj)


def test_ident_total_counts_longer_identifier_by_prefix(proj):
    db, c = _ident_db(proj)
    # `charge` is a prefix of the token `charge_customer`
    assert S.ident_total(db, c, "project", "all", "charge_cus")["lines"] >= 1
    db.close()


def _extra(tmp_path, name, shared):
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / "src/a.py", "def quokka_ident():\n    return 1\n")
    write(root / "docs/guide.md", "# Guide\n\nCall quokka_ident to start.\n")
    sh = "" if shared else "shared = false\n"
    write(root / ".myvector.toml", f'name = "{name}"\nshare_global = true\n{sh}include = []\nexclude = []\n')
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    do_sync(root)[1].close()
    return root


def test_ident_total_scope_all_skips_unshared_project(proj, tmp_path):
    _extra(tmp_path, "viz", True)
    _extra(tmp_path, "sec", False)
    db, c = _ident_db(proj)
    t = S.ident_total(db, c, "all", "all", "quokka_ident")
    assert t["lines"] == 2 and t["files"] == 2       # viz only: code + doc; `sec` is hidden
    db.close()


def test_ident_total_kind_docs_counts_only_docs(proj, tmp_path):
    _extra(tmp_path, "viz", True)
    db, c = _ident_db(proj)
    t = S.ident_total(db, c, "all", "docs", "quokka_ident")
    assert t["lines"] == 1 and t["files"] == 1
    db.close()


def test_ident_total_project_flag_counts_that_project(proj, tmp_path):
    _extra(tmp_path, "viz", True)
    out = srch("quokka_ident", "--project", "viz").stdout
    assert "total in index: 2 lines in 2 files" in out


# ------------------------------------------------------------- -q multi

def test_two_q_blocks_with_headers(proj):
    out = srch("-q", "charge_customer", "-q", "Cart").stdout
    assert "# 1/2 charge_customer" in out and "# 2/2 Cart" in out
    assert out.index("# 1/2") < out.index("# 2/2")


def test_positional_plus_q_positional_first(proj):
    out = srch("charge_customer", "-q", "Cart").stdout
    assert out.index("# 1/2 charge_customer") < out.index("# 2/2 Cart")


def test_single_positional_has_no_header(proj):
    assert "# 1/1" not in srch("charge_customer").stdout


def test_single_q_option_has_no_header(proj):
    out = srch("-q", "charge_customer").stdout
    assert "# 1/1" not in out and "src/pay.py" in out


def test_no_query_fails_code_1(proj):
    assert "give a query" in srch(code=1).output


def test_more_than_five_queries_fail(proj):
    args = [x for i in range(6) for x in ("-q", f"query{i}")]
    assert "at most 5" in srch(*args, code=1).output


def test_five_queries_allowed(proj):
    args = [x for i in range(5) for x in ("-q", f"query{i}")]
    assert "# 5/5" in srch(*args).stdout


def test_multi_q_json_is_list_of_query_results(proj):
    data = json.loads(srch("-q", "charge_customer", "-q", "Cart", "--json").stdout)
    assert [d["query"] for d in data] == ["charge_customer", "Cart"]
    assert all(isinstance(d["results"], list) for d in data)


def test_single_q_json_is_plain_list(proj):
    assert isinstance(json.loads(srch("-q", "charge_customer", "--json").stdout), list)


def test_multi_q_header_truncates_long_query_to_80(proj):
    q = "word " * 30
    out = srch("-q", "charge_customer", "-q", q).stdout
    assert f"# 2/2 {q[:80]}\n" in out and q[:81] not in out


def test_multi_q_writes_one_log_record_per_query(proj):
    srch("-q", "charge_customer", "-q", "Cart")
    recs = searchlog.read(0)
    assert len(recs) == 2 and all(r["fmt"] == "compact" for r in recs)


def test_multi_q_error_exit_code_and_stderr_prefix(proj):
    r = runner.invoke(app, ["search", "-q", "charge_customer", "-q", "x", "--scope", "nope"])
    assert r.exit_code == 1 and "# 1/2 " in r.stderr and "# 2/2 " in r.stderr


def test_multi_q_one_failing_keeps_other_output(proj, monkeypatch):
    real = searchcmd.run_search

    def flaky(q, *a, **k):
        if q == "bad":
            return searchcmd.CliResult(stdout="", stderr="boom", exit_code=2)
        return real(q, *a, **k)
    monkeypatch.setattr(searchcmd, "run_search", flaky)
    r = runner.invoke(app, ["search", "-q", "bad", "-q", "charge_customer"])
    assert r.exit_code == 2 and "# 1/2 boom" in r.stderr and "# 2/2 charge_customer" in r.stdout


# ------------------------------------------------------------- log

def test_log_fmt_values_and_no_query_text(proj):
    q = "zorbleflux_marker"
    srch(q)
    srch(q, "--expand")
    srch(q, "--json")
    srch(q, "--by-project")
    assert [r["fmt"] for r in searchlog.read(0)] == ["compact", "expanded", "json", "summary"]
    assert q not in (config.logs_dir() / "searches.jsonl").read_text()


def test_log_fmt_full_is_expanded(proj):
    srch("charge_customer", "--full")
    assert searchlog.read(0)[-1]["fmt"] == "expanded"


def test_compact_tokens_equals_actual_output_length(proj):
    res = searchcmd.run_search("charge_customer", proj, compact=True, total=True)
    assert res.tokens == len(res.stdout) // 4 and res.fmt == "compact"
    exp = searchcmd.run_search("charge_customer", proj, compact=False, total=True)
    assert res.tokens < exp.tokens


def test_run_search_default_is_expanded_without_total(proj):
    res = searchcmd.run_search("charge_customer", proj)
    assert res.fmt == "expanded" and "total in index" not in res.stdout and "def charge_customer" in res.stdout


def test_run_search_compact_ignored_for_json(proj):
    res = searchcmd.run_search("charge_customer", proj, compact=True, json_out=True)
    assert res.fmt == "json" and isinstance(json.loads(res.stdout), list)


# ------------------------------------------------------------- daemon

@pytest.fixture
def dclient(ws):
    do_sync(ws)[1].close()
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    yield TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
    st.executor.shutdown(wait=True)
    st.close()


def test_daemon_compact_equals_direct_and_echoes(dclient, ws):
    body = {"q": "renderPdf", "cwd": str(ws / "site"), "compact": True, "total": True}
    r = dclient.post("/api/cli/search", json=body, headers=H).json()
    d = searchcmd.run_search("renderPdf", ws / "site", compact=True, total=True)
    assert r["compact"] == 1 and r["stdout"] == d.stdout and r["exit_code"] == 0


def test_daemon_without_compact_field_is_expanded_as_before(dclient, ws):
    r = dclient.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H).json()
    d = searchcmd.run_search("renderPdf", ws / "site")
    assert r["stdout"] == d.stdout and "total in index" not in r["stdout"]


def test_daemon_logs_fmt(dclient, ws):
    dclient.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site"), "compact": True}, headers=H)
    dclient.post("/api/cli/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H)
    assert [r["fmt"] for r in searchlog.read(0)] == ["compact", "expanded"]


def test_client_sends_compact_and_total_and_uses_daemon_answer(proj, monkeypatch):
    bodies = []

    def fake(m, p, b=None, **k):
        bodies.append(b)
        return {"stdout": "FROM DAEMON", "stderr": "", "exit_code": 0, "compact": 1, "cross": 1}
    monkeypatch.setattr(daemon_client, "request", fake)
    assert "FROM DAEMON" in srch("charge_customer").stdout
    assert bodies[0]["compact"] is True and bodies[0]["total"] is True
    srch("charge_customer", "--expand")
    assert bodies[1]["compact"] is False


def test_client_without_compact_echo_falls_back_to_direct(proj, monkeypatch):
    monkeypatch.setattr(daemon_client, "request",
                        lambda *a, **k: {"stdout": "OLD DAEMON", "stderr": "", "exit_code": 0, "cross": 1})
    out = srch("charge_customer").stdout
    assert "OLD DAEMON" not in out and "src/pay.py" in out


def test_client_expand_accepts_daemon_without_echo(proj, monkeypatch):
    monkeypatch.setattr(daemon_client, "request",
                        lambda *a, **k: {"stdout": "OLD DAEMON", "stderr": "", "exit_code": 0, "cross": 1})
    assert "OLD DAEMON" in srch("charge_customer", "--expand").stdout


def test_client_json_accepts_daemon_without_echo(proj, monkeypatch):
    monkeypatch.setattr(daemon_client, "request",
                        lambda *a, **k: {"stdout": "[]", "stderr": "", "exit_code": 0, "cross": 1})
    assert srch("charge_customer", "--json").stdout.strip() == "[]"
