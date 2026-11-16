"""The web UI: static files served by the daemon + the API fields the UI relies on."""
import json
import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import do_sync, git, write
from myvec import config, daemon, memory, searchcmd
from myvec.embed import get_embedder
from myvec.scheduler import Scheduler

PORT = 7878
TOKEN = "test-token"
H = {"X-CSRF-Token": TOKEN}
UI = Path(daemon.__file__).parent / "ui"


@pytest.fixture
def state(ws):
    do_sync(ws)
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    yield st
    st.executor.shutdown(wait=True)
    st.close()


@pytest.fixture
def client(state):
    return TestClient(daemon.create_app(state), base_url=f"http://127.0.0.1:{PORT}")


# ------------------------------------------------------------------ static files

def test_index_is_the_real_ui_with_csrf_and_asset_links(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert f'<meta name="csrf-token" content="{TOKEN}">' in r.text
    assert "{{" not in r.text and 'lang="uk"' in r.text and 'id="app"' in r.text and 'id="toasts"' in r.text
    assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', r.text)
    assert {"/static/app.css", "/static/app.js", "/static/mark.svg", "/static/fonts/Geist-Variable.woff2"} <= set(assets)
    for a in assets:
        assert client.get(a).status_code == 200, a
    assert r.headers["cache-control"] == "no-store"
    assert "cdn" not in r.text.lower() and "http://" not in r.text and "https://" not in r.text   # no network assets


@pytest.mark.parametrize("name,ctype", [
    ("app.css", "text/css"), ("app.js", "javascript"), ("util.js", "javascript"), ("views.js", "javascript"), ("tracker.js", "javascript"),
    ("mark.svg", "image/svg+xml"), ("fonts/Geist-Variable.woff2", "font/woff2"),
    ("fonts/GeistMono-Variable.woff2", "font/woff2"),
])
def test_static_asset_content_types(client, name, ctype):
    r = client.get(f"/static/{name}")
    assert r.status_code == 200 and ctype in r.headers["content-type"], r.headers["content-type"]
    assert len(r.content) > 100
    assert r.headers.get("cache-control") == "no-cache"          # no stale UI after a reinstall


def test_js_module_imports_all_resolve(client):
    for mod in ("app.js", "views.js", "util.js", "tracker.js"):
        src = client.get(f"/static/{mod}").text
        for rel in re.findall(r"from '\./([^']+)'", src):
            assert client.get(f"/static/{rel}").status_code == 200, (mod, rel)


def test_stylesheet_has_design_system_and_font_files(client):
    css = client.get("/static/app.css").text
    assert '[data-theme="dark"]' in css and "--brand-primary" in css and ".trow" in css and ".banner" in css
    for rel in re.findall(r'url\("([^"]+)"\)', css):
        assert client.get(f"/static/{rel}").status_code == 200, rel


def test_ui_assets_are_package_data():
    # hatch ships every file under src/myvec (packages = ["src/myvec"]); make sure nothing is left outside it
    for rel in ("index.html", "app.css", "app.js", "util.js", "views.js", "tracker.js", "mark.svg",
                "fonts/Geist-Variable.woff2", "fonts/GeistMono-Variable.woff2"):
        assert (UI / rel).is_file(), rel
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert 'packages = ["src/myvec"]' in pyproject


def test_unreachable_static_and_path_traversal(client):
    assert client.get("/static/../daemon.py").status_code in (404, 400)
    assert client.get("/static/%2e%2e/daemon.py").status_code in (404, 400)


# ------------------------------------------------------------------ CSRF on UI searches

def test_post_search_requires_csrf_and_returns_ui_fields(client, ws):
    body = {"q": "renderPdf", "cwd": str(ws / "site"), "scope": "project"}
    assert client.post("/api/search", json=body).status_code == 403
    assert client.post("/api/search", json=body, headers={"X-CSRF-Token": "no"}).status_code == 403
    r = client.post("/api/search", json=body, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["route"] == "hybrid" and d["tokens"] > 0 and d["ms"] >= 0
    assert d["expand_top"] == 3 and d["expand_lines"] == 25
    assert set(d["weights"]) >= {"current_repo", "global_memory", "other_project"}
    h = d["results"][0]
    assert h["breakdown"].keys() >= {"vector_rank", "fts_rank", "rrf", "similarity", "weight", "weight_eff", "route"} and h["kind"] == "code"
    assert h["repo"].startswith("acme") and h["path"] and h["start"] and h["text"]


def test_search_events_carry_project_repo_branch_route(client, ws):
    client.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H)
    client.post("/api/cli/search", json={"q": "how do invoices get rendered", "cwd": str(ws / "libs")}, headers=H)
    evs = [e for e in client.get("/api/events/recent?limit=500").json()["events"] if e["type"] == "search"]
    a, b = evs[-2], evs[-1]
    assert (a["project"], a["repo"], a["branch"], a["route"]) == ("acme", "acme/site", "main", "hybrid")
    assert (b["project"], b["repo"], b["branch"], b["route"]) == ("acme", "acme/libs", "main", "vector")


def test_direct_cli_search_log_has_context(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    res = searchcmd.run_search("renderPdf", ws / "site")
    assert (res.project, res.repo, res.branch, res.route) == ("acme", "acme/site", "main", "hybrid")
    rec = json.loads((config.logs_dir() / "queries.jsonl").read_text().splitlines()[-1])
    assert rec["project"] == "acme" and rec["repo"] == "acme/site" and rec["branch"] == "main" and rec["route"] == "hybrid"


# ------------------------------------------------------------------ /api/status extensions

def test_status_has_ui_fields(client, state, ws):
    write(ws / ".myvector.toml", 'name = "acme"\nshare_global = true\ninclude = []\nexclude = ["site-cloud"]\n')
    memory.remember(state.db(), get_embedder(config.load_config()), "the deploy pipeline is blue green", "acme")
    g = client.get("/api/status").json()["global"]
    acme = client.get("/api/status").json()["projects"][0]
    assert acme["exclude"] == ["site-cloud"]
    assert g["config"]["model"] == "embeddinggemma" and g["config"]["dims"] == 64 and g["config"]["window_hours"] == 2.0
    assert g["config"]["weights"]["current_repo"] == 1.0 and g["config"]["k"] == 10
    assert g["model_mismatch"] is False
    assert g["home"] == str(config.home())
    assert g["vault"]["path"] == str(config.vault_dir()) and g["vault"]["git"] is True
    assert g["vault"]["last_commit"] and abs(g["vault"]["last_commit"] - time.time()) < 120
    assert g["daemon"]["debounce_s"] == 10.0 and g["daemon"]["safety_s"] == 900.0
    assert g["daemon"]["watch_backend"] in ("FSEvents", "inotify", "watchdog")
    assert g["searches"] == {"count_24h": 0, "p50_ms": None, "p95_ms": None}


def test_status_flags_model_mismatch(client):
    cfg = config.home() / "config.toml"
    cfg.write_text(cfg.read_text().replace('model = "embeddinggemma"', 'model = "bge-m3"'))
    g = client.get("/api/status").json()["global"]
    assert g["model_mismatch"] is True and g["model"] == "embeddinggemma" and g["config"]["model"] == "bge-m3"
    cfg.write_text(cfg.read_text().replace('model = "bge-m3"', 'model = "embeddinggemma"').replace("dims = 64", "dims = 32"))
    assert client.get("/api/status").json()["global"]["model_mismatch"] is True    # dims differ as well


def test_search_stats_count_and_percentiles(client, state, ws):
    for ms_ in (10, 20, 30, 40, 100):
        state.bus.publish("search", q="x", ms=ms_, results=1, tokens=1, via="daemon")
    old = state.bus.publish("search", q="old", ms=9999, results=1, tokens=1, via="daemon")
    old["ts"] -= 2 * 86400                      # the ring holds the same dict: older than 24 h, must not count
    s = client.get("/api/status").json()["global"]["searches"]
    assert s["count_24h"] == 5 and s["p50_ms"] == 30 and s["p95_ms"] == 100


# ------------------------------------------------------------------ project detail

def test_project_detail_branches_manifest_and_states(client, ws):
    site = ws / "site"
    write(site / "extra.py", "def only_on_feature():\n    return 'quasar-feature'\n")
    git(site, "checkout", "-q", "-b", "feat/x")
    git(site, "add", "-A"); git(site, "commit", "-q", "-m", "x")
    do_sync(ws)
    git(site, "checkout", "-q", "-b", "scratch"); do_sync(ws)           # a branch that will vanish
    git(site, "checkout", "-q", "main"); git(site, "merge", "-q", "feat/x")  # feat/x becomes an ancestor of main
    git(site, "branch", "-q", "-D", "scratch")
    do_sync(ws)

    d = client.get("/api/projects/acme/detail").json()
    assert d["path"] == "acme" and d["config_file"].endswith(".myvector.toml") and d["mode"] == "auto"
    repos = {r["path"]: r for r in d["repos"]}
    s = repos["acme/site"]
    assert s["current_branch"] == "main" and s["files"] > 0 and s["manifest_bytes"] > 0 and s["chunks"] > 0
    br = {b["branch"]: b for b in s["branches"]}
    assert set(br) == {"main", "feat/x", "scratch"}
    assert br["main"]["current"] is True and br["main"]["state"] == "current"
    assert s["branches"][0]["branch"] == "main"                              # current branch first
    assert br["feat/x"]["state"] == "merged" and br["feat/x"]["merged_into"] == "main"
    assert br["scratch"]["state"] == "gone"
    assert br["main"]["files"] >= 1 and br["main"]["manifest_bytes"] > 0 and br["main"]["updated"] > 0
    assert br["main"]["chunks"] > 0 and s["unique_chunks"] >= br["main"]["chunks"]
    assert repos["acme"]["current_branch"] == "-" and [b["branch"] for b in repos["acme"]["branches"]] == ["-"]
    assert repos["acme"]["branches"][0]["state"] == "current"


def test_project_detail_secrets_excludes_and_watch(client, state, ws):
    write(ws / ".myvector.toml", 'name = "acme"\nshare_global = true\ninclude = []\nexclude = ["site-cloud"]\n')
    write(ws / ".gitignore", "# comment\nstorage/\n*.log\n")
    write(ws / "site" / "settings.py", 'DB_PASSWORD = "hunter2hunter2"\nRATE = 3\n')
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "s")
    do_sync(ws)
    client.post("/api/projects/acme/mode", json={"mode": "always"}, headers=H)
    r = client.get("/api/projects/acme/detail")
    d = r.json()
    assert "hunter2" not in r.text                                           # values are never exposed
    sec = d["secrets"]
    assert sec["masked"] >= 1 and sec["dropped"] == 1
    files = {(f["repo"], f["path"]): f for f in sec["files"]}
    assert files[("acme/site", "settings.py")]["masked"] >= 1
    assert files[("acme/site", "keys/notes.txt")]["dropped"] == 1
    assert all(set(f) == {"repo", "path", "masked", "dropped"} for f in sec["files"])     # no line / key name / value
    ex = d["exclude"]
    assert ex["project"] == ["site-cloud"] and ex["gitignore"] == ["storage/", "*.log"]
    assert "vendor" in ex["dirs"] and "*.pem" in ex["secrets"] and ".env" in ex["secrets"] and "*.lock" in ex["files"]
    assert not set(ex["secrets"]) & set(ex["files"])
    w = d["watch"]
    assert w["debounce_s"] == 10.0 and w["safety_s"] == 900.0 and w["repos"] == 3 and w["watching"] is False
    assert w["next_safety"] and 800 < w["next_safety"] - time.time() <= 900                # project is active (mode always)
    assert client.get("/api/projects/nope/detail").status_code == 404


def test_scheduler_safety_in():
    t = [100.0]
    s = Scheduler(debounce=10, safety=900, clock=lambda: t[0])
    assert s.safety_in("p") is None
    s.set_active({"p"})
    t[0] += 300
    assert s.safety_in("p") == 600
    t[0] += 5000
    assert s.safety_in("p") == 0.0
    s.set_active(set())
    assert s.safety_in("p") is None


# ------------------------------------------------------------------ recent searches per project

def test_project_searches_from_the_bus(client, state, ws):
    client.post("/api/search", json={"q": "renderPdf", "cwd": str(ws / "site")}, headers=H)
    client.post("/api/cli/search", json={"q": "invoices get rendered", "cwd": str(ws / "libs")}, headers=H)
    state.bus.publish("search", q="from direct cli", cwd=str(ws / "site"), ms=7, results=2, tokens=40, via="direct")
    state.bus.publish("search", q="elsewhere", cwd="/nowhere/else", ms=7, results=2, tokens=40, via="direct")
    d = client.get("/api/projects/acme/searches").json()
    qs = [s["q"] for s in d["searches"]]
    assert qs == ["from direct cli", "invoices get rendered", "renderPdf"]          # newest first, cwd fallback, no strangers
    assert d["total_24h"] == 3 and d["p50_ms"] is not None and d["p95_ms"] is not None
    assert d["searches"][2]["repo"] == "acme/site" and d["searches"][2]["route"] == "hybrid"
    assert len(client.get("/api/projects/acme/searches?limit=2").json()["searches"]) == 2
    assert client.get("/api/projects/nope/searches").status_code == 404


# ------------------------------------------------------------------ memory detail

def test_memory_get_with_related_links_and_archived(client, state):
    db, emb = state.db(), get_embedder(config.load_config())
    old, _ = memory.remember(db, emb, "the deploy pipeline is blue green", "acme")
    new, _ = memory.remember(db, emb, "deploy now goes through the red pipeline", "acme", supersedes=old.id, pin=True)
    r = client.get(f"/api/memories/{new.id}")
    assert r.status_code == 200
    m = r.json()["memory"]
    assert m["id"] == new.id and m["pinned"] is True and m["archived"] is False and m["scope"] == "acme"
    assert m["created"] and m["updated"] and m["vault_file"].endswith(f"{new.id}.md")
    assert [x["id"] for x in m["related"]] == [old.id] and m["related"][0]["stem"].endswith(old.id)
    o = client.get(f"/api/memories/{old.id}").json()["memory"]                 # archived ones stay readable
    assert o["archived"] is True and o["related"] == []
    assert client.get(f"/api/memories/{new.id[:7]}").json()["memory"]["id"] == new.id   # prefix lookup follows memory.find
    assert client.get("/api/memories/zzzzzzzz").status_code == 404
    # the list carries the same field
    listed = client.get("/api/memories?include_archived=1").json()["memories"]
    assert {x["id"]: x["related"] for x in listed}[new.id][0]["id"] == old.id


def test_memory_related_ignores_plain_text_links(client, state):
    db, emb = state.db(), get_embedder(config.load_config())
    m, _ = memory.remember(db, emb, "see [[Some note]] and [[2026-11-02-slug-537cfe2c]] for details", "acme")
    rel = client.get(f"/api/memories/{m.id}").json()["memory"]["related"]
    assert rel == [{"stem": "Some note", "id": None}, {"stem": "2026-11-02-slug-537cfe2c", "id": "537cfe2c"}]


# ------------------------------------------------------------------ what Claude gets (same formatter as the CLI)

def test_ui_search_matches_cli_output_and_token_count(client, state, ws):
    body = "".join(f"    total += compute_step_{i}(value) * {i}\n" for i in range(60))
    write(ws / "site" / "big.py", "def bigfunc_zz(value):\n    total = 0\n" + body + "    return total\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "big")
    do_sync(ws)
    cwd = str(ws / "site")
    d = client.post("/api/search", json={"q": "bigfunc_zz", "cwd": cwd}, headers=H).json()
    cli = client.post("/api/cli/search", json={"q": "bigfunc_zz", "cwd": cwd}, headers=H).json()
    assert d["tokens"] == len(cli["stdout"]) // 4                      # the CLI's estimate, not the JSON size
    code = [h for h in d["results"] if h["kind"] == "code" and h["in_output"]]
    assert [h["expanded"] for h in code][:3] == [True] * min(3, len(code)) and not any(h["expanded"] for h in code[3:])
    for h in code:
        limit = d["expand_lines"] if h["expanded"] else 3
        assert 0 < len(h["shown"].splitlines()) <= limit
    big = next(h for h in d["results"] if h["path"] == "big.py")
    assert big["expanded"] and len(big["text"].splitlines()) > 25 and len(big["shown"].splitlines()) == 25
    # every printed block of the CLI is a shown hit and vice versa
    assert cli["stdout"].count("\n[") + 1 == len([h for h in d["results"] if h["in_output"]])
    mem = [h for h in d["results"] if h["kind"] == "memory"]
    assert all(len(h["shown"]) <= 223 for h in mem)
    ev = [e for e in client.get("/api/events/recent?limit=500").json()["events"] if e["type"] == "search"]
    assert ev[-2]["tokens"] == d["tokens"] and ev[-1]["tokens"] == len(cli["stdout"]) // 4


def test_layout_hits_respects_budget_and_limits():
    from myvec import search as S

    def code(i, n):
        return S.Hit("code", ("c", i), score=1 / i, data={"repo": "r", "path": f"f{i}.py", "start": 1, "end": n,
                                                          "symbol": None, "text": "\n".join(f"line {j}" for j in range(n))})

    hits = [code(i, 40) for i in range(1, 8)]
    lay = S.layout_hits(hits, 100000, expand_top=2, expand_lines=5)
    assert [x["expanded"] for x in lay] == [True, True] + [False] * 5
    assert [len(x["shown"].splitlines()) for x in lay] == [5, 5, 3, 3, 3, 3, 3]
    assert S.format_hits(hits, 100000, expand_top=2, expand_lines=5) == "\n".join(x["block"] for x in lay)
    assert len(S.layout_hits(hits, 60, expand_top=2, expand_lines=5)) < 7                # budget cuts the tail
    assert len(S.layout_hits(hits, 1, expand_top=2, expand_lines=5)) == 1                # the first hit always stays


# ------------------------------------------------------------------ scope weights at the project root

def _weights(client, cwd):
    d = client.post("/api/search", json={"q": "def", "cwd": str(cwd), "k": 50}, headers=H).json()
    return d, {h["repo"]: h["breakdown"]["weight"] for h in d["results"] if h["kind"] == "code"}


def test_project_root_cwd_gives_every_repo_of_the_project_full_weight(client, ws):
    d, w = _weights(client, ws)                                   # cwd = project root, not inside any sub-repo
    assert d["context"]["repo"] == "acme"
    assert {"acme/site", "acme/libs"} <= set(w) and set(w.values()) == {1.0}
    _, w = _weights(client, ws / "site")                          # inside a sub-repo: the others are siblings
    assert w["acme/site"] == 1.0 and w["acme/libs"] == 0.8


def test_project_root_cwd_memory_weights(client, state, ws):
    db, emb = state.db(), get_embedder(config.load_config())
    memory.remember(db, emb, "libs memory about quasar helpers", "acme/libs")
    for cwd, want in ((ws, 1.0), (ws / "site", 0.8)):
        d = client.post("/api/search", json={"q": "quasar helpers", "cwd": str(cwd), "kind": "memory"}, headers=H).json()
        assert [h["breakdown"]["weight"] for h in d["results"]] == [want]


def test_status_user_home(client):
    from pathlib import Path
    assert client.get("/api/status").json()["global"]["user_home"] == str(Path.home())


# ------------------------------------------------------------------ tickets API (E1-06)

TK_GROUP = "---\nid: E9\ntitle: Epic nine\nkind: group\nstatus: in_progress\n---\n# E9\n"
TK_1 = """---
id: E9-01
title: Zebra <img src=x onerror=alert(1)>
status: in_progress
parent: E9
size: S
deps: []
blocks: [E9-02]
summary: Does the quokka thing
---
# Zebra

## Acceptance criteria

- [x] first
- [ ] second

## Log

<!-- template -->

### 2026-11-08 coder

Did **it**.
"""
TK_2 = "---\nid: E9-02\ntitle: Second\nstatus: todo\nparent: E9\nsize: S\ndeps: [E9-01]\nblocks: []\n---\n# S\n\n## Acceptance criteria\n\n- [ ] a\n"
TK_3 = ("---\nid: E9-03\ntitle: Leaky\nstatus: todo\nparent: E9\nsize: S\ndeps: []\nblocks: []\n---\n# L\n\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n")


@pytest.fixture
def tclient(ws):
    write(ws / "docs/todo/E9-x/index.md", TK_GROUP)
    write(ws / "docs/todo/E9-x/E9-01-zebra.md", TK_1)
    write(ws / "docs/todo/E9-x/E9-02-second.md", TK_2)
    write(ws / "docs/todo/E9-x/E9-03-leaky.md", TK_3)
    do_sync(ws)
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    yield TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
    st.executor.shutdown(wait=True)
    st.close()


def test_tickets_api_summary_tree_and_card(tclient):
    d = tclient.get("/api/tickets").json()
    p = next(x for x in d["projects"] if x["project_path"] == "acme")
    assert p["n_tickets"] == 4 and p["counts"] == {"in_progress": 1, "todo": 2}
    assert "n_issues" in p and p["progress"] is not None
    t = tclient.get("/api/tickets/acme/tree").json()
    assert t["project"] == "acme" and t["dir"] == "docs/todo" and {"progress", "counts", "nodes", "issues"} <= set(t)
    assert [n["id"] for n in t["nodes"]][:2] == ["E9", "E9-01"]
    c = tclient.get("/api/tickets/acme/ticket/E9-01").json()
    sec = c["sections"]
    assert [a["done"] for a in sec["ac"]] == [True, False]
    assert sec["log"] == [{"date": "2026-11-08", "agent": "coder", "text": "Did **it**."}]
    assert c["title"].startswith("Zebra <img")          # raw text: the UI escapes it


def test_tickets_api_errors_and_methods(tclient):
    assert tclient.get("/api/tickets/nope/tree").status_code == 404
    assert tclient.get("/api/tickets/acme/ticket/NOPE").status_code == 404
    assert tclient.get("/api/tickets/nope/ticket/E9-01").status_code == 404
    for u in ("/api/tickets", "/api/tickets/acme/tree", "/api/tickets/acme/ticket/E9-01"):
        assert tclient.post(u, headers=H, json={}).status_code == 405
        assert tclient.get(u, headers={"Host": "evil.com"}).status_code == 403


def test_tickets_api_hides_secret_body(tclient):
    c = tclient.get("/api/tickets/acme/ticket/E9-03").json()
    assert c["body_dropped"] is True and c["body"] is None and c["sections"] is None
    assert "PRIVATE KEY" not in json.dumps(tclient.get("/api/tickets/acme/tree").json())


def test_tickets_api_path_with_slash(ws):
    from myvec import db as dbmod, ticketdb
    do_sync(ws)
    db = dbmod.open_ready()
    db.execute("INSERT INTO ticket_dirs VALUES('grp/sub','docs/todo','x',0,0,0)")
    db.commit()
    db.close()
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    try:
        c = TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
        d = c.get("/api/tickets/grp/sub/tree").json()
        assert d["project"] == "grp/sub" and d["nodes"] == []
        assert any(x["project_path"] == "grp/sub" and x["n_tickets"] == 0 for x in c.get("/api/tickets").json()["projects"])
    finally:
        st.executor.shutdown(wait=True)
        st.close()


def test_tickets_api_path_traversal_is_rejected(tclient):
    for u in ("/api/tickets/../../etc/passwd/tree", "/api/tickets/%2e%2e/%2e%2e/tree",
              "/api/tickets/acme/ticket/..%2F..%2Fetc%2Fpasswd", "/api/tickets/acme/ticket/%2e%2e",
              "/api/tickets/..%2Facme/tree", "/api/tickets/acme/ticket/E9-01%00"):
        r = tclient.get(u)
        assert r.status_code in (404, 400), (u, r.status_code)
        assert "root:" not in r.text


def test_tickets_api_guard_applies_to_all_new_routes(tclient):
    for u in ("/api/tickets", "/api/tickets/acme/tree", "/api/tickets/acme/ticket/E9-01"):
        assert tclient.get(u, headers={"Host": "evil.com"}).status_code == 403
        assert tclient.get(u, headers={"Host": f"127.0.0.1:{PORT + 1}"}).status_code == 403
        for m in ("put", "delete", "patch"):
            assert getattr(tclient, m)(u, headers=H).status_code == 405
        # a state-changing call without the CSRF token never reaches the route
        assert tclient.post(u, json={}).status_code == 403
        assert tclient.get(u).status_code == 200


def test_tickets_api_tree_order_parents_before_children_and_progress(tclient):
    t = tclient.get("/api/tickets/acme/tree").json()
    ids = [n["id"] for n in t["nodes"]]
    assert set(ids) == {"E9", "E9-01", "E9-02", "E9-03"}
    pos = {i: k for k, i in enumerate(ids)}
    for n in t["nodes"]:
        if n["parent"]:
            assert pos[n["parent"]] < pos[n["id"]]
    by = {n["id"]: n for n in t["nodes"]}
    assert by["E9-01"]["progress"] == 50.0 and by["E9-02"]["progress"] == 0.0
    assert 0 <= t["progress"] <= 100 and t["counts"] == {"in_progress": 1, "todo": 2}
    assert isinstance(t["issues"], list)


def test_tickets_api_card_shape_deps_and_ac(tclient):
    c = tclient.get("/api/tickets/acme/ticket/E9-02").json()
    assert c["id"] == "E9-02" and c["deps"] == ["E9-01"] and c["status"] == "todo"
    assert c["sections"]["ac"] == [{"text": "a", "done": False}] and c["sections"]["log"] == []
    c1 = tclient.get("/api/tickets/acme/ticket/E9-01").json()
    assert c1["blocks"] == ["E9-02"]


def test_tickets_api_masks_secret_literal_in_title(ws):
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    write(ws / "docs/todo/E9-x/index.md", TK_GROUP)
    write(ws / "docs/todo/E9-x/E9-05-tok.md",
          f"---\nid: E9-05\ntitle: Tok {secret}\nstatus: todo\nparent: E9\nsize: S\ndeps: []\nblocks: []\n---\n# T\n")
    do_sync(ws)
    st = daemon.DaemonState(PORT, TOKEN)
    st.refresh()
    try:
        c = TestClient(daemon.create_app(st), base_url=f"http://127.0.0.1:{PORT}")
        for u in ("/api/tickets", "/api/tickets/acme/tree", "/api/tickets/acme/ticket/E9-05"):
            assert secret not in c.get(u).text, u
    finally:
        st.executor.shutdown(wait=True)
        st.close()


def test_tickets_api_summary_include_empty_via_route_and_problem_inputs(tclient, ws):
    from myvec import db as dbmod
    db = dbmod.open_ready()
    db.execute("INSERT INTO ticket_dirs VALUES('bare','docs/todo','x',0,0,0)")
    db.commit()
    db.close()
    ps = {x["project_path"]: x for x in tclient.get("/api/tickets").json()["projects"]}
    assert ps["bare"]["n_tickets"] == 0 and ps["bare"]["counts"] == {} and ps["bare"]["n_issues"] == 0
    assert "counts" in ps["acme"] and isinstance(ps["acme"]["n_issues"], int)
