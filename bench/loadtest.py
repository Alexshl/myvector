"""sqlite-vec synthetic load test. Usage: uv run python loadtest.py [--sizes 50000x512,200000x512,1000000x512,200000x1024] [--keep]
Variants: f32 full scan | f32 partition key (100 partitions, 1 queried) | int8 | bit coarse top-500 + f32 rescoring.
DB files live in bench/tmp/lt_*.db and are deleted after each size. Output: results/loadtest.json"""
import json, os, sqlite3, sys, time, gc
import numpy as np, sqlite_vec
from common import *
TMP = os.path.join(BENCH, "tmp"); os.makedirs(TMP, exist_ok=True)
K = 50; COARSE = 500; NQ = 20; PARTS = 100

def connect(path):
    db = sqlite3.connect(path); db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False)
    db.execute("pragma journal_mode=off"); db.execute("pragma synchronous=off"); return db

def pct(xs, p): return float(np.percentile(xs, p))
def timeit(fn, qs):
    for q in qs[:2]: fn(q)          # warm
    ts = []
    for q in qs: t = time.perf_counter(); fn(q); ts.append((time.perf_counter() - t) * 1000)
    return {"p50_ms": pct(ts, 50), "p95_ms": pct(ts, 95), "n": len(ts)}

def gen(rng, n, d):
    v = rng.standard_normal((n, d), dtype=np.float32); v /= np.linalg.norm(v, axis=1, keepdims=True); return v

def fill(db, sql, n, d, rng, batch=20000, extra=None):
    """streams random vectors into `sql` (params: rowid, blob[, extra]); returns the first few vectors for queries & ground truth."""
    keep = None; i = 0
    while i < n:
        m = min(batch, n - i); v = gen(rng, m, d)
        if keep is None: keep = v[:2000].copy()
        db.executemany(sql, ((i + j + 1, v[j].tobytes()) + ((extra(i + j),) if extra else ()) for j in range(m)))
        i += m
    db.commit(); return keep

def run_size(n, d, results):
    print(f"=== {n} x {d}", flush=True)
    r = {"rows": n, "dims": d}
    qrng = np.random.default_rng(123); qs = [gen(qrng, 1, d)[0].tobytes() for _ in range(NQ)]
    paths = {k: os.path.join(TMP, f"lt_{k}_{n}_{d}.db") for k in ("f32", "part", "i8", "bit")}
    for p in paths.values():
        if os.path.exists(p): os.remove(p)
    # --- f32 full scan
    t = time.time(); db = connect(paths["f32"])
    db.execute(f"create virtual table v using vec0(embedding float[{d}] distance_metric=cosine)")
    fill(db, "insert into v(rowid, embedding) values (?,?)", n, d, np.random.default_rng(1))
    r["f32_build_s"] = time.time() - t; db.close(); db = connect(paths["f32"])
    exact = {}
    def q_f32(q):
        rows = db.execute(f"select rowid from v where embedding match ? and k={K}", (q,)).fetchall(); exact[q] = [x[0] for x in rows]; return rows
    r["f32_full_scan"] = timeit(q_f32, qs); r["f32_size_mb"] = os.path.getsize(paths["f32"]) / 1e6
    print(" f32", r["f32_full_scan"], flush=True); db.close()
    # --- f32 partition key
    t = time.time(); db = connect(paths["part"])
    db.execute(f"create virtual table v using vec0(part integer partition key, embedding float[{d}] distance_metric=cosine)")
    fill(db, "insert into v(rowid, embedding, part) values (?,?,?)", n, d, np.random.default_rng(1), extra=lambda i: i % PARTS)
    r["part_build_s"] = time.time() - t; db.close(); db = connect(paths["part"])
    cnt = [0]
    def q_part(q):
        cnt[0] += 1
        return db.execute(f"select rowid from v where embedding match ? and k={K} and part = ?", (q, cnt[0] % PARTS)).fetchall()
    r["f32_partition_1of100"] = timeit(q_part, qs); r["part_size_mb"] = os.path.getsize(paths["part"]) / 1e6
    print(" part", r["f32_partition_1of100"], flush=True); db.close(); os.remove(paths["part"])
    # --- int8
    t = time.time(); db = connect(paths["i8"])
    cos = True
    try: db.execute(f"create virtual table v using vec0(embedding int8[{d}] distance_metric=cosine)")
    except sqlite3.Error as e:
        print(" int8 cosine unsupported:", e); cos = False; db.execute(f"create virtual table v using vec0(embedding int8[{d}])")
    fill(db, "insert into v(rowid, embedding) values (?, vec_quantize_int8(?, 'unit'))", n, d, np.random.default_rng(1))
    r["i8_build_s"] = time.time() - t; r["i8_cosine_metric"] = cos; db.close(); db = connect(paths["i8"])
    res8 = {}
    def q_i8(q):
        rows = db.execute(f"select rowid from v where embedding match vec_quantize_int8(?, 'unit') and k={K}", (q,)).fetchall(); res8[q] = [x[0] for x in rows]; return rows
    r["int8"] = timeit(q_i8, qs); r["i8_size_mb"] = os.path.getsize(paths["i8"]) / 1e6
    print(" int8", r["int8"], flush=True); db.close(); os.remove(paths["i8"])
    # --- bit coarse + f32 rescoring (float vectors stored in a plain blob table next to the bit index)
    t = time.time(); db = connect(paths["bit"])
    db.execute(f"create virtual table b using vec0(embedding bit[{d}])")
    db.execute("create table f(id integer primary key, v blob)")
    rng = np.random.default_rng(1); i = 0
    while i < n:
        m = min(20000, n - i); v = gen(rng, m, d)
        rows = [(i + j + 1, v[j].tobytes()) for j in range(m)]
        db.executemany("insert into b(rowid, embedding) values (?, vec_quantize_binary(?))", rows)
        db.executemany("insert into f(id, v) values (?,?)", rows); i += m
    db.commit(); r["bit_build_s"] = time.time() - t; db.close(); db = connect(paths["bit"])
    resb = {}
    def q_bit(q):
        ids = [x[0] for x in db.execute(f"select rowid from b where embedding match vec_quantize_binary(?) and k={COARSE}", (q,))]
        rows = db.execute(f"select id, vec_distance_cosine(v, ?) d from f where id in ({','.join('?' * len(ids))}) order by d limit {K}", (q, *ids)).fetchall()
        resb[q] = [x[0] for x in rows]; return rows
    r["bit_coarse500_rescore_f32"] = timeit(q_bit, qs)
    # coarse-only timing and DB split
    def q_coarse(q): return db.execute(f"select rowid from b where embedding match vec_quantize_binary(?) and k={COARSE}", (q,)).fetchall()
    r["bit_coarse_only"] = timeit(q_coarse, qs)
    r["bit_db_size_mb_incl_f32_blobs"] = os.path.getsize(paths["bit"]) / 1e6
    print(" bit+rescore", r["bit_coarse500_rescore_f32"], "coarse only", r["bit_coarse_only"], flush=True)
    # recall vs exact (random data: pessimistic, near-orthogonal vectors; real-data recall is in real_quant)
    ov = lambda a, b, k: np.mean([len(set(a[q][:k]) & set(b[q][:k])) / k for q in qs if q in a and q in b])
    r["random_data_overlap_with_exact"] = {"int8@10": float(ov(exact, res8, 10)), "int8@50": float(ov(exact, res8, 50)), "bit_rescore@10": float(ov(exact, resb, 10)), "bit_rescore@50": float(ov(exact, resb, 50))}
    db.close()
    for k in ("f32", "bit"):
        if "--keep" not in sys.argv and os.path.exists(paths[k]): os.remove(paths[k])
    results.append(r); gc.collect()

