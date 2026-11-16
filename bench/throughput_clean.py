"""Quick re-measurement of embedding throughput (512 mid-corpus chunks, batch 32) per model; records load average. Output: results/throughput_clean.json"""
import json, os, time
from common import *
ch = load_chunks()[10000:10512]; out = {"loadavg": os.getloadavg()}
for key, (name, _, dpre) in MODELS.items():
    texts = [dpre + c["text"] for c in ch]; ollama_embed(name, texts[:8])   # warm
    t = time.time()
    for i in range(0, len(texts), 32): ollama_embed(name, texts[i:i + 32])
    out[key] = {"chunks_per_s": len(texts) / (time.time() - t), "min_for_30k": 30233 / (len(texts) / (time.time() - t)) / 60}
    print(key, out[key], flush=True)
out["loadavg_after"] = os.getloadavg()
json.dump(out, open(os.path.join(RES, "throughput_clean.json"), "w"), indent=1)
