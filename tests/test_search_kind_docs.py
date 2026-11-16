"""`--kind docs` (E8-03, ADR-032): a path filter over code chunks, applied inside the KNN."""
import json
import os

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import config, ctx as ctxmod, daemon, db as dbmod, memory, search as S
from myvec.commands import app
from myvec.embed import get_embedder

runner = CliRunner()
PORT = 7878
TOKEN = "test-token"

TICKET = """---
id: E1-01
title: Narwhal ticket
status: in_progress
parent: E1
size: S
deps: []
blocks: []
summary: narwhal plan
---
# Narwhal ticket

Narwhal migration details for the ticket pipeline.
"""
GROUP = "---\nid: E1\ntitle: Epic one\nkind: group\nstatus: in_progress\n---\n# E1\n"
DOC_PATHS = {"README.md", "docs/decisions.md", "docs/todo/E1-x/E1-01-a.md", "docs/todo/E1-x/index.md", "guide.rst"}


def marker(root, name, shared=None):
    t = f'name = "{name}"\n' + (f"shared = {shared}\n" if shared else "") + "share_global = true\ninclude = []\nexclude = []\n"
    write(root / ".myvector.toml", t)


def mkrepo(tmp_path, name="proj", shared=None, word="narwhal"):
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / "README.md", f"# Readme\n\nThe {word} readme explains why we picked this approach.\n")
    write(root / "docs/decisions.md", f"# Decisions\n\nWe decided on {word} storage because it is simple.\n")
    write(root / "docs/todo/E1-x/index.md", GROUP)
    write(root / "docs/todo/E1-x/E1-01-a.md", TICKET.replace("Narwhal", word.capitalize()).replace("narwhal", word))
    write(root / "guide.rst", f"Guide\n=====\n\nThe {word} guide in reStructuredText.\n")
    write(root / "src/a.py", f"def {word}_handler(x):\n    \"\"\"Handle {word} requests.\"\"\"\n    return x\n")
    write(root / "notes.txt", f"plain text notes about {word} handling\n")
    marker(root, name, shared)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path):
    root = mkrepo(tmp_path)
    _, db = do_sync(root)
    memory.remember(db, get_embedder(config.load_config()), "narwhal decision stored in memory", "proj")
    db.close()
    return root


@pytest.fixture(autouse=True)
def _cwd():
    old = os.getcwd()
    yield
    os.chdir(old)


def jrun(*args, cwd, code=0):
    os.chdir(cwd)
    r = runner.invoke(app, ["search", *args, "--json"])
    assert r.exit_code == code, r.output
    return json.loads(r.stdout) if code == 0 else r


def srch(root, query, **kw):
    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    try:
        c = ctxmod.current(db, root)
        return S.search(db, cfg, get_embedder(cfg), query, c, **kw)
    finally:
        db.close()


# ---------------------------------------------------------------- helpers

def test_is_doc_path_by_extension_case_insensitive():
    assert all(S.is_doc_path(p) for p in ("README.md", "a/B.MD", "x.markdown", "x.mdx", "x.rst", "x.adoc"))
    assert not any(S.is_doc_path(p) for p in ("notes.txt", "src/a.py", "mdx", "a.md.bak", "requirements.txt"))


def test_doc_predicate_is_built_from_doc_exts():
    p = S.doc_predicate("bf.path")
    assert all(f"'%{e}'" in p for e in S.DOC_EXTS)
    assert "txt" not in p


def test_kinds_has_docs():
    assert S.KINDS == ("code", "memory", "docs", "all")


# ---------------------------------------------------------------- vector and hybrid routes

@pytest.mark.parametrize("mode,query", [("vector", "why we picked narwhal"), ("hybrid", "narwhal")])
def test_kind_docs_returns_only_doc_paths_no_memory(repo, mode, query):
    hits = srch(repo, query, k=20, kind="docs", mode=mode)
    assert hits and {h.kind for h in hits} == {"code"}
    paths = {h.data["path"] for h in hits}
    assert paths <= DOC_PATHS
    assert "notes.txt" not in paths and "src/a.py" not in paths


