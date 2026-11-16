import json
import os

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import config, crossproject, daemon, daemon_client, db as dbmod, memory, projects, searchcmd
from myvec.commands import app
from myvec.embed import get_embedder

runner = CliRunner()
PORT = 7878
TOKEN = "test-token"
H = {"X-CSRF-Token": TOKEN}


def marker(root, name, shared=None, parent=None):
    t = f'name = "{name}"\n'
    if parent:
        t += f'parent = "{parent}"\n'
    if shared is not None:
        t += f"shared = {shared}\n"
    t += "share_global = true\ninclude = []\nexclude = []\n"
    write(root / ".myvector.toml", t)


def mkproject(tmp_path, name, word, shared=None):
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / f"{name}_mod.py", f"def {word}_handler(x):\n    \"\"\"Handle {word} requests.\"\"\"\n    return x * 2\n")
    marker(root, name, shared)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def trio(tmp_path):
    """a (viewer), b (shared), c (shared = false); all hold the same word `quokka` in code and memory."""
    roots = {n: mkproject(tmp_path, n, "quokka", shared=("false" if n == "c" else None)) for n in "abc"}
    for r in roots.values():
        do_sync(r)
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    emb = get_embedder(cfg)
    for n in "abc":
        memory.remember(db, emb, f"quokka decision recorded in project {n}", n)
    db.close()
    return roots


def run(*args, cwd):
    os.chdir(cwd)
    return runner.invoke(app, ["search", *args])


def jrun(*args, cwd):
    r = run(*args, "--json", cwd=cwd)
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


@pytest.fixture(autouse=True)
def _cwd():
    old = os.getcwd()
    yield
    os.chdir(old)


def repos_of(hits):
    return {h["repo"].split("/")[0] for h in hits if h["kind"] == "code"}


def mem_scopes(hits):
    return {h["scope"] for h in hits if h["kind"] == "memory"}


# ---------------------------------------------------------------- is_shared

def test_is_shared_default_true_without_marker_or_key(tmp_path):
    assert crossproject.is_shared(tmp_path / "nomarker") is True
    marker(tmp_path, "p")
    assert crossproject.is_shared(tmp_path) is True


def test_is_shared_false_and_true(tmp_path):
    marker(tmp_path, "p", "false")
    assert crossproject.is_shared(tmp_path) is False
    marker(tmp_path, "p", "true")
    assert crossproject.is_shared(tmp_path) is True


def test_is_shared_broken_toml_is_fail_closed(tmp_path):
    write(tmp_path / ".myvector.toml", 'name = "p"\nshared = = nope [\n')
    assert crossproject.is_shared(tmp_path) is False


@pytest.mark.parametrize("val", ['"false"', "0", '"no"', "[]"])
def test_is_shared_non_bool_is_fail_closed(tmp_path, val):
    write(tmp_path / ".myvector.toml", f'name = "p"\nshared = {val}\n')
    assert crossproject.is_shared(tmp_path) is False


def test_is_shared_edit_picked_up_by_mtime_without_sync(tmp_path):
    marker(tmp_path, "p", "false")
    f = tmp_path / ".myvector.toml"
    assert crossproject.is_shared(tmp_path) is False
    st = f.stat()
    marker(tmp_path, "p", "true")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert crossproject.is_shared(tmp_path) is True


def test_hidden_projects_family_sees_itself_and_descendants(tmp_path):
    (tmp_path / "x").mkdir()
    marker(tmp_path / "x", "x", "false")
    sub = tmp_path / "x" / "sub"
    sub.mkdir()
    marker(sub, "sub", None, parent="x")
    (tmp_path / "y").mkdir()
    marker(tmp_path / "y", "y")
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    for p, root in (("x", tmp_path / "x"), ("x/sub", sub), ("y", tmp_path / "y")):
        db.execute("INSERT INTO projects(path, root) VALUES (?, ?)", (p, str(root)))
    db.commit()
    from myvec import ctx as ctxmod
    y = ctxmod.current(db, tmp_path / "y")
    assert crossproject.hidden_projects(db, y) == {"x"}
    assert crossproject.is_hidden("x/sub", {"x"}) and not crossproject.is_hidden("xy", {"x"})
    assert crossproject.hidden_projects(db, ctxmod.current(db, tmp_path / "x")) == set()
    assert crossproject.hidden_projects(db, ctxmod.current(db, sub)) == set()
    assert crossproject.hidden_projects(db, None) == {"x"}
    db.close()


