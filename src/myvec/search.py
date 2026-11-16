"""Search with query routing (see `resolve_route`):

- identifier-like query (a single token: `renderPdf`, `Foo::bar`, `a.b.c`, `snake_case`) -> hybrid:
  RRF(k=60) of vector top-50 + FTS5 (unicode61) top-50;
- natural-language query -> vector only (FTS only adds noise for uk/ru queries over English code).

Scoring (one scale for code and memory, so the two kinds are merged by the final score):

- vector route: base = cosine similarity of the hit;
- hybrid route: base = RRF normalised to [0,1] by the best possible value 2/(k+1);
- final = base x softened scope weight (`soften`): the weights are a tie-breaker, not a verdict.

Cross-project scans (`scope=all`) over > BIT_THRESHOLD rows use a binary-quantized
coarse pass (top-500) with float32 rescoring.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field

from myvec import db as dbmod
from myvec import gitutil, projects, ticketdb
from myvec.config import Config
from myvec.ctx import Ctx, branch_visible
from myvec.embed import EmbedError, Embedder
from myvec.searchkind import DOC_EXTS   # `--kind docs`: a path filter over code chunks (ADR-032); lives in the light module

RRF_K = 60
TOP = 50
OVERFETCH = 200
BIT_THRESHOLD = 100_000   # rows in scope above which a `scope=all` code scan goes through the bit[] index
BIT_COARSE = 500
RRF_MAX = 2.0 / (RRF_K + 1)   # best possible RRF: rank 1 in both lists
KINDS = ("code", "memory", "docs", "all")


def is_doc_path(path: str) -> bool:
    return path.lower().endswith(DOC_EXTS)


_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_NAME = re.compile(r"(^test_.*\.py$)|(_test\.(py|go)$)|(\.(test|spec)\.[^.]+$)|((?-i:Test\.php$))", re.I)
_TEST_WORD = re.compile(r"\b(тест|test|spec|pytest|фікстур|фикстур|fixture)", re.I)


def is_test_path(path: str) -> bool:
    """A test file: a `tests/ test/ __tests__/ spec/` directory component or a test file name (case-insensitive)."""
    parts = path.replace("\\", "/").split("/")
    return any(p.lower() in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_NAME.search(parts[-1]))


def mentions_test(query: str) -> bool:
    """The query is about tests (uk/ru/en): test files keep their score then (E8-20)."""
    return bool(_TEST_WORD.search(query))


_DOC_WORD = re.compile(r"\b(тікет|тикет|ticket|adr|рішен|решени|вирішил|решил|decision|план|plan|док|doc|readme|урок|lesson"
                       r"|чому|почему|why|звіт|отчёт|report|аудит|audit)", re.I)


def is_ticket_path(path: str) -> bool:
    """A ticket file: a doc (`is_doc_path`) under `docs/todo/` (case-insensitive, any depth of the repo) (E8-21)."""
    parts = [p.lower() for p in path.replace("\\", "/").split("/")]
    return is_doc_path(path) and any(a == "docs" and b == "todo" for a, b in zip(parts, parts[1:-1]))


def mentions_docs(query: str) -> bool:
    """The query is about docs/tickets/decisions (uk/ru/en): tickets keep their score then (E8-21)."""
    return bool(_DOC_WORD.search(query))


def doc_predicate(column: str = "bf.path") -> str:
    """SQL predicate matching `is_doc_path` (built from the same DOC_EXTS)."""
    return "(" + " OR ".join(f"lower({column}) LIKE '%{e}'" for e in DOC_EXTS) + ")"


@dataclass
class Hit:
    kind: str                 # 'code' | 'memory'
    key: tuple
    score: float = 0.0
    weight: float = 1.0
    data: dict = field(default_factory=dict)
    ranks: dict = field(default_factory=dict)   # list name -> 1-based rank (for the score breakdown)
    rrf: float = 0.0                             # sum of 1/(k+rank) over the lists the hit is in
    route: str = "hybrid"                        # 'vector' | 'hybrid' | 'fts-fallback' (which lists fed the ranking)
    similarity: float | None = None              # cosine similarity (1 - distance); None if the hit has no vector rank
    import_weight: float = 1.0                   # cfg.import_weight for imported memories
    weight_eff: float = 1.0                      # softened scope weight x softened import weight
    path_weight: float = 1.0                     # cfg.test_path_weight x cfg.doc_path_weight for test files / tickets in a natural-language query (E8-20, E8-21)


def soften(w: float, softness: float) -> float:
    """Pull a scope weight towards 1: 1 - softness * (1 - w). softness 0.5: 0.6 -> 0.8, 0.8 -> 0.9."""
    return 1.0 - softness * (1.0 - w)


def effective_route(query: str, mode: str, qvec) -> str:
    """The route actually taken: without a query vector the search is FTS-only, whatever was requested."""
    return "fts-fallback" if qvec is None else resolve_route(query, mode)


def resolve_route(query: str, mode: str = "auto") -> str:
    """'hybrid' or 'vector'. auto: a single whitespace-free token (identifier-like) -> hybrid, else vector."""
    if mode in ("vector", "hybrid"):
        return mode
    return "hybrid" if len(query.split()) == 1 else "vector"


def _fts_query(q: str, min_len: int = 1) -> str | None:
    toks = [t for t in re.findall(r"\w+", q, re.UNICODE) if len(t) >= min_len]
    seen, uniq = set(), []
    for t in toks:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    if not uniq:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in uniq[:16])


def effective_branch(db: sqlite3.Connection, repo: sqlite3.Row) -> str | None:
    """Branch whose manifest to search: the live HEAD if it has been synced, else the last synced one."""
    if gitutil.is_repo(repo["dir"]):
        live = gitutil.head_branch(repo["dir"])
        if live and db.execute("SELECT 1 FROM branch_files WHERE repo_id=? AND branch=? LIMIT 1",
                               (repo["id"], live)).fetchone():
            return live
    return repo["current_branch"]


def _at_project_root(ctx: Ctx) -> bool:
    """cwd is the project root itself (not inside any sub-repo): there is no "current repo" to prefer."""
    return ctx.repo_path == ctx.project_path


def _repo_weight(cfg: Config, ctx: Ctx | None, repo_path: str, project_path: str) -> float:
    w = cfg.weights
    if ctx is None:
        return w["other_project"]
    if repo_path == ctx.repo_path or (_at_project_root(ctx) and project_path == ctx.project_path):
        return w["current_repo"]
    if projects.top_level(project_path) == projects.top_level(ctx.project_path):
        return w["sibling_repo"]
    return w["other_project"]


def _memory_weight(cfg: Config, ctx: Ctx | None, scope: str, scope_mode: str) -> float | None:
    """None = not visible in this scope mode."""
    w = cfg.weights
    if scope == "global":
        if scope_mode == "repo":
            return None
        if ctx is not None and not ctx.marker.share_global:
            return None
        return w["global_memory"]
    if ctx is None:
        return w["other_project"] if scope_mode == "all" else None
    if scope in projects.ancestors(ctx.repo_path) or (
            _at_project_root(ctx) and (scope == ctx.project_path or scope.startswith(ctx.project_path + "/"))
            and scope_mode != "repo"):
        return w["project_memory"]
    if scope_mode == "repo":
        return None
    if projects.top_level(scope) == projects.top_level(ctx.project_path):
        return w["sibling_memory"]
    return w["other_project"] if scope_mode == "all" else None


_UNSET = object()


def embed_query(embedder: Embedder | None, query: str, *, debug=None, warn=None) -> list[float] | None:
    """The query vector, or None (FTS only) if there is no embedder or it fails. Needs no database: callers that
    hold a lock around the DB (the daemon) run this before taking it."""
    t0 = time.perf_counter()
    dbg = debug or (lambda s: None)
    qvec = None
    if embedder is not None:
        try:
            qvec = embedder.embed_query(query)
        except EmbedError as e:
            dbg(f"embedding failed, FTS only: {e}")
            if warn is not None:
                warn(f"warning: {e}; using full-text search only")
            else:
                import sys

                print(f"warning: {e}; using full-text search only", file=sys.stderr)
    dbg(f"embed query: {(time.perf_counter() - t0) * 1000:.0f} ms")
    return qvec


def search(db: sqlite3.Connection, cfg: Config, embedder: Embedder | None, query: str, ctx: Ctx | None, *,
           k: int = 8, scope: str = "project", kind: str = "all", debug=None, warn=None,
           show_all_branches: bool = False,
           mode: str = "auto", qvec=_UNSET, respect_shared: bool = True, hidden: set[str] | None = None) -> list[Hit]:
    """`qvec`: a precomputed query vector (or None for FTS-only); by default the query is embedded here.
    `respect_shared`: `scope=all` drops projects with `shared = false` (E8-05); `hidden` is an explicit set of such
    project paths (the `--project` drill-down), used for any scope."""
    t0 = time.perf_counter()
    route = resolve_route(query, mode)
    dbg = debug or (lambda s: None)
    if qvec is _UNSET:
        qvec = embed_query(embedder, query, debug=debug, warn=warn)
    qblob = dbmod.pack(qvec) if qvec is not None else None
    use_fts = route == "hybrid" or qblob is None   # no query vector: FTS is all we have
    dbg(f"route: {effective_route(query, mode, qvec)}")
    lists: dict[str, list[Hit]] = {}
    hits: dict[tuple, Hit] = {}
    if hidden is None:
        from myvec import crossproject
        hidden = crossproject.hidden_projects(db, ctx) if respect_shared and scope == "all" else set()

    docs = kind == "docs"
    if kind in ("code", "docs", "all"):
        t1 = time.perf_counter()
        repos = _scope_repos(db, ctx, scope)
        if hidden:
            repos = [r for r in repos if not _hidden_path(r["project_path"], hidden)]
        branch_of = {r["id"]: effective_branch(db, r) for r in repos}
        repos = [r for r in repos if branch_of[r["id"]]]
        db.execute("CREATE TEMP TABLE IF NOT EXISTS scope_repos(repo_id INTEGER PRIMARY KEY, branch TEXT, weight REAL, "
                   "rpath TEXT)")
        db.execute("DELETE FROM scope_repos")
        for r in repos:
            db.execute("INSERT INTO scope_repos VALUES(?,?,?,?)",
                       (r["id"], branch_of[r["id"]], _repo_weight(cfg, ctx, r["path"], r["project_path"]), r["path"]))
        if repos:
            if qblob is not None:
                lists["code_vec"] = _code_vec(db, repos, qblob, hits, big_scan=scope == "all" and not docs,
                                          docs=docs)
            if use_fts:
                lists["code_fts"] = _code_fts(db, "chunks_fts", _fts_query(query), hits, docs=docs)
        dbg(f"code search: {(time.perf_counter() - t1) * 1000:.0f} ms")
    if kind in ("memory", "all"):
        t1 = time.perf_counter()
        if qblob is not None:
            lists["mem_vec"] = _mem_vec(db, cfg, ctx, scope, qblob, hits, show_all_branches, hidden)
        if use_fts:
            lists["mem_fts"] = _mem_fts(db, cfg, ctx, scope, _fts_query(query), hits, show_all_branches, hidden)
        dbg(f"memory search: {(time.perf_counter() - t1) * 1000:.0f} ms")

    for name, lst in lists.items():
        for rank, h in enumerate(lst, 1):
            h.rrf += 1.0 / (RRF_K + rank)
            h.ranks[name] = rank
    out = [h for h in hits.values() if h.ranks]
    eff_route = effective_route(query, mode, qvec)
    test_w = cfg.test_path_weight if eff_route == "vector" and not mentions_test(query) else 1.0
    doc_w = cfg.doc_path_weight if eff_route == "vector" and kind != "docs" and not mentions_docs(query) else 1.0
    for h in out:
        h.weight_eff = soften(h.weight, cfg.weight_softness) * soften(h.import_weight, cfg.weight_softness)
        base = max(h.similarity, 0.0) if route == "vector" and h.similarity is not None else h.rrf / RRF_MAX
        if h.kind == "code":
            if test_w != 1.0 and is_test_path(h.data["path"]):
                h.path_weight = test_w
            if doc_w != 1.0 and is_ticket_path(h.data["path"]):
                h.path_weight *= doc_w
        h.score = base * h.weight_eff * h.path_weight
        h.route = eff_route
    out.sort(key=lambda h: -h.score)
    dbg(f"total: {(time.perf_counter() - t0) * 1000:.0f} ms, {len(out)} candidates")
    if kind == "all":
        # memories must not crowd out code: at most max_memory_hits of them (by score), the rest of the slots is code
        merged, mems = [], 0
        for h in out:
            if h.kind == "memory":
                if mems >= cfg.max_memory_hits:
                    continue
                mems += 1
            merged.append(h)
            if len(merged) >= k:
                break
        return annotate_tickets(db, merged)
    return annotate_tickets(db, out[:k])


def annotate_tickets(db, hits: list[Hit]) -> list[Hit]:
    """Mark code hits that are ticket files: `data['ticket'] = {project, id, title, status, progress}` (ADR-005).
    Ranking is untouched; one query per search."""
    code = [h for h in hits if h.kind == "code"]
    if not code:
        return hits
    try:
        ids = sorted({h.data["repo_id"] for h in code})
        dirs = {r[0]: r[1] for r in db.execute(f"SELECT id, dir FROM repos WHERE id IN ({','.join('?' * len(ids))})", ids)}
        abs_of = {id(h): dirs[h.data["repo_id"]].rstrip("/") + "/" + h.data["path"]
                  for h in code if h.data["repo_id"] in dirs}
        found = ticketdb.tickets_by_file(db, set(abs_of.values()))
    except sqlite3.Error:
        return hits
    for h in code:
        t = found.get(abs_of.get(id(h)))
        if t:
            h.data["ticket"] = t
    return hits


def _scope_repos(db, ctx: Ctx | None, scope: str) -> list[sqlite3.Row]:
    rows = db.execute("SELECT * FROM repos").fetchall()
    if scope == "all":
        return rows
    if ctx is None:
        return []
    if scope == "repo":
        return [r for r in rows if r["path"] == ctx.repo_path]
    top = projects.top_level(ctx.project_path)
    return [r for r in rows if projects.top_level(r["project_path"]) == top]


def _code_row(r) -> dict:
    return {"chunk_id": r["id"], "repo_id": r["repo_id"], "repo": r["rpath"], "path": r["path"],
            "start": r["start_line"], "end": r["end_line"], "symbol": r["symbol"], "text": r["text"]}


def _bit_candidates(db, qblob: bytes) -> dict[int, float] | None:
    """Coarse top-500 on the binary-quantized table, rescored with exact float32 cosine distance.
    None if the bit index is unavailable (-> plain float32 scan)."""
    if not dbmod.has_bit_index(len(qblob) // 4):
        return None
    try:
        n_bit = db.execute("SELECT count(*) FROM chunks_bit").fetchone()[0]
        n_vec = db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
        if n_bit != n_vec:  # index created before the bit table existed (or interrupted): backfill once
            db.execute("DELETE FROM chunks_bit")
            db.execute("INSERT INTO chunks_bit(rowid, embedding) SELECT rowid, vec_quantize_binary(embedding) "
                       "FROM chunks_vec")
        coarse = [r[0] for r in db.execute(
            "SELECT rowid FROM chunks_bit WHERE embedding MATCH vec_quantize_binary(?) AND k=?", (qblob, BIT_COARSE))]
        if not coarse:
            return {}
        marks = ",".join("?" * len(coarse))
        rows = db.execute(f"SELECT rowid, vec_distance_cosine(embedding, ?) FROM chunks_vec WHERE rowid IN ({marks})",
                          [qblob, *coarse]).fetchall()
    except sqlite3.OperationalError:
        return None
    rows.sort(key=lambda r: r[1])
    return {r[0]: r[1] for r in rows[:OVERFETCH]}


def _code_vec_bit(db, dist: dict[int, float], hits: dict) -> list[Hit]:
    if not dist:
        return []
    marks = ",".join("?" * len(dist))
    rows = db.execute(
        "SELECT cr.vec_rowid, c.id, cr.repo_id, c.start_line, c.end_line, c.symbol, c.text, bf.path, sr.rpath, sr.weight "
        "FROM chunk_repo cr JOIN chunks c ON c.id=cr.chunk_id "
        "JOIN scope_repos sr ON sr.repo_id=cr.repo_id "
        "JOIN branch_files bf ON bf.repo_id=cr.repo_id AND bf.blob_sha=c.blob_sha AND bf.branch=sr.branch "
        f"WHERE cr.vec_rowid IN ({marks}) GROUP BY cr.vec_rowid", list(dist)).fetchall()
    rows.sort(key=lambda r: dist[r["vec_rowid"]])
    out, seen = [], set()
    for r in rows:
        key = ("c", r["repo_id"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        h = hits.setdefault(key, Hit("code", key, weight=r["weight"], data=_code_row(r)))
        h.similarity = 1.0 - dist[r["vec_rowid"]]
        out.append(h)
    return out[:TOP]


def _code_vec(db, repos, qblob: bytes, hits: dict, big_scan: bool = False, docs: bool = False) -> list[Hit]:
    if big_scan:
        total = db.execute(f"SELECT count(*) FROM chunk_repo WHERE repo_id IN ({','.join('?' * len(repos))})",
                           [r["id"] for r in repos]).fetchone()[0]
        if total > BIT_THRESHOLD:
            dist = _bit_candidates(db, qblob)
            if dist is not None:
                return _code_vec_bit(db, dist, hits)
    cands: list[tuple[float, Hit]] = []
    for repo in repos:
        rid = repo["id"]
        k = OVERFETCH
        for _attempt in range(3):
            if docs:   # the filter goes into the KNN, not after it: docs would drown in nearer code chunks
                vrows = db.execute(
                    "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k=? AND repo_id=? AND rowid IN "
                    "(SELECT cr.vec_rowid FROM chunk_repo cr JOIN chunks c ON c.id=cr.chunk_id "
                    "JOIN scope_repos sr ON sr.repo_id=cr.repo_id "
                    "JOIN branch_files bf ON bf.repo_id=cr.repo_id AND bf.blob_sha=c.blob_sha AND bf.branch=sr.branch "
                    f"WHERE cr.repo_id=? AND {doc_predicate()})", (qblob, k, rid, rid)).fetchall()
            else:
                vrows = db.execute(
                    "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k=? AND repo_id=?",
                    (qblob, k, rid)).fetchall()
            if not vrows:
                break
            dist = {r[0]: r[1] for r in vrows}
            marks = ",".join("?" * len(dist))
            rows = db.execute(
                "SELECT cr.vec_rowid, c.id, cr.repo_id, c.start_line, c.end_line, c.symbol, c.text, bf.path, sr.rpath "
                "FROM chunk_repo cr JOIN chunks c ON c.id=cr.chunk_id "
                "JOIN scope_repos sr ON sr.repo_id=cr.repo_id "
                "JOIN branch_files bf ON bf.repo_id=cr.repo_id AND bf.blob_sha=c.blob_sha AND bf.branch=sr.branch "
                f"WHERE cr.vec_rowid IN ({marks}){' AND ' + doc_predicate() if docs else ''} "
                "GROUP BY cr.vec_rowid", list(dist)).fetchall()
            if len(rows) >= TOP or len(vrows) < k:
                break
            k *= 4  # many hits belong to other branches: over-fetch more
        if not vrows:
            continue
        w = db.execute("SELECT weight FROM scope_repos WHERE repo_id=?", (rid,)).fetchone()[0]
        for r in rows:
            key = ("c", rid, r["id"])
            h = hits.setdefault(key, Hit("code", key, weight=w, data=_code_row(r)))
            h.similarity = 1.0 - dist[r["vec_rowid"]]
            cands.append((dist[r["vec_rowid"]], h))
    cands.sort(key=lambda t: t[0])
    seen, out = set(), []
    for _, h in cands:
        if h.key not in seen:
            seen.add(h.key)
            out.append(h)
    return out[:TOP]


# branch_files is reached through the (blob_sha, repo_id, branch) index: joining on blob_sha alone used to scan the
# whole manifest table because its other indexes lead with repo_id
_CODE_FTS_HEAD = (
    "SELECT c.id, bf.repo_id, c.start_line, c.end_line, c.symbol, c.text, bf.path, sr.rpath, sr.weight "
    "FROM {table} JOIN chunks c ON c.id={table}.rowid "
    "JOIN branch_files bf ON bf.blob_sha=c.blob_sha "
    "JOIN scope_repos sr ON sr.repo_id=bf.repo_id AND sr.branch=bf.branch "
    "WHERE {table} MATCH ?")
_CODE_FTS_TAIL = " ORDER BY bm25({table}) LIMIT ?"
CODE_FTS_SQL = _CODE_FTS_HEAD + _CODE_FTS_TAIL
CODE_FTS_DOCS_SQL = _CODE_FTS_HEAD + " AND " + doc_predicate() + _CODE_FTS_TAIL


def _code_fts(db, table: str, q: str | None, hits: dict, docs: bool = False) -> list[Hit]:
    if not q:
        return []
    try:
        rows = db.execute((CODE_FTS_DOCS_SQL if docs else CODE_FTS_SQL).format(table=table), (q, TOP * 3)).fetchall()
    except sqlite3.OperationalError:
        return []
    out, seen = [], set()
    for r in rows:
        key = ("c", r["repo_id"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(hits.setdefault(key, Hit("code", key, weight=r["weight"], data=_code_row(r))))
        if len(out) >= TOP:
            break
    return out


def _mem_hit(row: sqlite3.Row, w: float, hits: dict, cfg: Config) -> Hit:
    key = ("m", row["n"])
    # curated notes written by Claude win ties against imported files
    iw = cfg.import_weight if row["source"] == "import" else 1.0
    return hits.setdefault(key, Hit("memory", key, weight=w, import_weight=iw, data={
        "id": row["id"], "scope": row["scope"], "date": (row["updated"] or row["created"] or "")[:10],
        "text": row["text"], "pinned": bool(row["pinned"]), "source": row["source"]}))


def _hidden_path(path: str, hidden) -> bool:
    return any(path == h or path.startswith(h + "/") for h in hidden)


def _visible_scopes(db, cfg, ctx, scope_mode, hidden=()) -> list[str] | None:
    """Memory scopes that can show up in this scope mode (None = every scope, i.e. no restriction)."""
    if scope_mode == "all" and not hidden:
        return None
    return [r[0] for r in db.execute("SELECT DISTINCT scope FROM memories WHERE superseded_by IS NULL")
            if _memory_weight(cfg, ctx, r[0], scope_mode) is not None and not _hidden_path(r[0], hidden)]


def _mem_vec(db, cfg, ctx, scope, qblob, hits, all_branches: bool = False, hidden=()) -> list[Hit]:
    # the KNN runs only over the scopes that are visible (scope is a partition key of memories_vec), so memories of
    # unrelated projects cannot fill the k nearest and hide the ones that matter
    scopes = _visible_scopes(db, cfg, ctx, scope, hidden)
    if scopes is None:
        rows = db.execute("SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k=?",
                          (qblob, OVERFETCH)).fetchall()
    elif not scopes:
        return []
    else:
        marks = ",".join("?" * len(scopes))
        rows = db.execute(f"SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k=? "
                          f"AND scope IN ({marks})", (qblob, OVERFETCH, *scopes)).fetchall()
    rows = sorted(rows, key=lambda r: (r[1], -r[0]))  # partitions come back grouped: by distance again (ties: newer first)
    out = []
    for rid, dist in rows:
        m = db.execute("SELECT * FROM memories WHERE n=? AND superseded_by IS NULL", (rid,)).fetchone()
        if not m or not (all_branches or branch_visible(ctx, m)):
            continue
        w = _memory_weight(cfg, ctx, m["scope"], scope)
        if w is None:
            continue
        h = _mem_hit(m, w, hits, cfg)
        h.similarity = 1.0 - dist
        out.append(h)
        if len(out) >= TOP:
            break
    return out


def _mem_fts(db, cfg, ctx, scope, q, hits, all_branches: bool = False, hidden=()) -> list[Hit]:
    if not q:
        return []
    try:
        rows = db.execute("SELECT m.* FROM memories_fts JOIN memories m ON m.n=memories_fts.rowid "
                          "WHERE memories_fts MATCH ? AND m.superseded_by IS NULL ORDER BY bm25(memories_fts) LIMIT 200",
                          (q,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for m in rows:
        w = _memory_weight(cfg, ctx, m["scope"], scope)
        if w is None or not (all_branches or branch_visible(ctx, m)) or (hidden and _hidden_path(m["scope"], hidden)):
            continue
        out.append(_mem_hit(m, w, hits, cfg))
        if len(out) >= TOP:
            break
    return out


# ------------------------------------------------------------------ formatting

def _head_lines(text: str, n: int = 3, width: int = 160) -> list[str]:
    return [ln.rstrip()[:width] for ln in text.splitlines() if ln.strip()][:n]


def _first_lines(text: str, n: int = 3, width: int = 160) -> str:
    return "\n".join("    " + ln for ln in _head_lines(text, n, width))


def layout_hits(hits: list[Hit], budget_tokens: int, full: bool = False, expand_top: int = 3,
                expand_lines: int = 25) -> list[dict]:
    """What the CLI prints, hit by hit: the hits that fit the budget, each with its printed `block`, the plain
    `shown` text (code lines / collapsed memory text) and whether it is `expanded`."""
    out: list[dict] = []
    used = 0
    ci = 0
    for h in hits:
        d = h.data
        if h.kind == "code":
            ci += 1
            head = f"[{ci}] {d['repo']} {d['path']}:{d['start']}-{d['end']}" + (f" {d['symbol']}" if d["symbol"] else "") \
                   + f" ({h.score:.3f})" \
                   + (f" · ticket {d['ticket']['id']} [{d['ticket']['status']}]" if d.get("ticket") else "")
            expanded = ci <= expand_top
            shown = d["text"] if full else "\n".join(_head_lines(d["text"], expand_lines if expanded else 3))
            body = d["text"] if full else _first_lines(d["text"], expand_lines if expanded else 3)
        else:
            txt = " ".join(d["text"].split())
            mi = len([x for x in out if x["hit"].kind == "memory"]) + 1
            shown = txt if full else (txt[:220] + ("..." if len(txt) > 220 else ""))
            head = f"[M{mi}] {d['scope']} {d['date']} {d['id']}: " + shown
            body, expanded = "", True
        block = head + ("\n" + body if body else "")
        cost = (len(block) + 1) // 4 + 1
        if not full and out and used + cost > budget_tokens:
            break
        out.append({"hit": h, "block": block, "shown": shown, "expanded": expanded})
        used += cost
    return out


def format_hits(hits: list[Hit], budget_tokens: int, full: bool = False, expand_top: int = 3,
                expand_lines: int = 25) -> str:
    """`[i] repo path:start-end symbol (score)` + 3 lines per code hit; the top `expand_top` code hits get
    `expand_lines` lines. `full` prints whole chunks (no budget)."""
    return "\n".join(x["block"] for x in layout_hits(hits, budget_tokens, full, expand_top, expand_lines))


COMPACT_MEMORY_CHARS = 100
IDENT_MIN_LEN = 3
IDENT_MAX_CANDIDATES = 5000   # FTS candidates scanned for the identifier total; more -> the number is a lower bound


def format_compact(hits: list[Hit], budget_tokens: int, cur_repo: str | None = None) -> str:
    """The agent-facing output: one line per hit, no snippets and no score (formatting only; the ranking is not
    touched). Code: `[repo ]path:start-end symbol[ · ticket ID [status]]`, the repo prefix only when the hits come
    from several repos or the repo is not the one of cwd. Memory: `[M] date id: first characters`."""
    repos = {h.data["repo"] for h in hits if h.kind == "code"}
    prefix = len(repos) > 1 or (cur_repo is not None and any(r != cur_repo for r in repos)) \
        or (cur_repo is None and bool(repos))
    lines: list[str] = []
    used = 0
    for h in hits:
        d = h.data
        if h.kind == "code":
            line = (d["repo"] + " " if prefix else "") + f"{d['path']}:{d['start']}-{d['end']}" \
                + (f" {d['symbol']}" if d["symbol"] else "") \
                + (f" · ticket {d['ticket']['id']} [{d['ticket']['status']}]" if d.get("ticket") else "")
        else:
            txt = " ".join(d["text"].split())
            line = f"[M] {d['date']} {d['id']}: " + txt[:COMPACT_MEMORY_CHARS] \
                + ("..." if len(txt) > COMPACT_MEMORY_CHARS else "")
        cost = (len(line) + 1) // 4 + 1
        if lines and used + cost > budget_tokens:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)


def ident_total(db, ctx: Ctx | None, scope: str, kind: str, query: str, hidden=None) -> dict | None:
    """For a one-word query: how many distinct lines (and files) in the scope contain it literally - the
    completeness grep gives and a top-k search does not. None when the query is not an identifier. Chunks overlap
    by `chunk.OVERLAP` lines, so lines are counted by `(repo, path, absolute line)`, not summed per chunk.
    `lines`/`files` are lower bounds when `capped`. `root`: directory of the scope (None for `scope=all`)."""
    q = query.strip()
    if len(query.split()) != 1 or len(q) < IDENT_MIN_LEN or kind == "memory":
        return None
    toks = re.findall(r"\w+", q, re.UNICODE)
    if not toks:
        return None
    uniq = list(dict.fromkeys(toks))
    # the last token is a prefix (`renderPdf` finds `renderPdfFile`); infix hits stay invisible to FTS
    fts = " AND ".join('"' + t.replace('"', '""') + '"' + ("*" if i == len(uniq) - 1 else "")
                       for i, t in enumerate(uniq))
    repos = _scope_repos(db, ctx, scope)
    if hidden is None:
        from myvec import crossproject
        hidden = crossproject.hidden_projects(db, ctx) if scope == "all" else set()
    if hidden:
        repos = [r for r in repos if not _hidden_path(r["project_path"], hidden)]
    branch_of = {r["id"]: effective_branch(db, r) for r in repos}
    repos = [r for r in repos if branch_of[r["id"]]]
    root = None
    if scope != "all" and ctx is not None:
        root = str(ctx.repo_dir if scope == "repo" and ctx.repo_dir else ctx.marker.root)
    if not repos:
        return {"lines": 0, "files": 0, "capped": False, "root": root}
    rpath = {r["id"]: r["path"] for r in repos}
    marks = ",".join("?" * len(repos))
    found: set[tuple] = set()
    accepted = scanned = 0
    capped = False
    try:
        cur = db.execute(
            "SELECT c.start_line, c.text, bf.path, bf.repo_id, bf.branch FROM chunks_fts "
            "JOIN chunks c ON c.id=chunks_fts.rowid JOIN branch_files bf ON bf.blob_sha=c.blob_sha "
            f"WHERE chunks_fts MATCH ? AND bf.repo_id IN ({marks})", (fts, *rpath))
        for r in cur:
            scanned += 1
            # branch/docs are filtered before the cap, so rows of other branches do not eat the limit
            if r["branch"] != branch_of[r["repo_id"]] or (kind == "docs" and not is_doc_path(r["path"])):
                if scanned > IDENT_MAX_CANDIDATES * 10:
                    capped = True
                    break
                continue
            accepted += 1
            if accepted > IDENT_MAX_CANDIDATES:
                capped = True
                break
            for i, ln in enumerate(r["text"].splitlines()):
                if q in ln:
                    found.add((r["repo_id"], r["path"], r["start_line"] + i))
    except sqlite3.OperationalError:
        return None
    return {"lines": len(found), "files": len({(a, b) for a, b, _ in found}), "capped": capped, "root": root}


def format_ident_total(total: dict, query: str, cwd: str) -> str:
    """`total in index: N lines in M files · all: rg -n -F -- 'q' root`. No `rg` for `scope=all` (no root) and for
    a query with a hard secret (the count stays)."""
    import os
    import shlex

    from myvec import filters

    n = f"{'≥' if total['capped'] else ''}{total['lines']}"
    out = f"total in index: {n} lines in {total['files']} files (word/prefix matches)"
    if total["root"] is not None and not filters.has_hard_secret(query):
        root = os.path.relpath(total["root"], cwd) if os.path.isabs(total["root"]) else total["root"]
        out += f" · all: rg -n -F -- {shlex.quote(query.strip())} {shlex.quote(root)}"
    return out


def to_json(hits: list[Hit], breakdown: bool = False) -> list[dict]:
    res = []
    for h in hits:
        d = dict(h.data)
        d.update({"kind": h.kind, "score": round(h.score, 6)})
        if breakdown:
            d["breakdown"] = {
                "vector_rank": h.ranks.get("code_vec", h.ranks.get("mem_vec")),
                "fts_rank": h.ranks.get("code_fts", h.ranks.get("mem_fts")),
                "route": h.route,
                "similarity": round(h.similarity, 6) if h.similarity is not None else None,
                "rrf": round(h.rrf, 6), "rrf_k": RRF_K, "rrf_max": round(RRF_MAX, 6),
                "weight": h.weight, "import_weight": h.import_weight, "weight_eff": round(h.weight_eff, 6),
                "path_weight": h.path_weight,
            }
        res.append(d)
    return res
