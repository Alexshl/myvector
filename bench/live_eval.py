"""Live ranking bench (E8-20): runs the real `myvec.search.search()` over the real index, read-only.

Run from the repo root: `uv run python bench/live_eval.py [--project NAME] [--k 50]`.
Needs: the real index (`mode=ro`, nothing is written to MYVEC_HOME), the embed server for query vectors
(each query is embedded once, cached in `bench/tmp/live_qvec.json`). The daemon is not needed.
Cases: `bench/golden_live.jsonl`. Output: `bench/results/live_metrics.json`, `bench/results/live_per_query.csv`
(paths and ranks only: no chunk or memory text). A project missing from this machine's registry is skipped.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from myvec import config as cfgmod, ctx as ctxmod, embed, search as searchmod  # noqa: E402

is_test_path = searchmod.is_test_path   # one source: the same predicates the ranking uses
is_ticket_path = searchmod.is_ticket_path


def is_docs_dir_doc(path: str) -> bool:
    """Rejected variants A/B: any doc under a `docs/` component (not in the ranking, see REPORT section 9)."""
    parts = path.replace("\\", "/").split("/")
    return searchmod.is_doc_path(path) and any(p.lower() == "docs" for p in parts[:-1])


def open_ro() -> sqlite3.Connection:
    import sqlite_vec

    db = sqlite3.connect(f"file:{cfgmod.db_path()}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def load_qvecs(cfg, queries: list[str], cache: Path) -> dict[str, list[float] | None]:
    store = json.loads(cache.read_text()) if cache.exists() else {}
    tag = f"{cfg.model}/{cfg.dims}"
    emb = embed.get_embedder(cfg)
    dirty = False
    for q in queries:
        key = f"{tag}\x00{q}"
        if key not in store:
            store[key] = emb.embed_query(q)
            dirty = True
    if dirty:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(store))
    return {q: store[f"{tag}\x00{q}"] for q in queries}


# name -> (t, only_nl_without_test_word, only_vector_route); V4 is a quota, handled separately
VARIANTS = {"base": None}
for _t in (0.9, 0.8, 0.7, 0.6):
    VARIANTS[f"V1_t{_t}"] = (_t, False, False)
    VARIANTS[f"V2_t{_t}"] = (_t, True, False)
    VARIANTS[f"V3_t{_t}"] = (_t, True, True)
VARIANTS["V4_q1"] = "quota"
# E8-21: docs/ docs under d. B = always (vector route), Bw = unless the query mentions docs, Qw = quota in top 3
for _d in (0.9, 0.8, 0.7, 0.6):
    VARIANTS[f"B_d{_d}"] = ("docs", _d, False)
    VARIANTS[f"Bw_d{_d}"] = ("docs", _d, True)
for _d in (0.9, 0.8, 0.7, 0.6):
    VARIANTS[f"Cw_d{_d}"] = ("todo", _d, True)   # the implementation: tickets only (docs/todo/), unless the query mentions docs
VARIANTS["Qw"] = "docquota"


def rerank(hits: list[tuple], query: str, variant: str) -> list[tuple]:
    """hits: (path, score, kind, route) in baseline order. Returns the re-scored order (multiplier per hit)."""
    spec = VARIANTS[variant]
    if spec is None:
        return hits
    if spec == "quota":
        return _quota_merge(hits)
    if spec == "docquota":
        return hits if searchmod.mentions_docs(query) else _quota_merge(hits, is_docs_dir_doc, 1)
    if spec[0] in ("docs", "todo"):
        _, d, wordgate = spec
        pred = is_docs_dir_doc if spec[0] == "docs" else is_ticket_path
        if (wordgate and searchmod.mentions_docs(query)) or (hits and hits[0][3] != "vector"):
            return hits
        return sorted(((p, sc * d if kind == "code" and pred(p) else sc, kind, r)
                       for p, sc, kind, r in hits), key=lambda h: -h[1])
    t, need_nl, need_vec = spec
    if need_nl and searchmod.mentions_test(query):
        return hits
    if need_vec and hits and hits[0][3] != "vector":
        return hits
    return sorted(((p, sc * t if kind == "code" and is_test_path(p) else sc, kind, r) for p, sc, kind, r in hits),
                  key=lambda h: -h[1])


def _quota_merge(hits: list[tuple], pred=is_test_path, limit: int = 1) -> list[tuple]:
    """At most `limit` hits matching `pred` (tests by default) in the top 3 (distinct files), the rest shifts down;
    scores unchanged."""
    top, later, files, tests = [], [], set(), 0
    for h in hits:
        if h[2] != "code":
            top.append(h) if len(files) < 3 else later.append(h)
            continue
        if len(files) >= 3 and h[0] not in files:
            later.append(h)
            continue
        if h[0] not in files and pred(h[0]):
            if tests >= limit:
                later.append(h)
                continue
            tests += 1
        files.add(h[0])
        top.append(h)
    return top + later


def files_of(hits: list[tuple]) -> tuple[list[str], int]:
    """Distinct files in rank order (memory hits are only counted, never named)."""
    seen, out, mem = set(), [], 0
    for path, _score, kind, _route in hits:
        if kind != "code":
            mem += 1
        elif path not in seen:
            seen.add(path)
            out.append(path)
    return out, mem


def is_expected(expected: list[str], path: str) -> bool:
    """An `expected` entry ending in `/` matches any file under that directory."""
    return any(path.startswith(e) if e.endswith("/") else path == e for e in expected)


def score_case(case: dict, files: list[str]) -> dict:
    exp = case["expected"]
    first = next((i for i, p in enumerate(files, 1) if is_expected(exp, p)), None)
    top5 = files[:5]
    docs_before = files[:first - 1][:5] if first is not None and first <= 5 else top5
    if first is not None and first <= 5:
        tests_before = any(is_test_path(p) for p in files[:first - 1][:5])
    else:
        tests_before = any(is_test_path(p) for p in top5)
    top3 = files[:3]
    return {"rank": first, "r5": int(first is not None and first <= 5), "r10": int(first is not None and first <= 10),
            "rr": 1.0 / first if first else 0.0, "test_above_code": int(tests_before),
            "tests_at3": (sum(is_test_path(p) for p in top3) / len(top3)) if top3 else 0.0,
            "docs_above_code": int(any(is_docs_dir_doc(p) for p in docs_before)),
            "docs_at3": (sum(is_docs_dir_doc(p) for p in top3) / len(top3)) if top3 else 0.0}


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    return {"n": n, "R@5": sum(r["r5"] for r in rows) / n, "R@10": sum(r["r10"] for r in rows) / n,
            "MRR": sum(r["rr"] for r in rows) / n, "test_above_code": sum(r["test_above_code"] for r in rows) / n,
            "tests@3": sum(r["tests_at3"] for r in rows) / n,
            "docs_above_code": sum(r["docs_above_code"] for r in rows) / n,
            "docs@3": sum(r["docs_at3"] for r in rows) / n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="only cases of this project")
    ap.add_argument("--test-path-weight", type=float, default=1.0,
                    help="cfg.test_path_weight for the raw search() (default 1 = off, so variants re-score the old "
                         "ranking; pass the config value to check the implementation: variant `base` then equals it)")
    ap.add_argument("--doc-path-weight", type=float, default=1.0,
                    help="cfg.doc_path_weight for the raw search() (default 1 = off; pass the config value to check "
                         "the implementation: variant `base` then equals the matching B/Bw variant)")
    ap.add_argument("--embed-timeout", type=float, default=None,
                    help="bench only: seconds for the query-embed calls (a loaded node answers in ~8 s, "
                         "while embed.QUERY_URL_TIMEOUT is 2 s)")
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS), choices=list(VARIANTS))
    ap.add_argument("--cases", default=str(HERE / "golden_live.jsonl"))
    args = ap.parse_args()
    cases = [json.loads(l) for l in Path(args.cases).read_text().splitlines() if l.strip()]
    if args.project:
        cases = [c for c in cases if c["project"] == args.project]
    cfg = cfgmod.load_config()
    cfg.test_path_weight = args.test_path_weight
    cfg.doc_path_weight = args.doc_path_weight
    if args.embed_timeout:
        embed.QUERY_URL_TIMEOUT = args.embed_timeout
    db = open_ro()
    roots = {r["path"]: r["root"] for r in db.execute("SELECT path, root FROM projects")}
    ctxs, skipped = {}, []
    for name in sorted({c["project"] for c in cases}):
        c = ctxmod.current(db, roots[name]) if name in roots else None
        if c is None:
            skipped.append(name)
        else:
            ctxs[name] = c
    for name in skipped:
        print(f"skip: project {name} is not in this machine's registry", file=sys.stderr)
    cases = [c for c in cases if c["project"] in ctxs]
    qv = load_qvecs(cfg, sorted({c["q"] for c in cases}), HERE / "tmp" / "live_qvec.json")
    raw = {}
    for c in cases:
        hits = searchmod.search(db, cfg, None, c["q"], ctxs[c["project"]], k=args.k, scope="project", kind="all",
                                qvec=qv[c["q"]])
        raw[c["q"], c["project"]] = [(h.data.get("path"), h.score, h.kind, h.route) for h in hits]
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    variants, allper = {}, []
    for vname in args.variants:
        per, rows = [], []
        for c in cases:
            hs = rerank(raw[c["q"], c["project"]], c["q"], vname)
            files, mem = files_of(hs)
            m = score_case(c, files)
            route = hs[0][3] if hs else "none"
            rows.append({**c, **m})
            per.append({"variant": vname, "project": c["project"], "group": c["group"], "split": c["split"],
                        "route": route, "rank": m["rank"] if m["rank"] is not None else "",
                        "test_above_code": m["test_above_code"], "docs_above_code": m["docs_above_code"],
                        "top5": " | ".join(f"{p}{'*' if is_test_path(p) else ''}" for p in files[:5]),
                        "expected": " | ".join(c["expected"]), "memory_hits": mem})
        buckets = defaultdict(list)
        for r in rows:
            buckets[f"{r['group']}/{r['split']}"].append(r)
            buckets[f"{r['group']}/all"].append(r)
        variants[vname] = {k: aggregate(buckets[k]) for k in sorted(buckets)}
        allper.extend(per)
    (out / "live_metrics.json").write_text(json.dumps({"model": cfg.model, "k": args.k, "skipped": skipped,
                                                        "variants": variants}, ensure_ascii=False, indent=2) + "\n")
    with (out / "live_per_query.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(allper[0]))
        w.writeheader()
        w.writerows(allper)
    for vname, agg in variants.items():
        print(f"== {vname}")
        for k, v in agg.items():
            if k.split("/")[0] in ("test-seeking", "where-code", "identifier", "docs-ish", "arch-code", "docs-seeking"):
                print(f"{k:22} n={v['n']:2} R@5={v['R@5']:.2f} R@10={v['R@10']:.2f} MRR={v['MRR']:.3f} "
                      f"tac={v['test_above_code']:.2f} t@3={v['tests@3']:.2f} "
                      f"dac={v['docs_above_code']:.2f} d@3={v['docs@3']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
