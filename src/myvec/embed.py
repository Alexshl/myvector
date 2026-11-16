"""Embedders: Ollama, OpenAI-compatible (llama.cpp llama-server) and a deterministic fake (tests)."""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Callable

from myvec.config import Config

# counters for tests / status: number of texts embedded and number of calls
STATS = {"texts": 0, "calls": 0}


QUERY_TIMEOUT = 10.0    # a search must not hang on a stuck Ollama
QUERY_URL_TIMEOUT = 2.0  # dedicated query server (cfg.query_url): short, a dead one must not delay the fallback
BATCH_TIMEOUT = 300.0   # indexing big batches (first load of the model can be slow)


class EmbedError(RuntimeError):
    pass


def truncate_norm(vec: list[float], dims: int) -> list[float]:
    """MRL truncation to `dims` + L2 renormalisation."""
    if len(vec) < dims:
        raise EmbedError(f"model returned {len(vec)} dims, config wants {dims}")
    v = vec[:dims]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.query_prefix, self.doc_prefix = cfg.prefix_pair()

    def _raw(self, texts: list[str], timeout: float = BATCH_TIMEOUT, url: str | None = None) -> list[list[float]]:
        raise NotImplementedError

    def _embed(self, texts: list[str], timeout: float = BATCH_TIMEOUT, url: str | None = None,
               check: Callable[[], None] | None = None) -> list[list[float]]:
        out: list[list[float]] = []
        bs = max(1, self.cfg.batch_size)
        for i in range(0, len(texts), bs):
            if check is not None:
                check()  # raises to cancel between sub-batches; nothing is written by the caller then
            batch = texts[i : i + bs]
            STATS["calls"] += 1
            STATS["texts"] += len(batch)
            out.extend(truncate_norm(v, self.cfg.dims) for v in self._raw(batch, timeout, url))
        return out

    def embed_docs(self, texts: list[str], check: Callable[[], None] | None = None) -> list[list[float]]:
        if not texts:
            return []
        return self._embed([self.doc_prefix + t for t in texts], check=check)

    def embed_query(self, text: str) -> list[float]:
        """Query embedding. With `query_url` set (a separate server of the SAME model file, so vectors match
        the indexed ones) it goes there first, so a search never waits behind indexing batches; if that server
        is unreachable/fails, one attempt on the main url follows. Without `query_url` nothing changes."""
        texts = [self.query_prefix + text]
        qurl = self.cfg.query_url
        if qurl and qurl.rstrip("/") != self.cfg.ollama_url.rstrip("/"):
            try:
                return self._embed(texts, QUERY_URL_TIMEOUT, qurl)[0]
            except EmbedError:
                pass
        return self._embed(texts, QUERY_TIMEOUT)[0]


