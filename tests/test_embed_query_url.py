"""`query_url`: search queries go to a dedicated embedding server, docs/memory stay on `ollama_url` (E8-09, ADR-039)."""
import httpx
import pytest

from myvec import embed
from myvec.config import Config, default_config_text, load_config

MAIN, QUERY = "http://127.0.0.1:11435", "http://127.0.0.1:11436"


def _cfg(api, query_url=""):
    return Config(dims=8, embed_api=api, ollama_url=MAIN, query_url=query_url)


def _ok(api, req):
    if api == "openai":
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 8}]}, request=req)
    return httpx.Response(200, json={"embeddings": [[1.0] * 8]}, request=req)


@pytest.fixture(params=["openai", "ollama"])
def api(request, monkeypatch):
    monkeypatch.delenv("MYVEC_EMBEDDER", raising=False)   # conftest forces the fake embedder
    return request.param


def _fake(api, dead=(), calls=None):
    def post(url, json=None, timeout=None):
        calls.append((url.rsplit(":", 1)[1].split("/")[0], timeout))
        req = httpx.Request("POST", url)
        if any(d in url for d in dead):
            raise httpx.ConnectError("down", request=req)
        return _ok(api, req)
    return post


def test_query_goes_to_query_url_docs_to_main(api, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", _fake(api, calls=calls))
    e = embed.get_embedder(_cfg(api, QUERY))
    e.embed_query("q")
    e.embed_docs(["d"])
    assert [c[0] for c in calls] == ["11436", "11435"]
    assert calls[0][1] == embed.QUERY_URL_TIMEOUT


def test_no_query_url_everything_on_main(api, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", _fake(api, calls=calls))
    e = embed.get_embedder(_cfg(api))
    e.embed_query("q")
    e.embed_docs(["d"])
    assert calls == [("11435", embed.QUERY_TIMEOUT), ("11435", embed.BATCH_TIMEOUT)]


def test_query_url_down_falls_back_to_main(api, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", _fake(api, dead=("11436",), calls=calls))
    v = embed.get_embedder(_cfg(api, QUERY)).embed_query("q")
    assert len(v) == 8
    assert calls == [("11436", embed.QUERY_URL_TIMEOUT), ("11435", embed.QUERY_TIMEOUT)]


def test_both_down_embed_error_and_fts_fallback(api, monkeypatch):
    from myvec import search

    monkeypatch.setattr(httpx, "post", _fake(api, dead=("11436", "11435"), calls=[]))
    e = embed.get_embedder(_cfg(api, QUERY))
    with pytest.raises(embed.EmbedError):
        e.embed_query("q")
    assert search.embed_query(e, "q", warn=lambda *_: None) is None


def test_config_round_trip():
    assert Config().query_url == ""
    assert 'query_url = ""' in default_config_text()
    from myvec.config import home
    (home() / "config.toml").write_text(f'query_url = "{QUERY}"\n', encoding="utf-8")
    assert load_config().query_url == QUERY


# --- extra coverage (tester) ---

@pytest.mark.parametrize("qurl", [MAIN, MAIN + "/"])
def test_query_url_equal_to_main_single_request(api, monkeypatch, qurl):
    calls = []
    monkeypatch.setattr(httpx, "post", _fake(api, dead=("11435",), calls=calls))
    with pytest.raises(embed.EmbedError):
        embed.get_embedder(_cfg(api, qurl)).embed_query("q")
    assert calls == [("11435", embed.QUERY_TIMEOUT)]   # no double request


def test_main_url_trailing_slash_equal_to_query_url(api, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", _fake(api, calls=calls))
    cfg = Config(dims=8, embed_api=api, ollama_url=MAIN + "/", query_url=MAIN)
    embed.get_embedder(cfg).embed_query("q")
    assert calls == [("11435", embed.QUERY_TIMEOUT)]


def test_query_timeout_constants():
    assert embed.QUERY_TIMEOUT == 10.0 and embed.QUERY_URL_TIMEOUT == 2.0


def test_query_url_timeout_falls_back_once(api, monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url.rsplit(":", 1)[1].split("/")[0], timeout))
        req = httpx.Request("POST", url)
        if "11436" in url:
            raise httpx.ReadTimeout("slow", request=req)
        return _ok(api, req)
    monkeypatch.setattr(httpx, "post", post)
    assert len(embed.get_embedder(_cfg(api, QUERY)).embed_query("q")) == 8
    assert calls == [("11436", embed.QUERY_URL_TIMEOUT), ("11435", embed.QUERY_TIMEOUT)]


def test_query_url_non_200_falls_back_once(api, monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url.rsplit(":", 1)[1].split("/")[0], timeout))
        req = httpx.Request("POST", url)
        if "11436" in url:
            return httpx.Response(503, text="busy", request=req)
        return _ok(api, req)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404, request=httpx.Request("GET", a[0])))
    assert len(embed.get_embedder(_cfg(api, QUERY)).embed_query("q")) == 8
    assert calls == [("11436", embed.QUERY_URL_TIMEOUT), ("11435", embed.QUERY_TIMEOUT)]


