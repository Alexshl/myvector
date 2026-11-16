"""Build bench corpus: bench/emb/chunks.jsonl (one chunk per line).
Usage: python corpus.py [--stats]
Golden-set files are always force-included (so the cap-sampling never drops an expected file)."""
import json, os, re, subprocess, sys, random, collections
HOME = os.path.expanduser("~/projects")
BENCH = os.path.dirname(os.path.abspath(__file__))
REPOS = {  # name -> (path, extra excluded dirs)
    "project-b/site": f"{HOME}/project-b/site",
    "project-b/maximus-libs": f"{HOME}/project-b/maximus-libs",
    "project-b/ocr": f"{HOME}/project-b/ocr",
    "project-d.se/public_html": f"{HOME}/project-d.se/public_html",
    "project-g": f"{HOME}/project-g",
}
EXCL_DIR = re.compile(r"(^|/)(vendor|node_modules|dist|build|templates_c|graphify-out|\.git|\.idea|\.claude)/")
EXCL_NAME = re.compile(r"(\.min\.|\.lock$|-lock\.|lock\.json$|\.sql$|\.pem$|(^|/)\.env|password|secret|\.map$)", re.I)
TEXT_EXT = {"php","js","ts","tsx","jsx","vue","py","cs","md","html","htm","tpl","css","scss","json","yml","yaml","xml","sh","txt","csproj","xsd","prg","cmd","ini","conf","twig","blade","rules","java","go","rb"}
MAXBYTES = 1_000_000
WIN, OVER = 60, 10
CAP = 30000
MAXCHARS = 3000  # truncate pathological chunks (long lines)

def golden_files():
    p = os.path.join(BENCH, "golden.jsonl")
    s = set()
    if os.path.exists(p):
        for l in open(p):
            if l.strip():
                s.update(json.loads(l)["expected"])
    return s

def list_files(repo, path):
    out = subprocess.run(["git", "-C", path, "ls-files", "-z"], capture_output=True).stdout.decode("utf8", "replace").split("\0")
    res = []
    for f in out:
        if not f or EXCL_DIR.search(f) or EXCL_NAME.search(f): continue
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if ext not in TEXT_EXT: continue
        fp = os.path.join(path, f)
        try:
            if not os.path.isfile(fp) or os.path.getsize(fp) > MAXBYTES or os.path.getsize(fp) == 0: continue
        except OSError: continue
        res.append(f)
    return res

def chunk_file(repo, path, f):
    try:
        data = open(os.path.join(path, f), "rb").read()
    except OSError: return []
    if b"\0" in data[:4096]: return []
    lines = data.decode("utf8", "replace").splitlines()
    chunks, step, i = [], WIN - OVER, 0
    while i < len(lines):
        seg = lines[i:i + WIN]
        body = "\n".join(seg).strip()
        if len(body) >= 20:
            txt = f"{repo}/{f}\n{body}"[:MAXCHARS]
            chunks.append({"file": f"{repo}/{f}", "start": i + 1, "end": i + len(seg), "text": txt})
        if i + WIN >= len(lines): break
        i += step
    return chunks

def main():
    gold = golden_files()
    per_repo = {}
    for repo, path in REPOS.items():
        files = list_files(repo, path)
        per_repo[repo] = {f: chunk_file(repo, path, f) for f in files}
        per_repo[repo] = {f: c for f, c in per_repo[repo].items() if c}
    tot = {r: sum(len(c) for c in d.values()) for r, d in per_repo.items()}
    print("raw chunks per repo:", tot, "total", sum(tot.values()))
    if "--stats" in sys.argv: return
    if os.path.exists(os.path.join(BENCH, "emb", "chunks.jsonl")) and "--force" not in sys.argv:
        sys.exit("emb/chunks.jsonl exists (embeddings depend on its row order); pass --force to rebuild and then re-embed")
    total = sum(tot.values())
    final = []
    rnd = random.Random(42)
    small = sum(v for v in tot.values() if v < 1000)
    large = total - small
    for repo, d in per_repo.items():
        # small repos keep everything; large ones share the rest of the cap proportionally
        budget = tot[repo] if tot[repo] < 1000 or total <= CAP else (CAP - small) * tot[repo] / large
        files = sorted(d)
        forced = [f for f in files if f"{repo}/{f}" in gold]
        rest = [f for f in files if f"{repo}/{f}" not in gold]
        rnd.shuffle(rest)
        sel, n = list(forced), sum(len(d[f]) for f in forced)
        for f in rest:
            if total > CAP and n >= budget: break
            sel.append(f); n += len(d[f])
        for f in sorted(sel): final += d[f]
        print(f"  {repo}: files {len(sel)}/{len(files)} chunks {sum(len(d[f]) for f in sel)}")
    for i, c in enumerate(final): c["id"] = i
    with open(os.path.join(BENCH, "emb", "chunks.jsonl"), "w") as fh:
        for c in final: fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print("final chunks", len(final), "chars", sum(len(c["text"]) for c in final))
if __name__ == "__main__": main()
