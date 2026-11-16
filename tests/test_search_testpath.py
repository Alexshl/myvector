"""E8-20: test files are down-weighted for natural-language (vector-route) queries without a test word."""
import pytest
from conftest import do_sync, git, write
from myvec import config, ctx as ctxmod
from myvec import search as S

BODY = "def quasar_gate(token):\n    # opens the quasar gate when the nebula token matches\n    return token == 'nebula'\n"


@pytest.mark.parametrize("path", [
    "tests/test_a.py", "test/x.py", "src/__tests__/a.ts", "spec/a.rb", "pkg/test_mod.py", "pkg/mod_test.py",
    "pkg/mod_test.go", "web/a.test.ts", "web/a.spec.js", "app/FooTest.php", "TESTS/a.py", "Pkg/Test_A.PY",
])
def test_is_test_path_true(path):
    assert S.is_test_path(path)


@pytest.mark.parametrize("path", [
    "src/testing_utils.py", "src/contest.py", "src/latest_x.py", "src/filters.py", "docs/spec.md",
    "src/mod_test.rs", "a/testsuite/b.py", "src/protest.ts", "app/latest.php", "app/contest.php",
])
def test_is_test_path_false(path):
    assert not S.is_test_path(path)


@pytest.mark.parametrize("q", ["тест на маскування", "тести команди", "как проверить тесты", "how are tests run",
                               "pytest fixture setup", "фікстура env", "the spec file"])
def test_mentions_test_true(q):
    assert S.mentions_test(q)


@pytest.mark.parametrize("q", ["де маскуються секрети", "latest contest", "как работает замок", "open the gate"])
def test_mentions_test_false(q):
    assert not S.mentions_test(q)


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write(root / "src/gate.py", BODY)
    write(root / "tests/test_gate.py", BODY + "\n")  # a different blob: identical content is stored once
    write(root / ".myvector.toml", 'name = "p"\nshare_global = true\ninclude = []\nexclude = []\n')
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _run(proj, query, weight, **kw):
    _, db = do_sync(proj)
    cfg = config.load_config()
    cfg.test_path_weight = weight
    c = ctxmod.current(db, str(proj))
    from myvec.embed import get_embedder
    q = kw.pop("qtext", query)
    hits = S.search(db, cfg, get_embedder(cfg), q, c, k=10, kind="code", **kw)
    db.close()
    return {h.data["path"]: h for h in hits}


def test_nl_query_puts_test_below_code(proj):
    hits = _run(proj, "opens the quasar gate when the nebula token matches", 0.8)
    t, s = hits["tests/test_gate.py"], hits["src/gate.py"]
    assert t.path_weight == 0.8 and s.path_weight == 1.0
    for h in (t, s):
        assert h.score == pytest.approx(h.similarity * h.weight_eff * h.path_weight)
    base_t = _run(proj, "opens the quasar gate when the nebula token matches", 1.0)["tests/test_gate.py"]
    assert t.score == pytest.approx(base_t.score * 0.8)


def test_weight_one_is_off(proj):
    hits = _run(proj, "opens the quasar gate when the nebula token matches", 1.0)
    assert all(h.path_weight == 1.0 for h in hits.values())
    assert all(h.score == pytest.approx(h.similarity * h.weight_eff) for h in hits.values())


def test_test_word_in_query_keeps_score(proj):
    hits = _run(proj, "test opens the quasar gate when the nebula token matches", 0.8)
    assert all(h.path_weight == 1.0 for h in hits.values())


def test_identifier_route_unchanged(proj):
    hits = _run(proj, "quasar_gate", 0.8)
    assert all(h.route == "hybrid" and h.path_weight == 1.0 for h in hits.values())


def test_fts_fallback_unchanged(proj):
    hits = _run(proj, "opens the quasar gate", 0.8, qvec=None)
    assert hits and all(h.path_weight == 1.0 for h in hits.values())


def test_breakdown_has_path_weight(proj):
    hits = _run(proj, "opens the quasar gate when the nebula token matches", 0.8)
    out = S.to_json(list(hits.values()), breakdown=True)
    assert {d["breakdown"]["path_weight"] for d in out} == {0.8, 1.0}


def test_config_test_path_weight_clamped_and_written():
    assert config.load_config().test_path_weight == 0.8
    assert "test_path_weight = 0.8" in config.default_config_text()
    p = config.home() / "config.toml"
    p.write_text(p.read_text().replace("test_path_weight = 0.8", "test_path_weight = 5"))
    assert config.load_config().test_path_weight == 1.0
    p.write_text(p.read_text().replace("test_path_weight = 5", "test_path_weight = -1"))
    assert config.load_config().test_path_weight == 0.0


@pytest.mark.parametrize("q", ["attestation of the gate", "contestant list", "latest protest", "greatest hits"])
def test_mentions_test_not_triggered_by_inner_substring(q):
    assert not S.mentions_test(q)


@pytest.mark.parametrize("q", ["testament file", "tester role", "tests/test_a.py"])
def test_mentions_test_prefix_match_is_conservative(q):
    # \btest prefix: such queries keep full test-file score (safe direction, no demotion)
    assert S.mentions_test(q)


@pytest.mark.parametrize("path", ["src/testing/helpers.py", "src/contest/a.py", "src/latest/a.py", "a/specs/b.py"])
def test_is_test_path_false_for_lookalike_dirs(path):
    assert not S.is_test_path(path)


def test_is_test_path_windows_separators():
    assert S.is_test_path("pkg\\tests\\a.py")


def test_kind_all_memory_hits_have_path_weight_one(proj):
    from myvec import memory, db as dbmod
    from myvec.embed import get_embedder
    _, db = do_sync(proj)
    cfg = config.load_config()
    memory.remember(db, get_embedder(cfg), "opens the quasar gate when the nebula token matches", "p")
    cfg.test_path_weight = 0.5
    c = ctxmod.current(db, str(proj))
    hits = S.search(db, cfg, get_embedder(cfg), "opens the quasar gate when the nebula token matches", c, k=10, kind="all")
    db.close()
    mem = [h for h in hits if h.kind == "memory"]
    assert mem and all(h.path_weight == 1.0 for h in mem)
    assert any(h.kind == "code" and h.path_weight == 0.5 for h in hits)