def real_quant():
    """Quantization recall on the REAL qwen3 embeddings of the bench corpus (30k chunks) vs the float32 exact top-50."""
    out = {}
    docp = os.path.join(EMB, "qwen3_docs.npy"); qp = os.path.join(EMB, "qwen3_queries.npy")
    if not (os.path.exists(docp) and os.path.exists(qp)): return out
    for d in (512, 1024):
        D = truncate(np.load(docp), d); Q = truncate(np.load(qp), d); n = len(D)
        p = os.path.join(TMP, f"lt_real_{d}.db"); 
        if os.path.exists(p): os.remove(p)
        db = connect(p)
        db.execute(f"create virtual table v using vec0(embedding float[{d}] distance_metric=cosine)")
        db.execute(f"create virtual table i8 using vec0(embedding int8[{d}] distance_metric=cosine)")
        db.execute(f"create virtual table b using vec0(embedding bit[{d}])")
        for i in range(n):
            e = D[i].tobytes()
            db.execute("insert into v(rowid, embedding) values (?,?)", (i, e))
            db.execute("insert into i8(rowid, embedding) values (?, vec_quantize_int8(?, 'unit'))", (i, e))
            db.execute("insert into b(rowid, embedding) values (?, vec_quantize_binary(?))", (i, e))
        db.commit()
        ov = {"int8@10": [], "int8@50": [], "bit_rescore@10": [], "bit_rescore@50": [], "bit_only@10": []}
        for qi in range(len(Q)):
            qb = Q[qi].tobytes()
            ex = [x[0] for x in db.execute(f"select rowid from v where embedding match ? and k={K}", (qb,))]
            i8 = [x[0] for x in db.execute(f"select rowid from i8 where embedding match vec_quantize_int8(?, 'unit') and k={K}", (qb,))]
            ids = [x[0] for x in db.execute(f"select rowid from b where embedding match vec_quantize_binary(?) and k={COARSE}", (qb,))]
            bo = ids[:10]
            rs = [x[0] for x in db.execute(f"select rowid from v where rowid in ({','.join('?' * len(ids))}) and embedding match ? and k={K}", (*ids, qb))] if False else \
                 [x[0] for x in sorted(((j, np.dot(D[j], Q[qi])) for j in ids), key=lambda t: -t[1])[:K]]
            f = lambda a, b, k: len(set(a[:k]) & set(b[:k])) / k
            ov["int8@10"].append(f(ex, i8, 10)); ov["int8@50"].append(f(ex, i8, 50)); ov["bit_rescore@10"].append(f(ex, rs, 10)); ov["bit_rescore@50"].append(f(ex, rs, 50)); ov["bit_only@10"].append(f(ex, bo, 10))
        out[str(d)] = {k: float(np.mean(v)) for k, v in ov.items()}; out[str(d)]["rows"] = n
        out[str(d)]["sizes_mb"] = {"f32": n * d * 4 / 1e6, "int8": n * d / 1e6, "bit": n * d / 8 / 1e6}
        db.close(); os.remove(p); print("real_quant", d, out[str(d)], flush=True)
    return out

if __name__ == "__main__":
    sizes = "50000x512,200000x512,1000000x512,200000x1024"
    for i, a in enumerate(sys.argv):
        if a == "--sizes": sizes = sys.argv[i + 1]
    results = []
    outp = os.path.join(RES, "loadtest.json")
    for s in sizes.split(","):
        n, d = map(int, s.split("x")); run_size(n, d, results)
        json.dump({"synthetic": results}, open(outp, "w"), indent=1)
    if "--no-real" not in sys.argv:
        json.dump({"synthetic": results, "real_quant_qwen3": real_quant()}, open(outp, "w"), indent=1)
