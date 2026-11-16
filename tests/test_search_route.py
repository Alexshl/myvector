"""E8-08: the route reported (and logged) is the one actually taken: no query vector -> 'fts-fallback'."""
from conftest import do_sync
from myvec import config, ctx as ctxmod
from myvec import search as S


def test_effective_route():
    assert S.effective_route("a b", "auto", None) == "fts-fallback"
    assert S.effective_route("a b", "auto", [0.1]) == "vector"
    assert S.effective_route("ident", "auto", [0.1]) == "hybrid"
    assert S.effective_route("ident", "hybrid", None) == "fts-fallback"


def test_search_without_vector_labels_hits_fts_fallback(ws):
    _, db = do_sync(ws)
    cfg = config.load_config()
    c = ctxmod.current(db, str(ws / "site"))
    hits = S.search(db, cfg, None, "invoice total", c, qvec=None, mode="vector")
    db.close()
    assert hits and all(h.route == "fts-fallback" for h in hits)


def test_hybrid_mode_without_vector_is_fts_fallback(ws):
    _, db = do_sync(ws)
    cfg = config.load_config()
    c = ctxmod.current(db, str(ws / "site"))
    hits = S.search(db, cfg, None, "invoice", c, qvec=None, mode="hybrid")
    db.close()
    assert hits and all(h.route == "fts-fallback" for h in hits)