# ---------------------------------------------------------------- CLI hiding

def test_scope_all_hides_unshared_code_and_memory(trio):
    hits = jrun("quokka", "--scope", "all", "--k", "30", cwd=trio["a"])
    assert repos_of(hits) == {"a", "b"}
    assert mem_scopes(hits) == {"a", "b"}


def test_scope_all_from_unshared_family_sees_everything(trio):
    hits = jrun("quokka", "--scope", "all", "--k", "30", cwd=trio["c"])
    assert repos_of(hits) == {"a", "b", "c"}
    assert mem_scopes(hits) == {"a", "b", "c"}


def test_scope_project_inside_unshared_project_still_works(trio):
    hits = jrun("quokka", cwd=trio["c"])
    assert repos_of(hits) == {"c"}


def test_edit_shared_flag_takes_effect_without_sync(trio):
    assert "c" not in repos_of(jrun("quokka", "--scope", "all", "--k", "30", cwd=trio["a"]))
    f = trio["c"] / ".myvector.toml"
    st = f.stat()
    marker(trio["c"], "c", "true")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert "c" in repos_of(jrun("quokka", "--scope", "all", "--k", "30", cwd=trio["a"]))


def test_broken_marker_hides_project_from_others(trio):
    f = trio["b"] / ".myvector.toml"
    st = f.stat()
    f.write_text("name = [[[ broken", encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    hits = jrun("quokka", "--scope", "all", "--k", "30", cwd=trio["a"])
    assert "b" not in repos_of(hits) and "b" not in mem_scopes(hits)


def test_global_memory_not_hidden(trio):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    memory.remember(db, get_embedder(cfg), "quokka global rule", "global")
    db.close()
    hits = jrun("quokka", "--scope", "all", "--kind", "memory", "--k", "30", cwd=trio["a"])
    assert "global" in mem_scopes(hits) and "c" not in mem_scopes(hits)


# ---------------------------------------------------------------- --by-project

def test_by_project_json_excludes_unshared_and_current(trio):
    j = jrun("quokka", "--by-project", cwd=trio["a"])
    assert [p["project"] for p in j["projects"]] == ["b"]
    assert j["current"]["project"] == "a" and j["current"]["hits"] > 0
    p = j["projects"][0]
    assert p["code"] >= 1 and p["memory"] >= 1 and 1 <= len(p["top"]) <= crossproject.TOP_HITS
    assert j["query"] == "quokka" and j["kind"] == "all"
    assert "c" not in {p["project"] for p in j["projects"]}
    assert "quokka decision recorded in project c" not in json.dumps(j)


def test_by_project_from_unshared_family_includes_it(trio):
    j = jrun("quokka", "--by-project", cwd=trio["c"])
    assert {p["project"] for p in j["projects"]} == {"a", "b"}
    assert j["current"]["project"] == "c"


def test_by_project_text_mode(trio):
    r = run("quokka", "--by-project", cwd=trio["a"])
    assert r.exit_code == 0, r.output
    assert "found in 1 projects" in r.stdout
    assert "1. b " in r.stdout and "code" in r.stdout and "memory" in r.stdout
    assert "current project (a)" in r.stdout
    assert 'myvec search --project <name>' in r.stdout
    assert "\n2. " not in r.stdout


def test_by_project_kind_code_has_no_memory_hits(trio):
    j = jrun("quokka", "--by-project", "--kind", "code", cwd=trio["a"])
    assert all(p["memory"] == 0 and p["code"] >= 1 for p in j["projects"])


def test_by_project_no_results(trio):
    r = run("zzzxqvwunmatchable", "--by-project", "--kind", "code", cwd=trio["a"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "no results" or "found in" in r.stdout


def test_by_project_limit(tmp_path):
    roots = [mkproject(tmp_path, f"p{i}", "quokka") for i in range(crossproject.BY_PROJECT_LIMIT + 2)]
    for r in roots:
        do_sync(r)
    j = jrun("quokka", "--by-project", "--kind", "code", cwd=roots[0])
    assert len(j["projects"]) == crossproject.BY_PROJECT_LIMIT


def test_by_project_and_project_conflict(trio):
    r = run("quokka", "--by-project", "--project", "b", cwd=trio["a"])
    assert r.exit_code == 1 and "cannot be combined" in r.output


# ---------------------------------------------------------------- --project

def test_project_drilldown_searches_only_target(trio):
    hits = jrun("quokka", "--project", "b", "--k", "30", cwd=trio["a"])
    assert repos_of(hits) == {"b"} and mem_scopes(hits) == {"b"}


def test_project_drilldown_excludes_global_memory(trio):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    memory.remember(db, get_embedder(cfg), "quokka global rule", "global")
    db.close()
    hits = jrun("quokka", "--project", "b", "--k", "30", "--kind", "memory", cwd=trio["a"])
    assert "global" not in mem_scopes(hits)


def test_project_unshared_from_other_family_refused(trio):
    r = run("quokka", "--project", "c", cwd=trio["a"])
    assert r.exit_code == 1
    assert "shared = false" in r.output
    assert "quokka_handler" not in r.stdout


def test_project_unshared_from_own_family_allowed(trio):
    hits = jrun("quokka", "--project", "c", cwd=trio["c"])
    assert repos_of(hits) == {"c"}


def test_project_unknown(trio):
    r = run("quokka", "--project", "nonexistent", cwd=trio["a"])
    assert r.exit_code == 1 and "error:" in r.output


def test_project_by_path(trio):
    hits = jrun("quokka", "--project", str(trio["b"]), "--kind", "code", cwd=trio["a"])
    assert repos_of(hits) == {"b"}


def test_project_root_missing_on_this_machine(trio, tmp_path):
    import shutil
    shutil.rmtree(trio["b"])
    r = run("quokka", "--project", "b", cwd=trio["a"])
    assert r.exit_code == 1 and "does not exist" in r.output


def test_project_does_not_touch_target_activity(trio, monkeypatch):
    touched = []
    from myvec import activity
    monkeypatch.setattr(activity, "touch", lambda p: touched.append(p))
    jrun("quokka", "--project", "b", cwd=trio["a"])
    assert touched == ["a"]


def test_project_hidden_child_of_unshared_parent_refused(tmp_path):
    a = mkproject(tmp_path, "a", "quokka")
    c = mkproject(tmp_path, "c", "quokka", shared="false")
    sub = c / "sub"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    write(sub / "s.py", "def quokka_sub():\n    return 1\n")
    marker(sub, "sub", None, parent="c")
    git(sub, "add", "-A")
    git(sub, "commit", "-q", "-m", "i")
    for r in (a, c, sub):
        do_sync(r)
    assert "c" not in repos_of(jrun("quokka", "--scope", "all", "--k", "30", cwd=a))
    r = run("quokka", "--project", "c/sub", cwd=a)
    assert r.exit_code == 1 and "shared = false" in r.output


def test_project_relative_path_resolved_against_request_cwd(trio, tmp_path):
    os.chdir(trio["b"])   # the "daemon" cwd differs from the caller's
    r = searchcmd.run_search("quokka", str(tmp_path), project="b", kind="code", json_out=True)
    assert r.exit_code == 0 and repos_of(json.loads(r.stdout)) == {"b"}
    os.chdir(tmp_path.parent)
    r = searchcmd.run_search("quokka", str(tmp_path), project="./b", kind="code", json_out=True)
    assert r.exit_code == 0 and repos_of(json.loads(r.stdout)) == {"b"}


def test_scope_all_freshness_ignores_unshared_repo(trio):
    git(trio["c"], "commit", "-q", "--allow-empty", "-m", "more")   # c becomes stale
    asked = []
    r = searchcmd.run_search("quokka", str(trio["a"]), scope="all", kind="code",
                             trigger=lambda p, root: asked.append(p))
    assert r.exit_code == 0 and "c" not in asked and "(індекс оновлюється" not in r.stdout
    asked.clear()
    searchcmd.run_search("quokka", str(trio["c"]), scope="all", kind="code", trigger=lambda p, root: asked.append(p))
    assert "c" in asked   # its own family still sees it


def test_project_parent_from_outside_hides_unshared_child(tmp_path):
    a = mkproject(tmp_path, "a", "quokka")
    p = mkproject(tmp_path, "p", "quokka")
    ch = p / "ch"
    ch.mkdir()
    git(ch, "init", "-q", "-b", "main")
    write(ch / "s.py", "def quokka_child():\n    return 1\n")
    marker(ch, "ch", "false", parent="p")
    git(ch, "add", "-A")
    git(ch, "commit", "-q", "-m", "i")
    for r in (a, p, ch):
        do_sync(r)
    hits = jrun("quokka", "--project", "p", "--kind", "code", "--k", "30", cwd=a)
    assert repos_of(hits) == {"p"} and "quokka_child" not in json.dumps(hits)


# ---------------------------------------------------------------- daemon

@pytest.fixture
def client(trio):
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    yield TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}"), trio
    st.executor.shutdown(wait=True)
    st.close()


def test_api_search_ui_hides_unshared(client):
    c, trio = client
    j = c.get("/api/search", params={"q": "quokka", "cwd": str(trio["a"]), "scope": "all"}).json()
    s = json.dumps(j)
    assert "c_mod.py" not in s and "project c" not in s
    assert "b_mod.py" in s or "project b" in s


def test_api_cli_search_by_project_and_cross_marker(client):
    c, trio = client
    r = c.post("/api/cli/search", json={"q": "quokka", "cwd": str(trio["a"]), "by_project": True, "json": True},
               headers=H).json()
    assert r["cross"] == 1 and r["exit_code"] == 0
    assert [p["project"] for p in json.loads(r["stdout"])["projects"]] == ["b"]


def test_api_cli_search_project_refuses_unshared(client):
    c, trio = client
    r = c.post("/api/cli/search", json={"q": "quokka", "cwd": str(trio["a"]), "project": "c"}, headers=H).json()
    assert r["exit_code"] == 1 and "shared = false" in r["stderr"]


def test_api_cli_search_scope_all_hides_unshared(client):
    c, trio = client
    r = c.post("/api/cli/search", json={"q": "quokka", "cwd": str(trio["a"]), "scope": "all", "json": True, "k": 30},
               headers=H).json()
    assert "c" not in repos_of(json.loads(r["stdout"]))


def test_memory_browser_sees_unshared(client):
    c, trio = client
    items = c.get("/api/memories", params={"q": "quokka decision recorded"}).json()["memories"]
    assert {m["scope"] for m in items} >= {"a", "b", "c"}


def test_cli_old_daemon_without_cross_falls_back_to_direct(trio, monkeypatch):
    calls = []

    def fake(method, path, body=None, **kw):
        calls.append(body)
        return {"stdout": "OLD DAEMON ANSWER", "stderr": "", "exit_code": 0}   # no `cross`

    monkeypatch.setattr(daemon_client, "request", fake)
    r = run("quokka", "--by-project", "--json", cwd=trio["a"])
    assert calls and r.exit_code == 0 and "OLD DAEMON ANSWER" not in r.stdout
    assert json.loads(r.stdout)["projects"][0]["project"] == "b"
    r = run("quokka", "--project", "c", cwd=trio["a"])
    assert r.exit_code == 1 and "OLD DAEMON ANSWER" not in r.stdout


def test_cli_plain_search_accepts_old_daemon_answer(trio, monkeypatch):
    monkeypatch.setattr(daemon_client, "request",
                        lambda *a, **k: {"stdout": "OLD DAEMON ANSWER", "stderr": "", "exit_code": 0})
    r = run("quokka", "--expand", cwd=trio["a"])
    assert "OLD DAEMON ANSWER" in r.stdout


def test_cli_new_daemon_answer_used_for_by_project(trio, monkeypatch):
    monkeypatch.setattr(daemon_client, "request",
                        lambda *a, **k: {"stdout": "FROM DAEMON", "stderr": "", "exit_code": 0, "cross": 1})
    r = run("quokka", "--by-project", cwd=trio["a"])
    assert "FROM DAEMON" in r.stdout


# ---------------------------------------------------------------- init keeps shared

def test_write_marker_preserves_shared_false(tmp_path):
    marker(tmp_path, "p", "false")
    projects.write_marker(tmp_path, "p", None, [], [], True)
    assert "shared = false" in (tmp_path / ".myvector.toml").read_text()
    assert crossproject.is_shared(tmp_path) is False


def test_init_cli_preserves_shared_false(tmp_path):
    root = mkproject(tmp_path, "z", "quokka", shared="false")
    os.chdir(root)
    r = runner.invoke(app, ["init", "--name", "z"])
    assert r.exit_code == 0, r.output
    assert "shared = false" in (root / ".myvector.toml").read_text()


def test_api_cli_search_non_string_project_is_400(client):
    c, trio = client
    for bad in (5, ["b"], {"x": 1}):
        r = c.post("/api/cli/search", json={"q": "quokka", "cwd": str(trio["a"]), "project": bad}, headers=H)
        assert r.status_code == 400