def test_kind_docs_excludes_txt_and_code_even_when_they_match_best(repo):
    hits = srch(repo, "narwhal_handler", k=20, kind="docs", mode="hybrid")
    assert hits and all(S.is_doc_path(h.data["path"]) for h in hits)
    hits = srch(repo, "plain text notes about narwhal handling", k=20, kind="docs", mode="vector")
    assert hits and all(S.is_doc_path(h.data["path"]) for h in hits)


def test_kind_docs_rst_included(repo):
    hits = srch(repo, "narwhal guide reStructuredText", k=20, kind="docs", mode="vector")
    assert "guide.rst" in {h.data["path"] for h in hits}


# ---------------------------------------------------------------- filter inside KNN

def test_doc_chunk_outside_general_top200_is_found(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for i in range(215):   # code chunks identical to the query: all nearer than the doc
        write(root / f"src/m{i}.py", f"def zorblax_{i}():\n    return 'zorblax zorblax zorblax {i}'\n")
    write(root / "docs/far.md", "# Far\n\nzorblax " + " ".join(f"filler{j}" for j in range(40)) + "\n")
    marker(root, "big")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    _, db = do_sync(root)
    cfg = config.load_config()
    qblob = dbmod.pack(get_embedder(cfg).embed_query("zorblax zorblax zorblax"))
    top = {r[0] for r in db.execute("SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k=200", (qblob,))}
    doc_rows = {r[0] for r in db.execute(
        "SELECT cr.vec_rowid FROM chunk_repo cr JOIN chunks c ON c.id=cr.chunk_id JOIN branch_files bf "
        "ON bf.blob_sha=c.blob_sha WHERE bf.path LIKE '%.md'")}
    assert doc_rows and not (doc_rows & top)   # precondition: unfiltered KNN top-200 has no doc chunk
    c = ctxmod.current(db, root)
    hits = S.search(db, cfg, get_embedder(cfg), "zorblax zorblax zorblax", c, k=5, kind="docs", mode="vector")
    db.close()
    assert [h.data["path"] for h in hits] == ["docs/far.md"]


# ---------------------------------------------------------------- kind=code/all unchanged

def test_kind_code_unchanged_includes_md_and_txt_not_memory(repo):
    hits = srch(repo, "narwhal", k=30, kind="code")
    assert {h.kind for h in hits} == {"code"}
    paths = {h.data["path"] for h in hits}
    assert "src/a.py" in paths and "notes.txt" in paths and "README.md" in paths


def test_kind_all_unchanged_has_code_and_memory(repo):
    hits = srch(repo, "narwhal", k=30, kind="all")
    assert {h.kind for h in hits} == {"code", "memory"}
    assert {"src/a.py", "notes.txt"} <= {h.data["path"] for h in hits if h.kind == "code"}


def test_kind_docs_is_subset_of_code(repo):
    code = {h.data["path"]: h.score for h in srch(repo, "narwhal", k=50, kind="code", mode="hybrid")}
    docs = srch(repo, "narwhal", k=50, kind="docs", mode="hybrid")
    assert docs
    for h in docs:
        assert h.data["path"] in code


def test_kind_memory_still_memory_only(repo):
    hits = srch(repo, "narwhal", k=10, kind="memory")
    assert hits and {h.kind for h in hits} == {"memory"}


# ---------------------------------------------------------------- CLI

def test_cli_kind_docs_json(repo):
    j = jrun("why we picked narwhal", "--kind", "docs", "--k", "20", cwd=repo)
    assert j and {h["kind"] for h in j} == {"code"}
    assert {h["path"] for h in j} <= DOC_PATHS


def test_cli_kind_docs_hybrid_identifier(repo):
    j = jrun("narwhal", "--kind", "docs", "--mode", "hybrid", cwd=repo)
    assert j and all(h["path"] in DOC_PATHS for h in j)


@pytest.mark.parametrize("scope", ["repo", "project", "all"])
def test_cli_kind_docs_all_scopes(repo, scope):
    j = jrun("narwhal", "--kind", "docs", "--scope", scope, cwd=repo)
    assert j and all(h["path"] in DOC_PATHS for h in j)


@pytest.mark.parametrize("bad", ["doc", "bogus"])
def test_cli_unknown_kind_errors_and_lists_docs(repo, bad):
    os.chdir(repo)
    r = runner.invoke(app, ["search", "narwhal", "--kind", bad])
    assert r.exit_code != 0
    assert "code|memory|docs|all" in r.output


def test_run_search_unknown_kind_message(repo):
    from myvec import searchcmd
    r = searchcmd.run_search("narwhal", str(repo), kind="doc")
    assert r.exit_code == 1 and "docs" in r.stderr


# ---------------------------------------------------------------- tickets

def test_ticket_annotation_for_docs_todo(repo):
    j = jrun("narwhal ticket pipeline migration", "--kind", "docs", "--k", "20", cwd=repo)
    tk = [h for h in j if h["path"] == "docs/todo/E1-x/E1-01-a.md"]
    assert tk and tk[0]["ticket"]["id"] == "E1-01" and tk[0]["ticket"]["status"] == "in_progress"
    assert all("ticket" not in h for h in j if not h["path"].startswith("docs/todo"))


# ---------------------------------------------------------------- --by-project

@pytest.fixture
def trio(tmp_path):
    roots = {n: mkrepo(tmp_path, n, shared=("false" if n == "c" else None), word="quokka") for n in "abc"}
    for r in roots.values():
        do_sync(r)
    db = dbmod.open_ready(config.load_config())
    emb = get_embedder(config.load_config())
    for n in "abc":
        memory.remember(db, emb, f"quokka decision recorded in project {n}", n)
    db.close()
    return roots


def test_by_project_kind_docs_only_docs_no_memory_hides_unshared(trio):
    j = jrun("quokka", "--by-project", "--kind", "docs", cwd=trio["a"])
    assert j["kind"] == "docs"
    names = {p["project"] for p in j["projects"]}
    assert names == {"b"}   # c is shared=false, a is the current project
    for p in j["projects"]:
        assert p["memory"] == 0 and p["code"] > 0
        assert all(h["kind"] == "code" and S.is_doc_path(h["path"]) for h in p["top"])


def test_scope_all_docs_respects_shared_false(trio):
    j = jrun("quokka", "--kind", "docs", "--scope", "all", "--k", "50", cwd=trio["a"])
    repos = {h["repo"].split("/")[0] for h in j}
    assert "c" not in repos and "b" in repos
    assert all(S.is_doc_path(h["path"]) for h in j)


def test_scope_all_docs_from_unshared_family_sees_itself(trio):
    j = jrun("quokka", "--kind", "docs", "--scope", "all", "--k", "50", cwd=trio["c"])
    assert "c" in {h["repo"].split("/")[0] for h in j}


def test_project_drilldown_kind_docs(trio):
    j = jrun("quokka", "--project", "b", "--kind", "docs", "--k", "20", cwd=trio["a"])
    assert j and all(S.is_doc_path(h["path"]) for h in j)


# ---------------------------------------------------------------- daemon

@pytest.fixture
def client(trio):
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    yield TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}"), trio
    st.executor.shutdown(wait=True)
    st.close()


def test_api_search_kind_docs(client):
    c, trio = client
    r = c.get("/api/search", params={"q": "quokka", "cwd": str(trio["a"]), "kind": "docs"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "docs"
    hits = body["results"]
    assert hits and all(S.is_doc_path(h["path"]) for h in hits)


def test_api_search_kind_docs_scope_all_hides_unshared(client):
    c, trio = client
    res = c.get("/api/search", params={"q": "quokka", "cwd": str(trio["a"]), "kind": "docs",
                                       "scope": "all"}).json()["results"]
    repos = {h["repo"].split("/")[0] for h in res}
    assert "b" in repos and "c" not in repos


def test_api_search_unknown_kind_is_400_and_lists_docs(client):
    c, trio = client
    r = c.get("/api/search", params={"q": "quokka", "cwd": str(trio["a"]), "kind": "doc"})
    assert r.status_code == 400
    assert "code|memory|docs|all" in r.text
