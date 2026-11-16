import json
import os

import pytest
from typer.testing import CliRunner

from conftest import PHP, do_sync, git, write
from myvec import config, db as dbmod
from myvec.commands import app

runner = CliRunner()


def cli(*args, cwd=None, monkeypatch=None, input=None):
    if cwd is not None:
        os.chdir(cwd)
    return runner.invoke(app, list(args), input=input)


@pytest.fixture
def synced(ws, monkeypatch):
    do_sync(ws)
    monkeypatch.chdir(ws / "site")
    return ws


def search_json(*args):
    r = runner.invoke(app, ["search", *args, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def test_search_finds_code_in_current_repo(synced):
    hits = search_json("Invoice total price qty")
    assert hits[0]["kind"] == "code" and hits[0]["path"] == "src/Invoice.php"
    assert hits[0]["repo"] == "acme/site"


def test_search_text_format_and_budget(synced):
    r = runner.invoke(app, ["search", "invoice total", "--expand"])
    assert r.exit_code == 0
    assert r.stdout.startswith("[1] acme/site src/Invoice.php:")
    assert len(r.stdout) / 4 <= config.load_config().output_budget_tokens + 50
    # --full prints the entire chunk
    full = runner.invoke(app, ["search", "renderPdf", "--full", "--k", "1"])
    assert "function renderPdf" in full.stdout


def test_search_fts_identifier_substring_and_cyrillic(synced, ws):
    write(ws / "site/ru.md", "# Заметки\n\nОбработка накладных и платежей клиентов выполняется ночью.\n")
    git(ws / "site", "add", "-A"); git(ws / "site", "commit", "-q", "-m", "ru")
    do_sync(ws)
    assert search_json("helper_vat_rate", "--kind", "code")[0]["symbol"] == "helper_vat_rate"
    assert any("накладных" in h["text"] for h in search_json("накладных", "--kind", "code"))  # unicode61 FTS, cyrillic


def test_search_current_branch_only(synced, ws):
    site = ws / "site"
    assert search_json("feature_only_fn quasar_zeta", "--kind", "code", "--scope", "repo")[0]["path"] != "feature_only.py"
    git(site, "checkout", "-q", "-b", "feature")
    write(site / "feature_only.py", "def feature_only_fn():\n    return 'zeta'\n")
    git(site, "add", "-A"); git(site, "commit", "-q", "-m", "f")
    do_sync(ws)
    on_feature = search_json("feature_only_fn", "--kind", "code", "--scope", "repo")
    assert on_feature[0]["path"] == "feature_only.py"
    git(site, "checkout", "-q", "main")           # live HEAD is main, manifest for main exists
    on_main = search_json("feature_only_fn", "--kind", "code", "--scope", "repo")
    assert all(h["path"] != "feature_only.py" for h in on_main)
    git(site, "checkout", "-q", "feature")
    assert search_json("feature_only_fn", "--kind", "code", "--scope", "repo")[0]["path"] == "feature_only.py"


def test_scope_weighting_code(synced, ws, env):
    # identical file in libs and site
    for d in ("site", "libs"):
        write(ws / d / "same_tool.py", "def shared_tool_xyz():\n    return 'identical'\n")
        git(ws / d, "add", "-A"); git(ws / d, "commit", "-q", "-m", "same")
    do_sync(ws)
    os.chdir(ws / "site")
    h = [x for x in search_json("shared_tool_xyz", "--kind", "code") if x["path"] == "same_tool.py"]
    assert [x["repo"] for x in h] == ["acme/site", "acme/libs"]
    assert h[0]["score"] > h[1]["score"]
    # weights come from config: make siblings win and the order flips
    cfgp = env / "config.toml"
    cfgp.write_text(cfgp.read_text().replace("sibling_repo = 0.8", "sibling_repo = 3.0"))
    h = [x for x in search_json("shared_tool_xyz", "--kind", "code") if x["path"] == "same_tool.py"]
    assert [x["repo"] for x in h] == ["acme/libs", "acme/site"]
    cfgp.write_text(cfgp.read_text().replace("sibling_repo = 3.0", "sibling_repo = 0.8"))
    os.chdir(ws / "libs")
    h = [x for x in search_json("shared_tool_xyz", "--kind", "code") if x["path"] == "same_tool.py"]
    assert h[0]["repo"] == "acme/libs"
    # --scope repo restricts to the current repo
    only = search_json("shared_tool_xyz", "--kind", "code", "--scope", "repo")
    assert {x["repo"] for x in only} == {"acme/libs"}


def test_scope_weighting_memory(synced):
    assert runner.invoke(app, ["remember", "deploy runs through the blue pipeline"]).exit_code == 0
    assert runner.invoke(app, ["remember", "deploy runs through the blue pipeline!!", "--global"]).exit_code == 0
    hits = [h for h in search_json("deploy blue pipeline", "--kind", "memory", "--mode", "hybrid")]
    assert [h["scope"] for h in hits] == ["acme", "global"]
    assert hits[0]["score"] / hits[1]["score"] == pytest.approx(1 / 0.8, rel=1e-2)   # softened: 0.6 -> 0.8
    # repo scope: no global memories
    assert [h["scope"] for h in search_json("deploy blue pipeline", "--kind", "memory", "--scope", "repo")] == ["acme"]


def test_other_project_only_with_scope_all(synced, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    write(other / "mod.py", "def other_project_unique_marker():\n    return 'borealis'\n")
    write(other / ".myvector.toml", 'name = "other"\n')
    do_sync(other)
    monkeypatch.chdir(synced / "site")
    assert not [h for h in search_json("other_project_unique_marker", "--kind", "code") if h["repo"] == "other"]
    allh = [h for h in search_json("other_project_unique_marker", "--kind", "code", "--scope", "all") if h["repo"] == "other"]
    assert allh


def test_remember_dedup_updates_existing(synced, env):
    r1 = runner.invoke(app, ["remember", "use postgres for the billing database", "--tag", "db"])
    assert "saved memory" in r1.stdout
    r2 = runner.invoke(app, ["remember", "use postgres for the billing database", "--pin"])
    assert "updated existing memory" in r2.stdout
    files = list((env / "vault/projects/acme").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "pinned: true" in text and "tags: [db]" in text and "scope: acme" in text
    r3 = runner.invoke(app, ["remember", "completely different note about invoices and pdf rendering"])
    assert "saved memory" in r3.stdout
    assert len(list((env / "vault/projects/acme").glob("*.md"))) == 2
    # file name: date-slug-id
    assert any(f.name.split("-")[3].startswith("use") for f in files)


def test_remember_scopes_and_flags(synced, env):
    r = runner.invoke(app, ["remember", "repo level fact about the site", "--repo", "--branch"])
    assert r.exit_code == 0, r.output
    f = next((env / "vault/projects/acme/site").glob("*.md"))
    t = f.read_text()
    assert "scope: acme/site" in t and "branch: main" in t and "commit: " in t
    r = runner.invoke(app, ["remember", "global preference: be terse", "--global"])
    assert list((env / "vault/global").glob("*.md"))
    r = runner.invoke(app, ["remember", "aws key AKIAIOSFODNN7EXAMPLE for the bucket"])
    assert r.exit_code != 0 and "secret" in r.output


def test_remember_outside_project_needs_global(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["remember", "x y z"]).exit_code != 0
    assert runner.invoke(app, ["remember", "x y z", "--global"]).exit_code == 0


def test_related_wikilink_and_supersedes(synced, env):
    runner.invoke(app, ["remember", "the api rate limit is 100 requests per minute for partners"])
    r = runner.invoke(app, ["remember", "the api rate limit is 100 requests per minute for partners and 10 for guests"])
    assert r.exit_code == 0
    db = dbmod.open_ready()
    old = db.execute("SELECT id FROM memories ORDER BY n LIMIT 1").fetchone()[0]
    r = runner.invoke(app, ["remember", "rate limit changed: 500 per minute everywhere", "--supersedes", old])
    assert r.exit_code == 0, r.output
    assert db.execute("SELECT superseded_by IS NOT NULL FROM memories WHERE id=?", (old,)).fetchone()[0] == 1
    new = next(f for f in (env / "vault/projects/acme").glob("*.md") if "500" in f.read_text())
    assert "[[" in new.read_text() and f"supersedes: [{old}]" in new.read_text()
    assert list((env / "vault/_archive/projects/acme").glob(f"*{old}.md"))


def test_forget_archives_never_deletes(synced, env):
    runner.invoke(app, ["remember", "temporary fact about zanzibar deployment"])
    db = dbmod.open_ready()
    mid, = db.execute("SELECT id FROM memories").fetchone()
    assert runner.invoke(app, ["forget", mid]).exit_code == 0
    assert not list((env / "vault/projects/acme").glob("*.md"))
    assert list((env / "vault/_archive").rglob(f"*{mid}.md"))
    assert not [h for h in search_json("zanzibar", "--kind", "memory")]
    assert runner.invoke(app, ["forget", mid]).exit_code != 0


def test_pin_and_context(synced, env, tmp_path):
    runner.invoke(app, ["remember", "alpha note about invoicing"])
    runner.invoke(app, ["remember", "pinned global rule: always answer in Ukrainian", "--global"])
    db = dbmod.open_ready()
    gid, = db.execute("SELECT id FROM memories WHERE scope='global'").fetchone()
    assert runner.invoke(app, ["pin", gid]).exit_code == 0
    assert "pinned: true" in next((env / "vault/global").glob("*.md")).read_text()
    r = runner.invoke(app, ["context"])
    assert "project: acme" in r.stdout and "repo: acme/site" in r.stdout and "branch: main" in r.stdout
    assert "alpha note" in r.stdout and "always answer in Ukrainian" in r.stdout
    assert len(r.stdout) <= 1200
    os.chdir(tmp_path)
    assert runner.invoke(app, ["context"]).stdout.strip() == ""


def test_share_global_false_hides_global(synced, env):
    runner.invoke(app, ["remember", "global gamma insight about pricing", "--global"])
    write(synced / ".myvector.toml", 'name = "acme"\nshare_global = false\n')
    assert not search_json("gamma insight pricing", "--kind", "memory")


def test_rebuild_from_vault_after_deleting_index(synced, env):
    runner.invoke(app, ["remember", "rebuild survivor memory about kubernetes"])
    runner.invoke(app, ["remember", "global survivor about tabs", "--global"])
    (env / "index.db").unlink()
    for ext in ("-wal", "-shm"):
        p = env / f"index.db{ext}"
        if p.exists():
            p.unlink()
    r = runner.invoke(app, ["rebuild", "--no-import"])
    assert r.exit_code == 0, r.output
    assert "restored 2 memories" in r.stdout
    hits = search_json("kubernetes survivor")
    kinds = {h["kind"] for h in hits}
    assert kinds == {"memory", "code"} or "memory" in kinds
    assert any(h["kind"] == "memory" and "kubernetes" in h["text"] for h in hits)
    assert search_json("helper_vat_rate", "--kind", "code")[0]["symbol"] == "helper_vat_rate"  # code re-synced


def test_import_is_read_only_and_idempotent(synced, env, tmp_path):
    claude = tmp_path / "claude"
    enc = "".join(c if c.isalnum() else "-" for c in str(synced.resolve()))
    write(claude / "projects" / enc / "memory" / "feedback_x.md", "# Style\n\nPrefer tabs over spaces in the legacy module.\n")
    write(synced / "CLAUDE.md", "# Rules\n\nAlways run the linter before committing anything at all.\n")
    write(synced / "site/graphify-out/GRAPH_REPORT.md", "# Graph\n\nGod node is InvoiceService with many edges and communities.\n")
    before = {p: p.read_text() for p in claude.rglob("*.md")}
    r = runner.invoke(app, ["import"])
    assert r.exit_code == 0, r.output
    db = dbmod.open_ready()
    n1 = db.execute("SELECT count(*) FROM memories WHERE source='import'").fetchone()[0]
    assert n1 >= 3
    assert runner.invoke(app, ["import"]).exit_code == 0
    assert db.execute("SELECT count(*) FROM memories WHERE source='import'").fetchone()[0] == n1
    assert not list((env / "vault").rglob("feedback_x*"))               # not copied into the vault
    assert before == {p: p.read_text() for p in claude.rglob("*.md")}  # untouched
    assert any("Prefer tabs" in h["text"] for h in search_json("prefer tabs spaces", "--kind", "memory"))


def test_import_graph_report_keeps_overview_sections_only(synced, env):
    write(synced / "site/graphify-out/GRAPH_REPORT.md",
          "# Graph Report - site\n\n## Summary\n\n120 nodes, 300 edges across the billing code.\n\n"
          "## God Nodes (most connected)\n\n1. InvoiceService - 42 edges\n\n"
          "## Communities (2 total)\n\n### Community 1 - \"Billing\"\n\nNodes (3): a, b, c\n\n"
          "### Community 2 - \"Shop\"\n\nNodes (2): d, e\n")
    assert runner.invoke(app, ["import"]).exit_code == 0
    db = dbmod.open_ready()
    texts = [r[0] for r in db.execute("SELECT text FROM memories WHERE source='import' AND vault_file LIKE '%GRAPH_REPORT.md'")]
    assert any("InvoiceService" in t for t in texts) and any("300 edges" in t for t in texts)
    assert not any("Community" in t for t in texts)


def _claude_memory_dir(synced, tmp_path):
    enc = "".join(c if c.isalnum() else "-" for c in str(synced.resolve()))
    return tmp_path / "claude" / "projects" / enc / "memory"


NOTE = """---
name: project-dev-environment
description: "Local dev environment of the site, see the env brief"
metadata:
  type: project
---
Run the stack with docker compose and open the zebra dashboard.
"""


def test_import_strips_frontmatter_and_updates_in_place(synced, env, tmp_path):
    f = _claude_memory_dir(synced, tmp_path) / "project-dev-environment.md"
    write(f, NOTE)
    assert runner.invoke(app, ["import"]).exit_code == 0
    db = dbmod.open_ready()
    rows = db.execute("SELECT n, id, text, vault_file FROM memories WHERE source='import'").fetchall()
    mine = [r for r in rows if r["vault_file"] == str(f)]
    assert len(mine) == 1
    text = mine[0]["text"]
    assert text.splitlines()[0] == "Local dev environment of the site, see the env brief"
    assert "zebra dashboard" in text
    for junk in ("---", "metadata:", "name: project-dev-environment", "type: project"):
        assert junk not in text
    # the printed one-liner starts with the description
    r = runner.invoke(app, ["search", "zebra dashboard docker compose", "--kind", "memory"])
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("[M"))
    assert line.split(": ", 1)[1].startswith("Local dev environment of the site")
    assert "metadata:" not in line and "---" not in line
    # re-import after an edit: updated in place, no duplicates
    write(f, NOTE.replace("zebra dashboard", "quokka dashboard"))
    assert runner.invoke(app, ["import"]).exit_code == 0
    mine = [r for r in db.execute("SELECT text, vault_file FROM memories WHERE source='import'")
            if r["vault_file"] == str(f)]
    assert len(mine) == 1 and "quokka dashboard" in mine[0]["text"] and "zebra" not in mine[0]["text"]
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == db.execute(
        "SELECT count(*) FROM memories_vec").fetchone()[0]


def test_import_frontmatter_without_description_uses_name(synced, env, tmp_path):
    f = _claude_memory_dir(synced, tmp_path) / "n.md"
    write(f, "---\nname: just-a-name\n---\nThe body of the note about the quokka cache.\n")
    assert runner.invoke(app, ["import"]).exit_code == 0
    db = dbmod.open_ready()
    (text,) = [r[0] for r in db.execute("SELECT text FROM memories WHERE source='import' AND vault_file=?", (str(f),))]
    assert text.splitlines()[0] == "just-a-name" and "quokka cache" in text and "---" not in text


def _import_notes(synced, tmp_path, n=5):
    mdir = _claude_memory_dir(synced, tmp_path)
    for i in range(n):
        write(mdir / f"note{i}.md", f"---\nname: note{i}\ndescription: Invoice total price qty note {i}\n---\n"
                                    f"Invoice total price qty for case {i}, remember it.\n")
    assert runner.invoke(app, ["import"]).exit_code == 0


def test_memory_hits_are_capped_in_merged_output(synced, env, tmp_path):
    _import_notes(synced, tmp_path)
    cfg = env / "config.toml"
    q = "Invoice total price qty"
    assert config.load_config().max_memory_hits == 3
    cfg.write_text(cfg.read_text().replace("max_memory_hits = 3", "max_memory_hits = 99")
                   .replace("import_weight = 0.85", "import_weight = 1.0"))
    uncapped = search_json(q, "--k", "10")
    assert sum(h["kind"] == "memory" for h in uncapped) == 5   # the fixture really would crowd the output
    cfg.write_text(cfg.read_text().replace("max_memory_hits = 99", "max_memory_hits = 3"))
    hits = search_json(q, "--k", "10")
    assert sum(h["kind"] == "memory" for h in hits) == 3
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    # the freed slots went to code
    assert sum(h["kind"] == "code" for h in hits) >= sum(h["kind"] == "code" for h in uncapped)
    # --kind memory is unaffected by the cap
    assert len(search_json(q, "--kind", "memory", "--k", "5")) == 5


def test_import_weight_ranks_curated_memories_first(synced, env, tmp_path):
    text = "Deploy goes through the blue pipeline, never by hand."
    assert runner.invoke(app, ["remember", text]).exit_code == 0
    write(_claude_memory_dir(synced, tmp_path) / "deploy.md", f"---\nname: deploy\ndescription: {text}\n---\n")
    assert runner.invoke(app, ["import"]).exit_code == 0
    hits = search_json(text, "--kind", "memory", "--mode", "vector")
    assert [h["source"] for h in hits] == ["vault", "import"]
    assert hits[1]["score"] / hits[0]["score"] == pytest.approx(0.925, rel=1e-2)   # softened: 0.85 -> 0.925


def test_soften_formula():
    from myvec.search import soften

    assert soften(0.6, 0.5) == pytest.approx(0.8) and soften(0.8, 0.5) == pytest.approx(0.9)
    assert soften(0.85, 0.5) == pytest.approx(0.925) and soften(1.0, 0.5) == 1.0
    assert soften(0.6, 0.0) == 1.0 and soften(0.6, 1.0) == pytest.approx(0.6)
    assert config.load_config().weight_softness == 0.5


def test_closest_global_memory_outranks_weighted_siblings_and_imports(synced, tmp_path, monkeypatch):
    q = "open the local search interface in the browser at port 7878"
    assert runner.invoke(app, ["remember", "--global", q]).exit_code == 0                      # the exact match
    monkeypatch.chdir(synced / "libs")
    assert runner.invoke(app, ["remember", "--repo", "laravel boost search interface setup for the browser"]).exit_code == 0
    monkeypatch.chdir(synced / "site")
    write(_claude_memory_dir(synced, tmp_path) / "boost.md",
          "---\nname: boost\ndescription: the local boost interface in a browser, other port notes\n---\n")
    assert runner.invoke(app, ["import"]).exit_code == 0
    hits = search_json(q, "--kind", "memory", "--mode", "vector")
    assert len(hits) >= 3 and {h["scope"] for h in hits} >= {"global", "acme/libs", "acme"}
    assert hits[0]["scope"] == "global" and hits[0]["text"] == q
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert hits[0]["score"] > hits[1]["score"] * 1.2               # a clear win, not a tie broken by the weights
    # one merged list: the exact memory also leads the mixed code + memory output
    assert search_json(q, "--mode", "vector")[0]["scope"] == "global"


def test_vector_route_weights_are_a_tie_breaker(synced, ws, monkeypatch):
    for d in ("site", "libs"):
        write(ws / d / "same_tool.py", "def shared_tool_xyz():\n    return 'identical'\n")
        git(ws / d, "add", "-A"); git(ws / d, "commit", "-q", "-m", "same")
    do_sync(ws)
    h = [x for x in search_json("the shared tool function that returns identical", "--kind", "code", "--mode", "vector")
         if x["path"] == "same_tool.py"]
    assert [x["repo"] for x in h] == ["acme/site", "acme/libs"]       # equally similar: the current repo wins
    assert h[1]["score"] / h[0]["score"] == pytest.approx(0.9, rel=1e-3)
    # a clearly closer sibling hit beats the current-repo one: weights do not override similarity
    write(ws / "libs" / "closer.py", "def where_is_the_quasar_pipeline_configured():\n    return 'quasar pipeline'\n")
    git(ws / "libs", "add", "-A"); git(ws / "libs", "commit", "-q", "-m", "closer")
    do_sync(ws)
    top = search_json("def where_is_the_quasar_pipeline_configured(): return 'quasar pipeline'", "--kind", "code", "--mode", "vector")[0]
    assert top["path"] == "closer.py" and top["repo"] == "acme/libs"


def test_hybrid_score_is_rrf_normalised_to_unit_range(synced):
    from myvec import ctx as ctxmod, search as S
    from myvec.embed import get_embedder

    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    c = ctxmod.current(db, synced / "site")
    hits = S.search(db, cfg, get_embedder(cfg), "renderPdf", c, kind="code")
    assert hits and all(h.route == "hybrid" for h in hits)
    for h in hits:
        assert 0 < h.score <= 1
        assert h.score == pytest.approx(h.rrf / (2 / 61) * h.weight_eff)
    both = [h for h in hits if h.ranks.get("code_vec") == 1 and h.ranks.get("code_fts") == 1]
    for h in both:   # rank 1 in both lists is the best possible: normalised RRF = 1
        assert h.rrf == pytest.approx(2 / 61) and h.score == pytest.approx(h.weight_eff)
    bd = S.to_json(hits, breakdown=True)[0]["breakdown"]
    assert bd.keys() >= {"vector_rank", "fts_rank", "rrf", "similarity", "weight", "weight_eff", "route"}


def test_topics(synced, env):
    for t in ["postgres tuning for invoices", "postgres index on invoices", "frontend button colors", "frontend css grid layout"]:
        runner.invoke(app, ["remember", t])
    r = runner.invoke(app, ["topics"])
    assert r.exit_code == 0, r.output
    note = (env / "vault/_topics/acme.md").read_text()
    assert "[[" in note


def test_model_mismatch_refuses(synced, env):
    (env / "config.toml").write_text(config.default_config_text().replace("dims = 768", "dims = 32"))
    r = runner.invoke(app, ["search", "invoice"])
    assert r.exit_code == 2 and "reembed" in r.output
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 2 and "reembed" in r.output
    r = runner.invoke(app, ["reembed"])
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["search", "invoice"]).exit_code == 0


def test_status(synced):
    r = runner.invoke(app, ["status"])
    assert "acme/site" in r.stdout and "branch=main" in r.stdout and "skipped secret chunks: 1" in r.stdout


def test_context_has_search_hint_without_memories(synced):
    from myvec import context, hooks
    out = context.build(synced / "site")
    assert hooks.SEARCH_HINT in out and "project: acme" in out
    assert "remember" not in out


def test_context_hint_survives_memories_overflowing_max_chars(synced):
    from myvec import context, hooks
    for i in range(6):
        runner.invoke(app, ["remember", f"note {i} " + "very long memory text about invoicing " * 12])
    out = context.build(synced / "site")
    assert hooks.SEARCH_HINT in out and "Project memories:" in out
    assert len(out) <= context.MAX_CHARS


def test_context_outside_project_is_empty_without_hint(tmp_path):
    from myvec import context
    assert context.build(tmp_path) == ""
