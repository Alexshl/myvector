"""E8-21: tickets (`docs/todo/**`) are down-weighted for natural-language (vector-route) queries without a docs word."""
import pytest
from conftest import do_sync, git, write
from myvec import config, ctx as ctxmod
from myvec import search as S

BODY = "def quasar_gate(token):\n    # opens the quasar gate when the nebula token matches\n    return token == 'nebula'\n"
NL = "opens the quasar gate when the nebula token matches"


@pytest.mark.parametrize("path", [
    "docs/todo/E1/x.md", "docs/todo/E1/E1-01-a.md", "sub/docs/todo/a.rst", "DOCS/TODO/a.md", "docs\\todo\\a.md",
])
def test_is_ticket_path_true(path):
    assert S.is_ticket_path(path)


@pytest.mark.parametrize("path", [
    "README.md", "docs/decisions.md", "skill/x/SKILL.md", "docs/todo/x.py", "mydocs/todo/a.md", "docs/todo.md",
    "src/todo/a.md", "docs/lessons.md", "docs/design/todo/a.md",
])
def test_is_ticket_path_false(path):
    assert not S.is_ticket_path(path)


@pytest.mark.parametrize("q", ["тікет про зупинку демона", "тикет про остановку", "що вирішили про вагу", "план автентифікації",
                               "чому обрали модель", "почему так", "why this model", "ADR about weights", "the plan",
                               "документація", "уроки міграцій", "аудит безопасности", "readme text",
                               "decision on ranking", "звіт про тести"])
def test_mentions_docs_true(q):
    assert S.mentions_docs(q)


@pytest.mark.parametrize("q", ["як влаштований вотчер", "как устроен демон", "how does the scheduler sync", "open the gate",
                               "повільна зупинка демона з відкритим SSE", "shadow of the gate"])
def test_mentions_docs_false(q):
    assert not S.mentions_docs(q)


@pytest.mark.parametrize("q", ["dockerfile build", "adroit gate", "docker compose up"])
def test_mentions_docs_prefix_false_positives_accepted(q):
    # ACCEPTED (E8-21 review Minor): the docs words are prefix matches, so "doc"/"adr" also hit "docker"/"adroit".
    # The cost is only that tickets keep their full score for such queries (the old ranking); not docs intent.
    assert S.mentions_docs(q)


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write(root / "src/gate.py", BODY)
    write(root / "docs/todo/E1-01-gate.md", "# gate\n" + BODY + "\n")
    write(root / "docs/note.md", "# note\n" + BODY + "\n\n")
    write(root / "tests/docs/a.md", "# t\n" + BODY + "\n\n\n")
    write(root / ".myvector.toml", 'name = "p"\nshare_global = true\ninclude = []\nexclude = []\n')
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _run(proj, query, weight, test_w=0.8, kind="code", **kw):
    _, db = do_sync(proj)
    cfg = config.load_config()
    cfg.doc_path_weight = weight
    cfg.test_path_weight = test_w
    c = ctxmod.current(db, str(proj))
    from myvec.embed import get_embedder
    hits = S.search(db, cfg, get_embedder(cfg), query, c, k=10, kind=kind, **kw)
    db.close()
    return {h.data["path"]: h for h in hits}


def test_nl_query_puts_ticket_below_code(proj):
    hits = _run(proj, NL, 0.5)
    t = hits["docs/todo/E1-01-gate.md"]
    assert t.path_weight == 0.5
    assert hits["src/gate.py"].path_weight == 1.0 and hits["docs/note.md"].path_weight == 1.0
    assert t.score == pytest.approx(t.similarity * t.weight_eff * 0.5)
    base = _run(proj, NL, 1.0)["docs/todo/E1-01-gate.md"]
    assert t.score == pytest.approx(base.score * 0.5)


def test_weight_one_is_off(proj):
    hits = _run(proj, NL, 1.0, test_w=1.0)
    assert all(h.path_weight == 1.0 for h in hits.values())
    assert all(h.score == pytest.approx(h.similarity * h.weight_eff) for h in hits.values())


def test_docs_word_keeps_score(proj):
    hits = _run(proj, "тікет: " + NL, 0.5, test_w=1.0)
    assert all(h.path_weight == 1.0 for h in hits.values())


def test_identifier_route_unchanged(proj):
    hits = _run(proj, "quasar_gate", 0.5)
    assert hits and all(h.route == "hybrid" and h.path_weight == 1.0 for h in hits.values())


def test_fts_fallback_unchanged(proj):
    hits = _run(proj, "opens the quasar gate", 0.5, qvec=None)
    assert hits and all(h.path_weight == 1.0 for h in hits.values())


def test_kind_docs_unchanged(proj):
    hits = _run(proj, NL, 0.5, test_w=1.0, kind="docs")
    assert "docs/todo/E1-01-gate.md" in hits and all(h.path_weight == 1.0 for h in hits.values())


def test_test_and_ticket_weights_multiply(proj):
    write(proj / "docs/todo/tests/b.md", "# b\n" + BODY + "\n\n\n\n")
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "more")
    hits = _run(proj, NL, 0.5, test_w=0.8)
    assert hits["docs/todo/tests/b.md"].path_weight == pytest.approx(0.4)
    assert hits["tests/docs/a.md"].path_weight == 0.8
    assert hits["docs/todo/E1-01-gate.md"].path_weight == 0.5


def test_breakdown_has_path_weight(proj):
    hits = _run(proj, NL, 0.5)
    out = S.to_json(list(hits.values()), breakdown=True)
    assert 0.5 in {d["breakdown"]["path_weight"] for d in out}


def test_config_doc_path_weight_clamped_and_written():
    assert config.load_config().doc_path_weight == 0.9
    assert "doc_path_weight = 0.9" in config.default_config_text()
    p = config.home() / "config.toml"
    p.write_text(p.read_text().replace("doc_path_weight = 0.9", "doc_path_weight = 5"))
    assert config.load_config().doc_path_weight == 1.0
    p.write_text(p.read_text().replace("doc_path_weight = 5", "doc_path_weight = -1"))
    assert config.load_config().doc_path_weight == 0.0


def test_forced_hybrid_mode_exempt_for_nl_query(proj):
    hits = _run(proj, NL, 0.5, test_w=1.0, mode="hybrid")
    assert hits and all(h.route == "hybrid" and h.path_weight == 1.0 for h in hits.values())


def test_forced_vector_mode_applies_to_identifier(proj):
    hits = _run(proj, "quasar_gate", 0.5, mode="vector")
    assert hits["docs/todo/E1-01-gate.md"].path_weight == 0.5


def test_weight_one_restores_old_order(proj):
    off = _run(proj, NL, 1.0, test_w=1.0)
    on = _run(proj, NL, 0.0, test_w=1.0)
    assert on["docs/todo/E1-01-gate.md"].score == 0.0
    assert off["docs/todo/E1-01-gate.md"].score > 0
    assert on["src/gate.py"].score == pytest.approx(off["src/gate.py"].score)
