"""Renders results/*.json into markdown tables -> results/tables.md (pasted into REPORT.md)."""
import json, os
from common import RES
L = lambda n: json.load(open(os.path.join(RES, n))) if os.path.exists(os.path.join(RES, n)) else {}
M = L("metrics.json"); out = []
def f(x): return f"{x:.3f}"
def table(title, groups, modes_filter=None):
    out.append(f"\n### {title}\n"); hdr = "| Варіант | Режим | " + " | ".join(f"R@5 {g}|R@10 {g}|MRR {g}" .replace("|", " | ") for g in groups) + " |"
    out.append(hdr.replace("|  |", "|")); out.append("|" + "---|" * (2 + 3 * len(groups)))
    for k in sorted(M, key=lambda k: (k.split("|")[0] == "-", k)):
        var, mode = k.split("|")
        if modes_filter and mode not in modes_filter: continue
        row = [var, mode]
        for g in groups:
            m = M[k].get(g)
            row += [f(m["recall@5"]), f(m["recall@10"]), f(m["mrr"])] if m else ["-"] * 3
        out.append("| " + " | ".join(row) + " |")
table("Усі запити (n=45)", ["all"])
table("За типом: semantic (n=35) / identifier (n=10)", ["semantic", "identifier"])
table("Semantic за мовою: uk (13) / ru (10) / en (12)", ["semantic:uk", "semantic:ru", "semantic:en"])
E = {k: L(f"embed_{k}.json") for k in ("qwen3", "bge-m3", "gemma")}
out.append("\n### Індексація (embedding через Ollama)\n\n| Модель | dims | chunks | chunks/s | хв на 30k |\n|---|---|---|---|---|")
for k, v in E.items():
    if v: out.append(f"| {v['model']} | {v['dims']} | {v['chunks']} | {v['chunks_per_s']:.1f} | {v['embed_secs']/60:.1f} |")
LE = L("latency_embed.json")
out.append("\n### Латентність (warm, p50/p95, мс)\n\n| Крок | p50 | p95 |\n|---|---|---|")
for k, v in LE.items(): out.append(f"| embedding запиту, {v['model']} (Ollama) | {v['p50_ms']:.0f} | {v['p95_ms']:.0f} |")
LS = L("latency_sqlite.json")
for k, v in LS.get("sqlite_search", {}).items(): out.append(f"| sqlite пошук (30k chunks): {k} | {v['p50_ms']:.1f} | {v['p95_ms']:.1f} |")
out.append("\n### Розмір БД (30k chunks)\n\n| Файл | MB |\n|---|---|")
for k, v in LS.get("db_sizes_bytes", {}).items(): out.append(f"| {k} | {v/1e6:.0f} |")
T = L("tokens.json")
if T:
    c = T.get("_corpus", {}); out.append(f"\n### Токени на відповідь (top-5 chunks, chars/4). Середній chunk = {c.get('avg_chunk_chars',0):.0f} симв., медіана {c.get('median_chunk_chars',0):.0f}\n\n| Варіант\\|режим | повні chunks | шлях + 3 рядки |\n|---|---|---|")
    for k, v in T.items():
        if k != "_corpus": out.append(f"| {k} | {v['whole_chunks_tokens']:.0f} | {v['path_plus_3_lines_tokens']:.0f} |")
LT = L("loadtest.json")
if LT:
    out.append("\n### sqlite-vec: синтетичний load test, k=50, p50 / p95 мс\n\n| rows x dims | f32 full scan | f32 partition (1 з 100) | int8 | bit top-500 + f32 rescoring | (bit coarse only) | f32 MB | int8 MB | bit+f32 blobs MB |\n|---|---|---|---|---|---|---|---|---|")
    for r in LT.get("synthetic", []):
        g = lambda k: f"{r[k]['p50_ms']:.1f} / {r[k]['p95_ms']:.1f}"
        out.append(f"| {r['rows']:,} x {r['dims']} | {g('f32_full_scan')} | {g('f32_partition_1of100')} | {g('int8')} | {g('bit_coarse500_rescore_f32')} | {g('bit_coarse_only')} | {r['f32_size_mb']:.0f} | {r['i8_size_mb']:.0f} | {r['bit_db_size_mb_incl_f32_blobs']:.0f} |")
    out.append("\nПобудова індексу (с): " + "; ".join(f"{r['rows']:,}x{r['dims']}: f32 {r['f32_build_s']:.0f}, part {r['part_build_s']:.0f}, int8 {r['i8_build_s']:.0f}, bit {r['bit_build_s']:.0f}" for r in LT.get("synthetic", [])))
    rq = LT.get("real_quant_qwen3", {})
    if rq:
        out.append("\n### Якість квантизації на реальних qwen3-ембедінгах (overlap з f32 exact top-k, 45 запитів, 30k chunks)\n\n| dims | int8@10 | int8@50 | bit→rescore@10 | bit→rescore@50 | bit only@10 |\n|---|---|---|---|---|---|")
        for d, v in rq.items(): out.append(f"| {d} | {v['int8@10']:.3f} | {v['int8@50']:.3f} | {v['bit_rescore@10']:.3f} | {v['bit_rescore@50']:.3f} | {v['bit_only@10']:.3f} |")
open(os.path.join(RES, "tables.md"), "w").write("\n".join(out) + "\n"); print("\n".join(out))
