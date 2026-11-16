"""Warm query-embedding latency via Ollama /api/embed (single query per request), per model. Output: results/latency_embed.json"""
import json, os, time
import numpy as np
from common import *
gold = load_golden(); out = {}
for key, (name, qpre, _) in MODELS.items():
    ollama_embed(name, [qpre + gold[0]["q"]]); ollama_embed(name, [qpre + gold[1]["q"]])   # load + warm
    ts = []
    for rep in range(3):
        for g in gold:
            t = time.perf_counter(); ollama_embed(name, [qpre + g["q"]]); ts.append((time.perf_counter() - t) * 1000)
    out[key] = {"model": name, "p50_ms": float(np.percentile(ts, 50)), "p95_ms": float(np.percentile(ts, 95)), "n": len(ts)}
    print(key, out[key], flush=True)
json.dump(out, open(os.path.join(RES, "latency_embed.json"), "w"), indent=1)
