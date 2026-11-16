"""OpenAIEmbedder against a fake llama-server (no network)."""
import httpx
import pytest

from myvec import embed
from myvec.config import Config


class FakeServer:
    """Tokens = characters; per-slot context n_ctx; rejects longer inputs like llama-server does."""

    def __init__(self, n_ctx=50, ub=100):
        self.n_ctx, self.ub = n_ctx, ub
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        path = url.split("127.0.0.1:11435", 1)[1]
        self.calls.append((path, json))
        req = httpx.Request("POST", url)
        if path == "/tokenize":
            return httpx.Response(200, json={"tokens": [ord(c) for c in json["content"]]}, request=req)
        if path == "/detokenize":
            return httpx.Response(200, json={"content": "".join(map(chr, json["tokens"]))}, request=req)
        assert path == "/v1/embeddings"
        longest = max(len(t) + 2 for t in json["input"])      # + BOS/EOS
        if longest > self.ub:                                  # checked before n_ctx, carries no n_ctx
            return httpx.Response(500, json={"error": {"code": 500, "type": "server_error", "message":
                                  f"input ({longest} tokens) is too large to process. increase the physical batch size"}},
                                  request=req)
        if longest > self.n_ctx:
            return httpx.Response(400, json={"error": {"code": 400, "type": "exceed_context_size_error",
                                                       "n_prompt_tokens": longest, "n_ctx": self.n_ctx}}, request=req)
        data = [{"index": i, "embedding": [float(len(t))] + [1.0] * 7} for i, t in enumerate(json["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))}, request=req)


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(httpx, "post", s)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: httpx.Response(
        200, json={"default_generation_settings": {"n_ctx": s.n_ctx}}, request=httpx.Request("GET", url)))
    return s


def emb():
    return embed.OpenAIEmbedder(Config(dims=8, embed_api="openai", ollama_url="http://127.0.0.1:11435",
                                       model="embeddinggemma", batch_size=16))


def test_short_inputs_single_call_and_order_by_index(server):
    e = emb()
    vecs = e._raw(["a", "bbb"])
    assert [v[0] for v in vecs] == [1.0, 3.0]
    assert [c[0] for c in server.calls] == ["/v1/embeddings"]


def test_over_context_inputs_are_truncated_head_kept_and_retried(server):
    e = emb()
    long = "x" * 30 + "y" * 100
    vecs = e._raw(["short", long])
    limit = server.n_ctx - e.CTX_MARGIN
    assert [v[0] for v in vecs] == [5.0, float(limit)]
    final = server.calls[-1][1]["input"]
    assert final[0] == "short" and final[1] == long[:limit]
    assert [c[0] for c in server.calls].count("/v1/embeddings") == 2


def test_input_over_physical_batch_uses_props_n_ctx(server):
    e = emb()
    vecs = e._raw(["z" * 500])
    assert vecs[0][0] == float(server.n_ctx - e.CTX_MARGIN)


def test_other_errors_are_raised(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None:
                        httpx.Response(500, text="boom", request=httpx.Request("POST", url)))
    with pytest.raises(embed.EmbedError, match="500"):
        emb()._raw(["a"])


def test_unreachable_server(monkeypatch):
    def down(url, json=None, timeout=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(embed.EmbedError, match="not reachable"):
        emb()._raw(["a"])
