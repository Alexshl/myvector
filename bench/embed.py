"""Embed all chunks + golden queries with each model; checkpoints parts to bench/emb/<key>_parts/.
Usage: uv run embed.py [qwen3 bge-m3 gemma]   (default: all). Safe to re-run: finished parts are skipped."""
import json, os, subprocess, sys, time
import numpy as np
from common import *
PART = 1024
BATCH = 32

def ensure_model(name):
    out = subprocess.run(["ollama", "list"], capture_output=True, text=True).stdout
    if name.split(":")[0] not in out:
        print("pulling", name, flush=True); subprocess.run(["ollama", "pull", name], check=True)

def run(key):
    name, qpre, dpre = MODELS[key]
    ensure_model(name)
    chunks = load_chunks(); N = len(chunks)
    pdir = os.path.join(EMB, f"{key}_parts"); os.makedirs(pdir, exist_ok=True)
    metap = os.path.join(pdir, "meta.json")
    meta = json.load(open(metap)) if os.path.exists(metap) else {"secs": 0.0, "chunks": 0}
    texts = [dpre + c["text"] for c in chunks]
    t_run = time.time(); done_here = 0
    for p in range(0, N, PART):
        fp = os.path.join(pdir, f"{p:06d}.npy")
        if os.path.exists(fp): continue
        t0 = time.time(); vecs = []
        for i in range(p, min(p + PART, N), BATCH):
            vecs += ollama_embed(name, texts[i:i + BATCH])
        np.save(fp, np.asarray(vecs, dtype=np.float32))
        dt = time.time() - t0; n = min(PART, N - p)
        meta["secs"] += dt; meta["chunks"] += n; done_here += n
        json.dump(meta, open(metap, "w"))
        el = time.time() - t_run
        print(f"[{key}] {p + n}/{N}  {n / dt:.1f} chunks/s  elapsed {el / 60:.1f} min", flush=True)
    parts = [np.load(os.path.join(pdir, f"{p:06d}.npy")) for p in range(0, N, PART)]
    D = np.concatenate(parts)
    assert D.shape[0] == N
    np.save(os.path.join(EMB, f"{key}_docs.npy"), D)
    gold = load_golden()
    Q = np.asarray(ollama_embed(name, [qpre + g["q"] for g in gold]), dtype=np.float32)
    np.save(os.path.join(EMB, f"{key}_queries.npy"), Q)
    os.makedirs(RES, exist_ok=True)
    json.dump({"model": name, "dims": int(D.shape[1]), "chunks": N, "embed_secs": meta["secs"], "chunks_per_s": meta["chunks"] / max(meta["secs"], 1e-9)},
              open(os.path.join(RES, f"embed_{key}.json"), "w"), indent=1)
    print(f"[{key}] done: {D.shape}, {meta['chunks'] / max(meta['secs'], 1e-9):.1f} chunks/s overall", flush=True)

if __name__ == "__main__":
    keys = sys.argv[1:] or list(MODELS)
    for k in keys: run(k)
