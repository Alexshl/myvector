"""Retrieval-quality + latency evaluation. Needs emb/*_docs.npy, *_queries.npy from embed.py.
Usage: uv run python evalbench.py [--no-lat]
Outputs: results/metrics.json, results/per_query.csv, results/latency.json, results/tokens.json"""
import csv, json, os, re, shutil, sqlite3, statistics, subprocess, sys, time
from collections import defaultdict
import numpy as np, sqlite_vec
from common import *

TMP = os.path.join(BENCH, "tmp"); os.makedirs(TMP, exist_ok=True)
K = 50; RRF_K = 60
VARIANTS = {  # name -> (model key, dims)
    "qwen3-1024": ("qwen3", 1024), "qwen3-512": ("qwen3", 512),
    "bge-m3-1024": ("bge-m3", 1024),
    "gemma-768": ("gemma", 768), "gemma-512": ("gemma", 512),
}
REPO_PATHS = {"project-b/site": "project-b/site", "project-b/maximus-libs": "project-b/maximus-libs", "project-b/ocr": "project-b/ocr",
              "project-d.se/public_html": "project-d.se/public_html", "project-g": "project-g"}
HOME = os.path.expanduser("~/projects")

def words(q): return re.findall(r"\w+", q.lower())
def fts_query(q, typ, kind):
    ws = words(q)
    if kind == "unicode61":
        if typ == "identifier": return '"' + " ".join(ws) + '"'          # phrase of identifier parts
        ws = [w for w in ws if len(w) >= 3] or ws
        return " OR ".join(f'"{w}"' for w in dict.fromkeys(ws))
    # trigram: substring match, tokens < 3 chars can never match
    if typ == "identifier": return '"' + q.replace('"', '') + '"'
    ws = [w for w in ws if len(w) >= 3]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(ws)) or '"zzzz"'

def connect(path):
    db = sqlite3.connect(path); db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False); return db

def build_fts(chunks):
    path = os.path.join(TMP, "fts.db")
    if os.path.exists(path) and os.path.getsize(path) > 1e6:
        return path
    for s in ("", "-wal", "-shm"):
        if os.path.exists(path + s): os.remove(path + s)
    db = sqlite3.connect(path)
    db.execute("create virtual table fts_u using fts5(text, tokenize='unicode61 remove_diacritics 2')")
    db.execute("create virtual table fts_t using fts5(text, tokenize='trigram')")
    t = time.time()
    db.executemany("insert into fts_u(rowid,text) values(?,?)", ((c["id"], c["text"]) for c in chunks))
    tu = time.time() - t; t = time.time()
    db.executemany("insert into fts_t(rowid,text) values(?,?)", ((c["id"], c["text"]) for c in chunks))
    tt = time.time() - t
    db.commit(); db.execute("insert into fts_u(fts_u) values('optimize')"); db.execute("insert into fts_t(fts_t) values('optimize')"); db.commit(); db.close()
    json.dump({"fts_unicode61_build_s": tu, "fts_trigram_build_s": tt}, open(os.path.join(RES, "fts_build.json"), "w"))
    return path

def build_vec(name, D):
    path = os.path.join(TMP, f"vec_{name}.db")
    if os.path.exists(path) and os.path.getsize(path) > 1e6: return path
    if os.path.exists(path): os.remove(path)
    db = connect(path)
    db.execute(f"create virtual table v using vec0(embedding float[{D.shape[1]}] distance_metric=cosine)")
    t = time.time()
    db.executemany("insert into v(rowid, embedding) values(?,?)", ((i, D[i].tobytes()) for i in range(len(D))))
    db.commit(); db.close()
    return path

def rrf(*lists):
    sc = defaultdict(float)
    for lst in lists:
        for r, cid in enumerate(lst): sc[cid] += 1.0 / (RRF_K + r + 1)
    return [c for c, _ in sorted(sc.items(), key=lambda x: -x[1])]

def to_files(cids, chunks):
    seen, out = set(), []
    for c in cids:
        f = chunks[c]["file"]
        if f not in seen: seen.add(f); out.append(f)
    return out

def metrics(files, expected):
    exp = set(expected)
    def rec(k): return len(exp & set(files[:k])) / len(exp)
    rr = 0.0
    for i, f in enumerate(files):
        if f in exp: rr = 1.0 / (i + 1); break
    return rec(5), rec(10), rr

# ---------- ripgrep baseline
def rg_cmd():
    p = shutil.which("rg")
    if p:
        try:
            if subprocess.run([p, "--version"], capture_output=True).returncode == 0: return [p], {}
        except Exception: pass
    claude = os.path.expanduser("~/.local/bin/claude")   # Claude Code embeds ripgrep (ARGV0=rg)
    return [claude], {"ARGV0": "rg"}

