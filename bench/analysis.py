"""Derived analysis from results/per_query.csv: 'adaptive' router (query without whitespace -> hybrid_u, else vector) and bootstrap 95% CIs. -> results/analysis.json"""
import csv, json, os
import numpy as np
from common import RES
rows = list(csv.DictReader(open(os.path.join(RES, "per_query.csv"))))
Q = {}
for r in rows: Q.setdefault((r["variant"], r["mode"]), {})[int(r["qidx"])] = (float(r["recall@5"]), float(r["recall@10"]), float(r["rr"]), r["type"])
def series(v, m): return np.array([Q[(v, m)][i][:3] for i in range(45)])
typ = [Q[("gemma-768", "vector")][i][3] for i in range(45)]
rng = np.random.default_rng(0)
def ci(x):  # x: 45 values
    b = [x[rng.integers(0, 45, 45)].mean() for _ in range(5000)]; return [float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))]
out = {}
for v in ("gemma-768", "gemma-512", "bge-m3-1024", "qwen3-1024", "qwen3-512"):
    vec, hyb = series(v, "vector"), series(v, "hybrid_u")
    ad = np.array([hyb[i] if typ[i] == "identifier" else vec[i] for i in range(45)])
    for name, s in (("vector", vec), ("hybrid_u", hyb), ("adaptive", ad)):
        out[f"{v}|{name}"] = {"recall@5": float(s[:, 0].mean()), "recall@10": float(s[:, 1].mean()), "mrr": float(s[:, 2].mean()), "recall@10_ci95": ci(s[:, 1]), "mrr_ci95": ci(s[:, 2])}
g, b = series("gemma-768", "vector")[:, 1], series("bge-m3-1024", "vector")[:, 1]
d = g - b; bs = [d[rng.integers(0, 45, 45)].mean() for _ in range(5000)]
out["paired_gemma768_minus_bge-m3_vector_recall@10"] = {"mean": float(d.mean()), "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}
json.dump(out, open(os.path.join(RES, "analysis.json"), "w"), indent=1)
for k, v in out.items(): print(k, v)