class OllamaEmbedder(Embedder):
    def _raw(self, texts: list[str], timeout: float = BATCH_TIMEOUT, url: str | None = None) -> list[list[float]]:
        import httpx

        base = url or self.cfg.ollama_url
        url = base.rstrip("/") + "/api/embed"
        try:
            body = {"model": self.cfg.model, "input": texts}
            if self.cfg.keep_alive:
                body["keep_alive"] = self.cfg.keep_alive
            r = httpx.post(url, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise EmbedError(f"Ollama not reachable at {base}: {e}") from e
        if r.status_code != 200:
            raise EmbedError(f"Ollama /api/embed returned {r.status_code}: {r.text[:200]}")
        embs = r.json().get("embeddings")
        if not embs or len(embs) != len(texts):
            raise EmbedError("Ollama returned an unexpected number of embeddings")
        return embs


class OpenAIEmbedder(Embedder):
    """OpenAI-compatible /v1/embeddings (llama.cpp llama-server --embeddings, vLLM...).

    Unlike Ollama, llama-server rejects inputs longer than its per-slot context instead of truncating them.
    On that error the over-long texts are cut to the context via the server's /tokenize + /detokenize
    (head kept, like Ollama's truncation) and the batch is retried once."""

    CTX_MARGIN = 4   # room for BOS/EOS the server adds around the content

    def _post(self, path: str, body: dict, timeout: float, url: str | None = None):
        import httpx

        base = url or self.cfg.ollama_url
        try:
            return httpx.post(base.rstrip("/") + path, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise EmbedError(f"embedding server not reachable at {base}: {e}") from e

    def _over_ctx(self, r, timeout: float, url: str | None = None) -> int | None:
        """Per-slot n_ctx if `r` is llama-server rejecting an over-long input, else None.

        Over n_ctx it answers 400 exceed_context_size_error (carries n_ctx); an input longer than the
        physical batch (-ub) is caught first and comes back as 500 'too large to process' (no n_ctx: ask /props)."""
        if r.status_code not in (400, 500):
            return None
        try:
            err = r.json().get("error") or {}
        except Exception:  # noqa: BLE001 - non-JSON / odd error bodies are just "not an overflow"
            return None
        if err.get("type") == "exceed_context_size_error" and err.get("n_ctx"):
            return int(err["n_ctx"])
        if "too large to process" not in str(err.get("message", "")):
            return None
        import httpx

        try:
            p = httpx.get((url or self.cfg.ollama_url).rstrip("/") + "/props", timeout=timeout)
            n = (p.json().get("default_generation_settings") or {}).get("n_ctx") if p.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            n = None
        return int(n) if n else None

    def _fit(self, texts: list[str], n_ctx: int, timeout: float, url: str | None = None) -> list[str]:
        limit = max(1, n_ctx - self.CTX_MARGIN)
        out = []
        for t in texts:
            r = self._post("/tokenize", {"content": t, "add_special": False}, timeout, url)
            if r.status_code != 200:
                raise EmbedError(f"/tokenize returned {r.status_code}: {r.text[:200]}")
            toks = r.json().get("tokens") or []
            if len(toks) <= limit:
                out.append(t)
                continue
            r = self._post("/detokenize", {"tokens": toks[:limit]}, timeout, url)
            if r.status_code != 200:
                raise EmbedError(f"/detokenize returned {r.status_code}: {r.text[:200]}")
            out.append(r.json().get("content", ""))
        return out

    def _raw(self, texts: list[str], timeout: float = BATCH_TIMEOUT, url: str | None = None) -> list[list[float]]:
        body = {"model": self.cfg.model, "input": texts}
        r = self._post("/v1/embeddings", body, timeout, url)
        n_ctx = self._over_ctx(r, timeout, url)
        if n_ctx:
            body["input"] = self._fit(texts, n_ctx, timeout, url)
            r = self._post("/v1/embeddings", body, timeout, url)
        if r.status_code != 200:
            raise EmbedError(f"/v1/embeddings returned {r.status_code}: {r.text[:200]}")
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(texts):
            raise EmbedError("embedding server returned an unexpected number of embeddings")
        return [d["embedding"] for d in data]


_TOKEN = re.compile(r"[\w]+", re.UNICODE)


class FakeEmbedder(Embedder):
    """Feature-hashing bag-of-words: deterministic, similar texts get similar vectors."""

    def _raw(self, texts: list[str], timeout: float = BATCH_TIMEOUT, url: str | None = None) -> list[list[float]]:
        out = []
        dims = self.cfg.dims
        for t in texts:
            v = [0.0] * dims
            for tok in _TOKEN.findall(t.lower()):
                h = hashlib.md5(tok.encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "little") % dims
                v[idx] += 1.0 if h[4] & 1 else -1.0
            if not any(v):
                v[0] = 1.0
            out.append(v)
        return out


def get_embedder(cfg: Config) -> Embedder:
    if os.environ.get("MYVEC_EMBEDDER") == "fake":
        return FakeEmbedder(cfg)
    if cfg.embed_api == "openai":
        return OpenAIEmbedder(cfg)
    return OllamaEmbedder(cfg)