def test_both_fail_non_200_search_route_fts_fallback(api, monkeypatch):
    from myvec import search

    def post(url, json=None, timeout=None):
        return httpx.Response(500, text="x", request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404, request=httpx.Request("GET", a[0])))
    e = embed.get_embedder(_cfg(api, QUERY))
    qvec = search.embed_query(e, "q", warn=lambda *_: None)
    assert qvec is None and search.effective_route("q", "auto", qvec) == "fts-fallback"


def test_embed_docs_never_touches_query_url(api, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", _fake(api, dead=("11436",), calls=calls))
    e = embed.get_embedder(_cfg(api, QUERY))
    e.embed_docs(["a"])
    assert {c[0] for c in calls} == {"11435"}


def test_same_vectors_via_both_paths(api, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake(api, calls=[]))
    with_q = embed.get_embedder(_cfg(api, QUERY)).embed_query("q")
    without = embed.get_embedder(_cfg(api)).embed_query("q")
    assert with_q == without


def test_openai_over_ctx_uses_same_url_as_embedding(monkeypatch):
    seen = []
    state = {"n": 0}

    def post(url, json=None, timeout=None):
        seen.append(url)
        req = httpx.Request("POST", url)
        if url.endswith("/v1/embeddings"):
            state["n"] += 1
            if state["n"] % 2 == 1:
                return httpx.Response(400, json={"error": {"type": "exceed_context_size_error", "n_ctx": 16}},
                                      request=req)
            return _ok("openai", req)
        if url.endswith("/tokenize"):
            return httpx.Response(200, json={"tokens": list(range(100))}, request=req)
        return httpx.Response(200, json={"content": "short"}, request=req)

    def get(url, timeout=None, **kw):
        seen.append(url)
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16}},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.delenv("MYVEC_EMBEDDER", raising=False)
    embed.get_embedder(_cfg("openai", QUERY)).embed_query("q" * 500)
    assert any(u.endswith("/tokenize") for u in seen) and any(u.endswith("/detokenize") for u in seen)
    assert all(":11436" in u for u in seen)


def test_openai_over_ctx_fallback_goes_to_main(monkeypatch):
    seen = []

    def post(url, json=None, timeout=None):
        seen.append(url)
        req = httpx.Request("POST", url)
        if "11436" in url:
            raise httpx.ConnectError("down", request=req)
        return _ok("openai", req)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.delenv("MYVEC_EMBEDDER", raising=False)
    embed.get_embedder(_cfg("openai", QUERY)).embed_query("q")
    assert [u.split("/v1")[0] for u in seen] == [QUERY, MAIN]


def test_contrib_query_unit_sanity():
    from pathlib import Path
    d = Path(__file__).resolve().parent.parent / "contrib" / "llama-embed"
    s = (d / "embed-query-server").read_text()
    assert "11436" in s and "-np 1" in s and "--embeddings" in s
    assert "11436" in (d / "embed-query-server.service").read_text()
    main = (d / "embed-server").read_text()
    assert "11436" not in main and "11436" not in (d / "embed-server.service").read_text()