def rg_rank(q, corpus_files):
    ws = [w for w in words(q) if len(w) > 3]
    if not ws: return []
    pat = "|".join(re.escape(w) for w in dict.fromkeys(ws))
    cmd, env = rg_cmd()
    counts = {}
    for repo, rel in REPO_PATHS.items():
        r = subprocess.run(cmd + ["-c", "-i", "--no-messages", "-e", pat, "."], cwd=f"{HOME}/{rel}", capture_output=True, text=True, env={**os.environ, **env})
        for line in r.stdout.splitlines():
            f, _, n = line.rpartition(":")
            f = f[2:] if f.startswith("./") else f
            full = f"{repo}/{f}"
            if full in corpus_files: counts[full] = int(n)
    return [f for f, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]

def pct(xs, p): return float(np.percentile(xs, p))

def main():
    lat = "--no-lat" not in sys.argv
    chunks = load_chunks(); gold = load_golden(); N = len(chunks)
    corpus_files = {c["file"] for c in chunks}
    missing = [(g["q"], e) for g in gold for e in g["expected"] if e not in corpus_files]
    print("expected files missing from corpus:", missing)
    fts_path = build_fts(chunks)
    fdb = sqlite3.connect(fts_path)
    # FTS rankings (chunk ids)
    fts_res = {"fts_unicode61": [], "fts_trigram": []}
    for g in gold:
        for kind, tbl in (("fts_unicode61", "fts_u"), ("fts_trigram", "fts_t")):
            qq = fts_query(g["q"], g["type"], kind.split("_")[1])
            try: ids = [r[0] for r in fdb.execute(f"select rowid from {tbl} where {tbl} match ? order by bm25({tbl}) limit {K}", (qq,))]
            except sqlite3.OperationalError as e: print("FTS error", kind, qq, e); ids = []
            fts_res[kind].append(ids)
    rankings = {}   # (variant, mode) -> list of chunk-id lists per query
    for mode, lists in fts_res.items(): rankings[("-", mode)] = lists
    vec_times = {}; sizes = {}
    for name, (key, d) in VARIANTS.items():
        dp, qp = os.path.join(EMB, f"{key}_docs.npy"), os.path.join(EMB, f"{key}_queries.npy")
        if not (os.path.exists(dp) and os.path.exists(qp)): print("skip (no embeddings)", name); continue
        D = l2(np.load(dp)[:, :d] if d < np.load(dp, mmap_mode="r").shape[1] else np.load(dp)); Q = l2(np.load(qp)[:, :d])
        if len(D) != N: print("doc count mismatch", name); continue
        path = build_vec(name, D); sizes[name] = os.path.getsize(path)
        db = connect(path)
        vr = []
        for qi in range(len(gold)):
            vr.append([r[0] for r in db.execute(f"select rowid from v where embedding match ? and k={K} order by distance", (Q[qi].tobytes(),))])
        rankings[(name, "vector")] = vr
        rankings[(name, "hybrid_u")] = [rrf(vr[i], fts_res["fts_unicode61"][i]) for i in range(len(gold))]
        rankings[(name, "hybrid_t")] = [rrf(vr[i], fts_res["fts_trigram"][i]) for i in range(len(gold))]
        rankings[(name, "hybrid_ut")] = [rrf(vr[i], fts_res["fts_unicode61"][i], fts_res["fts_trigram"][i]) for i in range(len(gold))]
        if lat:   # warm sqlite search latency (query vec already computed), 5 passes
            for mode in ("vector", "hybrid_u", "hybrid_t"):
                ts = []
                for rep in range(5):
                    for qi, g in enumerate(gold):
                        t = time.perf_counter()
                        ids = [r[0] for r in db.execute(f"select rowid from v where embedding match ? and k={K} order by distance", (Q[qi].tobytes(),))]
                        if mode != "vector":
                            kind = "unicode61" if mode == "hybrid_u" else "trigram"; tbl = "fts_u" if kind == "unicode61" else "fts_t"
                            f = [r[0] for r in fdb.execute(f"select rowid from {tbl} where {tbl} match ? order by bm25({tbl}) limit {K}", (fts_query(g["q"], g["type"], kind),))]
                            rrf(ids, f)
                        ts.append((time.perf_counter() - t) * 1000)
                vec_times[f"{name}/{mode}"] = {"p50_ms": pct(ts, 50), "p95_ms": pct(ts, 95), "n": len(ts)}
        db.close()
    # FTS-only latency
    if lat:
        for kind, tbl in (("unicode61", "fts_u"), ("trigram", "fts_t")):
            ts = []
            for rep in range(5):
                for g in gold:
                    t = time.perf_counter()
                    fdb.execute(f"select rowid from {tbl} where {tbl} match ? order by bm25({tbl}) limit {K}", (fts_query(g["q"], g["type"], kind),)).fetchall()
                    ts.append((time.perf_counter() - t) * 1000)
            vec_times[f"fts_{kind}"] = {"p50_ms": pct(ts, 50), "p95_ms": pct(ts, 95), "n": len(ts)}
    # ripgrep baseline
    rg_files, rg_times = [], []
    for g in gold:
        t = time.perf_counter(); rg_files.append(rg_rank(g["q"], corpus_files)); rg_times.append((time.perf_counter() - t) * 1000)
    # ---------- metrics
    per_query = []; agg = defaultdict(lambda: defaultdict(list))
    def add(variant, mode, qi, files):
        g = gold[qi]; r5, r10, rr = metrics(files, g["expected"])
        first = next((i + 1 for i, f in enumerate(files) if f in set(g["expected"])), 0)
        per_query.append([variant, mode, qi, g["lang"], g["type"], r5, r10, rr, first, g["q"][:60]])
        for grp in ("all", g["type"], f"{g['type']}:{g['lang']}"):
            agg[(variant, mode, grp)]["r5"].append(r5); agg[(variant, mode, grp)]["r10"].append(r10); agg[(variant, mode, grp)]["mrr"].append(rr)
    for (variant, mode), lists in rankings.items():
        for qi, ids in enumerate(lists): add(variant, mode, qi, to_files(ids, chunks))
    for qi, files in enumerate(rg_files): add("-", "ripgrep", qi, files)
    out = {}
    for (variant, mode, grp), m in agg.items():
        out.setdefault(f"{variant}|{mode}", {})[grp] = {"recall@5": float(np.mean(m["r5"])), "recall@10": float(np.mean(m["r10"])), "mrr": float(np.mean(m["mrr"])), "n": len(m["r5"])}
    json.dump(out, open(os.path.join(RES, "metrics.json"), "w"), indent=1)
    with open(os.path.join(RES, "per_query.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["variant", "mode", "qidx", "lang", "type", "recall@5", "recall@10", "rr", "first_hit_rank", "query"]); w.writerows(per_query)
    # ---------- tokens per answer: top-5 chunks (rank order, may share a file) whole vs path+first 3 lines
    tok = {}
    for (variant, mode) in [(v, m) for (v, m) in rankings if m in ("vector", "hybrid_u", "hybrid_ut")]:
        whole, brief = [], []
        for ids in rankings[(variant, mode)]:
            top = ids[:5]
            whole.append(sum(len(chunks[c]["text"]) for c in top) / 4)
            brief.append(sum(len(chunks[c]["file"]) + 12 + len("\n".join(chunks[c]["text"].split("\n")[1:4])) for c in top) / 4)
        tok[f"{variant}|{mode}"] = {"whole_chunks_tokens": float(np.mean(whole)), "path_plus_3_lines_tokens": float(np.mean(brief))}
    tok["_corpus"] = {"chunks": N, "avg_chunk_chars": float(np.mean([len(c["text"]) for c in chunks])), "median_chunk_chars": float(np.median([len(c["text"]) for c in chunks]))}
    json.dump(tok, open(os.path.join(RES, "tokens.json"), "w"), indent=1)
    vec_times["ripgrep_all_5_repos"] = {"p50_ms": pct(rg_times, 50), "p95_ms": pct(rg_times, 95), "n": len(rg_times)}
    json.dump({"sqlite_search": vec_times, "db_sizes_bytes": {**sizes, "fts.db(unicode61+trigram)": os.path.getsize(fts_path)}}, open(os.path.join(RES, "latency_sqlite.json"), "w"), indent=1)
    # console summary
    print(f"{'variant|mode':32s} {'R@5':>6} {'R@10':>6} {'MRR':>6}   sem R@10 / MRR   ident R@10 / MRR")
    for k, v in sorted(out.items()):
        a, s, i = v["all"], v.get("semantic", {}), v.get("identifier", {})
        print(f"{k:32s} {a['recall@5']:6.3f} {a['recall@10']:6.3f} {a['mrr']:6.3f}   {s.get('recall@10', 0):.3f} / {s.get('mrr', 0):.3f}      {i.get('recall@10', 0):.3f} / {i.get('mrr', 0):.3f}")

if __name__ == "__main__": main()
