"""Helpers: a fake `cm` executable (Python script in tmp) driven by a JSON file; never the real cm/tmux/claude."""
import json
import os
import stat
import sys
from pathlib import Path

SCRIPT = '''#!%(py)s
import json, os, sys, time
base = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(base, "cfg.json")))
a = sys.argv[1:]
stdin = sys.stdin.read() if ("-" in a and a[0] == "send") or (a[0] == "answer" and "--text" in a and "--help" not in a) else None
log = os.path.join(base, "calls.jsonl")
n = sum(1 for ln in open(log) if json.loads(ln)["argv"][0] == a[0] and "--help" not in json.loads(ln)["argv"]) if os.path.exists(log) else 0
open(log, "a").write(json.dumps({"argv": a, "stdin": stdin}) + chr(10))
def pick(v):
    return v[min(n, len(v) - 1)] if isinstance(v, list) else v
if "--help" in a:
    if (cfg.get("old") and a[0] == "send") or (cfg.get("no_answer") and a[0] == "answer"):
        sys.exit(2)
    print(cfg.get("send_help", "usage: cm send <id> <text|-> [--wait N]"))
    sys.exit(0)
if a[0] == "who":
    print(json.dumps(pick(cfg.get("who", {}))))
    sys.exit(pick(cfg.get("who_code", 0)))
if a[0] == "ls":
    print(json.dumps(cfg.get("ls", [])))
    sys.exit(0)
time.sleep(cfg.get("sleep", 0) if a[0] == "send" else 0)
if cfg.get("echo") and stdin:
    sys.stderr.write("failed: " + stdin)
sys.exit(pick(cfg.get("codes", {}).get(a[0], 0)))
'''


class FakeCm:
    def __init__(self, base: Path):
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / "cm"
        self.path.write_text(SCRIPT % {"py": sys.executable})
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self.set()

    def set(self, **cfg) -> None:
        (self.base / "cfg.json").write_text(json.dumps(cfg))

    def calls(self) -> list[dict]:
        p = self.base / "calls.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def real(self) -> list[dict]:
        return [c for c in self.calls() if "--help" not in c["argv"]]

    def verbs(self) -> list[str]:
        return [c["argv"][0] for c in self.calls() if "--help" not in c["argv"]]
