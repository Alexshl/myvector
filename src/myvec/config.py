"""Paths and configuration (config.toml in MYVEC_HOME)."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

QWEN_QUERY = "Instruct: Given a search query, retrieve relevant code or documentation passages\nQuery: "

# model-name prefix -> (query_prefix, doc_prefix); overridable in config.toml [prefixes."<model>"]
BUILTIN_PREFIXES: dict[str, tuple[str, str]] = {
    "qwen3-embedding": (QWEN_QUERY, ""),
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "bge-m3": ("", ""),
}

DEFAULT_WEIGHTS = {
    "current_repo": 1.0,
    "sibling_repo": 0.8,
    "project_memory": 1.0,
    "sibling_memory": 0.8,
    "global_memory": 0.6,
    "other_project": 0.5,
}


def home() -> Path:
    return Path(os.environ.get("MYVEC_HOME") or Path.home() / "MyVector").expanduser()


def db_path() -> Path:
    return home() / "index.db"


def load_db_path() -> Path:
    """Node load samples (ADR-012): a separate database, so the big index.db is not written every minute."""
    return home() / "load.db"


def tokens_db_path() -> Path:
    """Token usage from Claude Code transcripts (ADR-013): a separate database, not derived from the index."""
    return home() / "tokens.db"


def tokens_lock_path() -> Path:
    return home() / "state" / "tokens.lock"


def lock_path() -> Path:
    return home() / "sync.lock"


def vault_dir() -> Path:
    return home() / "vault"


def logs_dir() -> Path:
    return home() / "logs"


def daemon_json_path() -> Path:
    return home() / "daemon.json"


def activity_path() -> Path:
    return home() / "activity"


def session_events_path() -> Path:
    return home() / "state" / "session_events.jsonl"


def test_runs_path() -> Path:
    return home() / "state" / "test_runs.jsonl"


def registry_path() -> Path:
    return home() / "registry.toml"


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    for sub in ("vault/global", "vault/projects", "logs"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    return h


@dataclass
class Config:
    model: str = "embeddinggemma"
    dims: int = 768
    ollama_url: str = "http://localhost:11434"
    query_url: str = ""             # optional separate embedding server for search queries only (same model file!); "" = ollama_url
    embed_api: str = "ollama"       # "ollama" (/api/embed) or "openai" (/v1/embeddings: llama.cpp llama-server, vLLM...)
    output_budget_tokens: int = 2000   # budget of `myvec search` output (chars/4)
    expand_top: int = 3                # the top N code hits are expanded...
    expand_lines: int = 25             # ...to this many lines (the rest: path:lines + 3 lines)
    k: int = 10                        # default number of hits
    lessons_project: str = "vector-local"   # target project of `myvec ticket lesson` (ADR-027)
    max_memory_hits: int = 3           # at most this many memories in the merged (--kind all) output
    import_weight: float = 0.85        # weight of imported (Claude auto-memory, CLAUDE.md...) vs. written memories
    weight_softness: float = 0.5       # scope weights are pulled to 1: w_eff = 1 - softness * (1 - w); 0 = off, 1 = raw
    doc_path_weight: float = 0.9       # score multiplier of tickets (`docs/todo/`) for natural-language (vector-route) queries without a docs word; 1 = off (E8-21)
    test_path_weight: float = 0.8      # score multiplier of test files for natural-language (vector-route) queries without a test word; 1 = off (E8-20)
    batch_size: int = 16
    window_hours: float = 2.0       # default activity window of a project (see `myvec mode`)
    daemon_port: int = 7878
    session_idle_minutes: float = 15.0   # a "working" session silent this long is shown as idle
    session_retention_days: float = 30.0   # ended sessions are deleted after this many days
    cm_path: str = "~/.local/bin/cm"   # claude-manager CLI used by the "resume session" action (ADR-010)
    cm_timeout_s: float = 90.0         # one `cm` call (restart + send --wait)
    load_enabled: bool = True          # node load sampler thread in the daemon (ADR-012)
    load_sample_s: float = 5.0         # seconds between node load snapshots
    load_raw_hours: float = 24.0       # raw snapshots are kept this long
    load_minutes_days: float = 30.0    # per-minute aggregates are kept this long
    load_temp_sensor: str = ""         # psutil sensor name overriding the CPU temperature auto-pick
    load_alert_pct: float = 85.0       # CPU alert: whole-machine CPU at or above this % (ADR-018)
    load_alert_minutes: float = 5.0    # ... for longer than this many minutes (<= 55: the ring keeps 1 h)
    test_regression_pct: float = 25.0  # a test run this % slower (wall or CPU) than the median is a regression
    test_regression_runs: int = 10     # median of this many previous runs of the same key
    test_regression_min_runs: int = 3  # fewer previous runs: no regression verdict
    test_regression_min_abs_s: float = 2.0   # ... and the excess must be at least this many seconds
    tokens_enabled: bool = True        # token usage collector in the daemon (ADR-013)
    tokens_scan_s: float = 60.0        # seconds between transcript scans
    tokens_scan_budget_mb: int = 512   # transcript bytes read per scan (a big first import takes several passes)
    claude_projects_dir: str = ""      # "" = $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects
    keep_alive: str = "2h"          # Ollama keep_alive sent with embedding requests ("" = Ollama default)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    prefixes: dict[str, dict[str, str]] = field(default_factory=dict)

    def prefix_pair(self) -> tuple[str, str]:
        """(query_prefix, doc_prefix) for the configured model."""
        if self.model in self.prefixes:
            p = self.prefixes[self.model]
            return p.get("query", ""), p.get("doc", "")
        for fam, pair in BUILTIN_PREFIXES.items():
            if self.model.startswith(fam):
                return pair
        return "", ""


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def default_config_text() -> str:
    cfg = Config()
    lines = [
        "# myvec global config",
        f"model = {_toml_str(cfg.model)}",
        f"dims = {cfg.dims}",
        f"ollama_url = {_toml_str(cfg.ollama_url)}",
        f"query_url = {_toml_str(cfg.query_url)}",
        f"embed_api = {_toml_str(cfg.embed_api)}",
        f"output_budget_tokens = {cfg.output_budget_tokens}",
        f"expand_top = {cfg.expand_top}",
        f"expand_lines = {cfg.expand_lines}",
        f"k = {cfg.k}",
        f"lessons_project = {_toml_str(cfg.lessons_project)}",
        f"max_memory_hits = {cfg.max_memory_hits}",
        f"import_weight = {cfg.import_weight}",
        f"weight_softness = {cfg.weight_softness}",
        f"test_path_weight = {cfg.test_path_weight}",
        f"doc_path_weight = {cfg.doc_path_weight}",
        f"batch_size = {cfg.batch_size}",
        f"window_hours = {cfg.window_hours}",
        f"daemon_port = {cfg.daemon_port}",
        f"session_idle_minutes = {cfg.session_idle_minutes:g}",
        f"session_retention_days = {cfg.session_retention_days:g}",
        f"cm_path = {_toml_str(cfg.cm_path)}",
        f"cm_timeout_s = {cfg.cm_timeout_s:g}",
        f"load_enabled = {'true' if cfg.load_enabled else 'false'}",
        f"load_sample_s = {cfg.load_sample_s:g}",
        f"load_raw_hours = {cfg.load_raw_hours:g}",
        f"load_minutes_days = {cfg.load_minutes_days:g}",
        f"load_temp_sensor = {_toml_str(cfg.load_temp_sensor)}",
        f"load_alert_pct = {cfg.load_alert_pct:g}",
        f"load_alert_minutes = {cfg.load_alert_minutes:g}",
        f"test_regression_pct = {cfg.test_regression_pct:g}",
        f"test_regression_runs = {cfg.test_regression_runs}",
        f"test_regression_min_runs = {cfg.test_regression_min_runs}",
        f"test_regression_min_abs_s = {cfg.test_regression_min_abs_s:g}",
        f"tokens_enabled = {'true' if cfg.tokens_enabled else 'false'}",
        f"tokens_scan_s = {cfg.tokens_scan_s:g}",
        f"tokens_scan_budget_mb = {cfg.tokens_scan_budget_mb}",
        f"claude_projects_dir = {_toml_str(cfg.claude_projects_dir)}",
        f"keep_alive = {_toml_str(cfg.keep_alive)}",
        "",
        "[weights]",
    ]
    lines += [f"{k} = {v}" for k, v in DEFAULT_WEIGHTS.items()]
    for fam, (q, d) in BUILTIN_PREFIXES.items():
        lines += ["", f"[prefixes.{_toml_str(fam)}]", f"query = {_toml_str(q)}", f"doc = {_toml_str(d)}"]
    return "\n".join(lines) + "\n"


def load_config() -> Config:
    h = ensure_home()
    path = h / "config.toml"
    if not path.exists():
        path.write_text(default_config_text(), encoding="utf-8")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = Config()
    for key in ("model", "ollama_url", "query_url", "embed_api", "keep_alive", "cm_path", "load_temp_sensor",
                "claude_projects_dir", "lessons_project"):
        if key in data:
            setattr(cfg, key, str(data[key]))
    for key in ("dims", "output_budget_tokens", "expand_top", "expand_lines", "k", "batch_size", "daemon_port",
                "max_memory_hits", "tokens_scan_budget_mb", "test_regression_runs", "test_regression_min_runs"):
        if key in data:
            setattr(cfg, key, int(data[key]))
    if "token_budget" in data and "output_budget_tokens" not in data:  # pre-0.2 name
        cfg.output_budget_tokens = int(data["token_budget"])
    if "window_hours" in data:
        cfg.window_hours = float(data["window_hours"])
    for key in ("session_idle_minutes", "session_retention_days", "cm_timeout_s", "load_sample_s",
                "load_raw_hours", "load_minutes_days", "tokens_scan_s", "load_alert_pct", "load_alert_minutes",
                "test_regression_pct", "test_regression_min_abs_s"):
        if key in data:
            setattr(cfg, key, float(data[key]))
    cfg.load_alert_pct = min(100.0, max(1.0, cfg.load_alert_pct))
    cfg.load_alert_minutes = min(55.0, max(1.0, cfg.load_alert_minutes))
    if cfg.test_regression_pct <= 0:
        cfg.test_regression_pct = Config.test_regression_pct
    cfg.test_regression_min_abs_s = max(0.0, cfg.test_regression_min_abs_s)
    cfg.test_regression_runs = min(50, max(3, cfg.test_regression_runs))
    cfg.test_regression_min_runs = min(cfg.test_regression_runs, max(1, cfg.test_regression_min_runs))
    cfg.load_sample_s = max(1.0, cfg.load_sample_s)   # a lower bound keeps the sampler cheap
    # the snapshot ring (RING snapshots) must hold the alert window with some slack
    cfg.load_alert_minutes = min(cfg.load_alert_minutes, max(1.0, 720 * cfg.load_sample_s / 60 - 1))
    cfg.tokens_scan_s = max(1.0, cfg.tokens_scan_s)
    cfg.tokens_scan_budget_mb = max(1, cfg.tokens_scan_budget_mb)
    if "load_enabled" in data:
        cfg.load_enabled = bool(data["load_enabled"])
    if "tokens_enabled" in data:
        cfg.tokens_enabled = bool(data["tokens_enabled"])
    if "weight_softness" in data:
        cfg.weight_softness = float(data["weight_softness"])
    if "test_path_weight" in data:
        cfg.test_path_weight = min(1.0, max(0.0, float(data["test_path_weight"])))
    if "doc_path_weight" in data:
        cfg.doc_path_weight = min(1.0, max(0.0, float(data["doc_path_weight"])))
    if "import_weight" in data:
        cfg.import_weight = float(data["import_weight"])
    cfg.weights.update({k: float(v) for k, v in data.get("weights", {}).items()})
    # prefixes keyed by model family or full model name; resolve by startswith
    raw = data.get("prefixes", {})
    for name, val in raw.items():
        cfg.prefixes[name] = {k: str(v) for k, v in val.items()}
    # family keys apply to any model starting with that name
    if cfg.model not in cfg.prefixes:
        for name in list(cfg.prefixes):
            if cfg.model.startswith(name):
                cfg.prefixes[cfg.model] = cfg.prefixes[name]
                break
    return cfg


# ---- project registry (list of project roots, so index.db is rebuildable) ----

def claude_projects_root(cfg: "Config | None" = None) -> Path:
    """Directory with Claude Code transcripts (`<proj>/<session>.jsonl`)."""
    explicit = (cfg.claude_projects_dir if cfg is not None else load_config().claude_projects_dir).strip()
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("MYVEC_CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def load_registry() -> list[str]:
    p = registry_path()
    if not p.exists():
        return []
    try:
        return list(tomllib.loads(p.read_text(encoding="utf-8")).get("roots", []))
    except Exception:
        return []


def save_registry(roots: list[str]) -> None:
    ensure_home()
    body = "roots = [\n" + "".join(f"  {_toml_str(r)},\n" for r in sorted(set(roots))) + "]\n"
    tmp = registry_path().with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, registry_path())


def register_root(root: str) -> None:
    roots = load_registry()
    if root not in roots:
        save_registry(roots + [root])


def unregister_roots(roots: list[str]) -> int:
    """Drop roots from the registry (atomic rewrite); returns how many were present."""
    cur = load_registry()
    drop = set(roots)
    keep = [r for r in cur if r not in drop]
    if len(keep) != len(cur):
        save_registry(keep)
    return len(cur) - len(keep)
