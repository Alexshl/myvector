import json, os, time, urllib.request
import numpy as np
BENCH = os.path.dirname(os.path.abspath(__file__))
EMB = os.path.join(BENCH, "emb")
RES = os.path.join(BENCH, "results")
OLLAMA = "http://localhost:11434"
MODELS = {  # key -> (ollama name, query prefix, doc prefix)
    "qwen3": ("qwen3-embedding:0.6b", "Instruct: Given a search query, retrieve relevant code or documentation passages\nQuery: ", ""),
    "bge-m3": ("bge-m3", "", ""),
    "gemma": ("embeddinggemma", "task: search result | query: ", "title: none | text: "),
}
def load_chunks():
    return [json.loads(l) for l in open(os.path.join(EMB, "chunks.jsonl"))]
def load_golden():
    return [json.loads(l) for l in open(os.path.join(BENCH, "golden.jsonl")) if l.strip()]
def ollama_embed(model, inputs, timeout=900):
    req = urllib.request.Request(f"{OLLAMA}/api/embed", json.dumps({"model": model, "input": inputs}).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))["embeddings"]
def l2(a):
    a = np.asarray(a, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-12)
def truncate(a, d):
    return l2(a[..., :d])
