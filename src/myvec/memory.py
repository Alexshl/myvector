"""Two-level memory: Obsidian-compatible vault (source of truth) + index in SQLite."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from myvec import chunk as chunkmod
from myvec import config, filters, gitutil, vaultgit
from myvec import db as dbmod
from myvec.embed import Embedder

DEDUP_SIM = 0.92
REPLACE_SIM = 0.98
RELATED_SIM = 0.80
MAX_EMBED_CHARS = 6000


@dataclass
class Memory:
    id: str
    scope: str
    body: str
    branch: str | None = None
    commit: str | None = None
    created: str = ""
    updated: str = ""
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    supersedes: list[str] = field(default_factory=list)
    file: Path | None = None
    origin_branch: str | None = None   # where it was written (metadata only; `branch` is the visibility filter)
    origin_repo: str | None = None
    base_commit: str | None = None     # merge-base with the default branch when `remember --branch` ran
    masked: int = 0                    # number of secret values masked on save (not persisted)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ vault files

def _fmt_list(xs: list[str]) -> str:
    return "[" + ", ".join(xs) + "]"


def render(m: Memory) -> str:
    fm = [f"id: {m.id}", f"scope: {m.scope}"]
    if m.branch:
        fm.append(f"branch: {m.branch}")
    if m.origin_branch:
        fm.append(f"origin_branch: {m.origin_branch}")
    if m.origin_repo:
        fm.append(f"origin_repo: {m.origin_repo}")
    if m.commit:
        fm.append(f"commit: {m.commit}")
    if m.base_commit:
        fm.append(f"base_commit: {m.base_commit}")
    fm += [f"created: {m.created}", f"updated: {m.updated}", f"tags: {_fmt_list(m.tags)}",
           f"pinned: {'true' if m.pinned else 'false'}", f"supersedes: {_fmt_list(m.supersedes)}"]
    return "---\n" + "\n".join(fm) + "\n---\n" + m.body.strip() + "\n"


def _parse_list(v: str) -> list[str]:
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip().strip("'\"") for x in v.split(",") if x.strip()]


def parse(text: str, file: Path | None = None) -> Memory | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    meta: dict[str, str] = {}
    for ln in text[3:end].splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            meta[k.strip()] = v.strip()
    body = text[end + 4:].lstrip("\n").strip()
    if "id" not in meta or "scope" not in meta:
        return None
    return Memory(
        id=meta["id"], scope=meta["scope"], body=body, branch=meta.get("branch") or None,
        commit=meta.get("commit") or None, created=meta.get("created", ""), updated=meta.get("updated", ""),
        tags=_parse_list(meta.get("tags", "")), pinned=meta.get("pinned", "false").lower() == "true",
        supersedes=_parse_list(meta.get("supersedes", "")), file=file,
        origin_branch=meta.get("origin_branch") or meta.get("branch") or None,
        origin_repo=meta.get("origin_repo") or None, base_commit=meta.get("base_commit") or None,
    )


def slugify(text: str, words: int = 6, maxlen: int = 40) -> str:
    toks = re.findall(r"\w+", text.lower(), re.UNICODE)[:words]
    return ("-".join(toks))[:maxlen].strip("-") or "note"


def inside(path: Path, base: Path) -> bool:
    """True if `path` (symlinks and `..` resolved) is `base` or lies below it."""
    try:
        rp, rb = Path(path).resolve(), Path(base).resolve()
    except (OSError, RuntimeError):
        return False
    return rp == rb or rb in rp.parents


def scope_dir(scope: str) -> Path:
    """Vault directory of a memory scope. A scope that would leave `vault/projects` (`..`, absolute) is refused."""
    v = config.vault_dir()
    if scope == "global":
        return v / "global"
    base = v / "projects"
    d = base / scope
    if not scope or Path(scope).is_absolute() or not inside(d, base) or d.resolve() == base.resolve():
        raise ValueError(f"unsafe memory scope {scope!r}")
    return d


def filename_for(m: Memory) -> str:
    return f"{m.created[:10]}-{slugify(m.body)}-{m.id}.md"


def write_file(m: Memory) -> Path:
    path = m.file or (scope_dir(m.scope) / filename_for(m))
    if not inside(path, config.vault_dir()):
        raise ValueError(f"refusing to write outside the vault: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(m), encoding="utf-8")
    os.replace(tmp, path)
    m.file = path
    return path


def vault_files() -> list[Path]:
    v = config.vault_dir()
    out = []
    for sub in ("global", "projects"):
        d = v / sub
        if d.exists():
            out += sorted(d.rglob("*.md"))
    return out


# ------------------------------------------------------------------ DB helpers

def _row_to_memory(r: sqlite3.Row) -> Memory:
    return Memory(id=r["id"], scope=r["scope"], body=r["text"], branch=r["branch"], commit=r["commit"],
                  created=r["created"] or "", updated=r["updated"] or "",
                  tags=[t for t in (r["tags"] or "").split(",") if t], pinned=bool(r["pinned"]),
                  file=Path(r["vault_file"]) if r["vault_file"] else None,
                  origin_branch=r["origin_branch"], origin_repo=r["origin_repo"], base_commit=r["base_commit"])


def find(db: sqlite3.Connection, ident: str) -> sqlite3.Row | None:
    rows = db.execute("SELECT * FROM memories WHERE id=?", (ident,)).fetchall()
    if not rows:
        rows = db.execute("SELECT * FROM memories WHERE id LIKE ? ESCAPE '\\'",
                          (ident.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
    return rows[0] if len(rows) == 1 else None


def _index(db: sqlite3.Connection, m: Memory, vec, source: str = "vault", vault_file: str | None = None) -> int:
    """Insert or replace the DB row (+vec) of memory m. Returns n."""
    row = db.execute("SELECT n FROM memories WHERE id=? AND source=?", (m.id, source)).fetchone()
    vf = vault_file or (str(m.file) if m.file else None)
    if row:
        n = row[0]
        db.execute('UPDATE memories SET scope=?, branch=?, "commit"=?, vault_file=?, text=?, created=?, updated=?, '
                   "pinned=?, tags=?, origin_branch=?, origin_repo=?, base_commit=? WHERE n=?",
                   (m.scope, m.branch, m.commit, vf, m.body, m.created, m.updated, int(m.pinned), ",".join(m.tags),
                    m.origin_branch, m.origin_repo, m.base_commit, n))
        db.execute("DELETE FROM memories_vec WHERE rowid=?", (n,))
    else:
        n = db.execute(
            'INSERT INTO memories(id, scope, branch, "commit", vault_file, text, created, updated, pinned, source, tags, '
            "origin_branch, origin_repo, base_commit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.id, m.scope, m.branch, m.commit, vf, m.body, m.created, m.updated, int(m.pinned), source,
             ",".join(m.tags), m.origin_branch, m.origin_repo, m.base_commit)).lastrowid
    if vec is not None:
        db.execute("INSERT INTO memories_vec(rowid, scope, embedding) VALUES(?,?,?)", (n, m.scope, dbmod.pack(vec)))
    return n


def _embed_body(embedder: Embedder, body: str) -> list[float]:
    return embedder.embed_docs([body[:MAX_EMBED_CHARS]])[0]


def nearest_in_scope(db: sqlite3.Connection, vec: list[float], scope: str, k: int = 50, *,
                     branch: str | None = None, origin_repo: str | None = None) -> list[tuple[sqlite3.Row, float]]:
    """Live vault memories of exactly `scope`, nearest first, as dedup/related candidates for a note written with
    this `branch` filter: only memories with the same `branch` value (NULL with NULL) that are visible there. The KNN
    runs inside the scope's partition, so memories of other scopes cannot crowd the result."""
    rows = db.execute("SELECT rowid, distance FROM memories_vec WHERE embedding MATCH ? AND k=? AND scope=?",
                      (dbmod.pack(vec), k, scope)).fetchall()
    out = []
    for r in rows:
        m = db.execute("SELECT * FROM memories WHERE n=? AND scope=? AND superseded_by IS NULL AND source='vault'",
                       (r[0], scope)).fetchone()
        if not m or m["branch"] != branch:
            continue
        if branch is not None and m["origin_repo"] and origin_repo and m["origin_repo"] != origin_repo:
            continue  # a branch note is only visible in the repo it was written in
        out.append((m, 1.0 - r[1]))
    out.sort(key=lambda t: -t[1])
    return out


# ------------------------------------------------------------------ operations

def remember(db: sqlite3.Connection, embedder: Embedder, text: str, scope: str, *, branch: str | None = None,
             commit: str | None = None, tags: list[str] | None = None, pin: bool = False,
             supersedes: str | None = None, origin_branch: str | None = None,
             origin_repo: str | None = None, base_commit: str | None = None) -> tuple[Memory, str]:
    """Returns (memory, action) with action in {'created', 'updated'}.

    `branch` is the visibility filter (set only by `remember --branch`); `origin_branch`/`origin_repo`/`commit`
    are plain metadata, `base_commit` is the merge-base with the default branch at write time (lifecycle proof).
    Literal secret values are masked; private keys / known token formats are refused. A similar note is merged
    only if it has the same `branch` filter (a project note is never turned into a branch note or vice versa).
    If the database write fails, the vault file is rolled back (no orphans).
    """
    text = text.strip()
    if not text:
        raise ValueError("empty memory text")
    if filters.has_hard_secret(text):
        raise ValueError("text contains a secret (private key / known token format); refusing to store it")
    text, nmasked = filters.mask_secrets(text)
    tags = tags or []
    scope_dir(scope)  # refuses scopes that would leave the vault
    vec = _embed_body(embedder, text)
    near = nearest_in_scope(db, vec, scope, branch=branch, origin_repo=origin_repo)
    now = now_iso()
    if near and near[0][1] >= DEDUP_SIM and supersedes is None:
        row, sim = near[0]
        m = _row_to_memory(row)
        m.body = text if sim >= REPLACE_SIM else (m.body.rstrip() + "\n\n" + text if text not in m.body else m.body)
        m.updated = now
        m.tags = sorted(set(m.tags) | set(tags))
        m.pinned = m.pinned or pin
        if commit:
            m.commit = commit
        if base_commit:
            m.base_commit = base_commit
        m.origin_branch = origin_branch or m.origin_branch
        m.origin_repo = origin_repo or m.origin_repo
        if m.file is None or not m.file.exists():
            m.file = None
        previous = m.file.read_bytes() if m.file is not None else None
        path = write_file(m)
        vec = _embed_body(embedder, m.body)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                _index(db, m, vec, vault_file=str(m.file))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        except Exception:
            _restore_file(path, previous)
            raise
        m.masked = nmasked
        vaultgit.autocommit(f"myvec: remember {m.id} (updated) {slugify(m.body, 5)}")
        return m, "updated"

    m = Memory(id=uuid.uuid4().hex[:8], scope=scope, body=text, branch=branch, commit=commit,
               created=now, updated=now, tags=tags, pinned=pin, origin_branch=origin_branch,
               origin_repo=origin_repo, base_commit=base_commit, masked=nmasked)
    links = []
    if supersedes:
        old = find(db, supersedes)
        if old is None:
            raise ValueError(f"memory {supersedes!r} not found")
        m.supersedes = [old["id"]]
        if old["vault_file"]:
            links.append("Supersedes: [[" + Path(old["vault_file"]).stem + "]]")
    elif near and near[0][1] >= RELATED_SIM and near[0][0]["vault_file"]:
        links.append("Related: [[" + Path(near[0][0]["vault_file"]).stem + "]]")
    if links:
        m.body = m.body + "\n\n" + "\n".join(links)
    path = write_file(m)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            _index(db, m, vec)
            if supersedes and m.supersedes:
                _archive_row(db, find_row_by_id(db, m.supersedes[0]), superseded_by=m.id)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    except Exception:
        _restore_file(path, None)
        raise
    vaultgit.autocommit(f"myvec: remember {m.id} {slugify(m.body, 5)}")
    return m, "created"


def _restore_file(path: Path, previous: bytes | None) -> None:
    """Undo write_file after a failed DB write: put the old content back, or remove the new file."""
    try:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
    except OSError:
        pass


def find_row_by_id(db, mid: str) -> sqlite3.Row:
    return db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()


def _archive_row(db: sqlite3.Connection, row: sqlite3.Row, superseded_by: str) -> str | None:
    """Mark superseded, drop from vec, move vault file to _archive. Caller manages the transaction."""
    new_file = None
    vault = config.vault_dir()
    if row["source"] == "vault" and row["vault_file"] and Path(row["vault_file"]).exists() \
            and inside(Path(row["vault_file"]), vault):
        src = Path(row["vault_file"])
        try:
            rel = src.relative_to(vault)
        except ValueError:
            rel = Path(src.name)
        dst = vault / "_archive" / rel
        if not inside(dst, vault / "_archive"):
            dst = vault / "_archive" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        new_file = str(dst)
    db.execute("UPDATE memories SET superseded_by=?, vault_file=coalesce(?, vault_file) WHERE n=?",
               (superseded_by, new_file, row["n"]))
    db.execute("DELETE FROM memories_vec WHERE rowid=?", (row["n"],))
    return new_file


def forget(db: sqlite3.Connection, ident: str) -> Memory:
    row = find(db, ident)
    if row is None:
        raise ValueError(f"memory {ident!r} not found (or ambiguous prefix)")
    if row["superseded_by"]:
        raise ValueError(f"memory {row['id']} is already archived")
    db.execute("BEGIN IMMEDIATE")
    try:
        _archive_row(db, row, superseded_by="archived")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    vaultgit.autocommit(f"myvec: forget {row['id']}")
    return _row_to_memory(row)


def pin(db: sqlite3.Connection, ident: str, on: bool = True) -> Memory:
    row = find(db, ident)
    if row is None:
        raise ValueError(f"memory {ident!r} not found (or ambiguous prefix)")
    m = _row_to_memory(row)
    m.pinned = on
    if row["source"] == "vault" and m.file and m.file.exists():
        parsed = parse(m.file.read_text(encoding="utf-8"), m.file)
        if parsed:
            parsed.pinned = on
            parsed.file = m.file
            write_file(parsed)
    db.execute("UPDATE memories SET pinned=? WHERE n=?", (int(on), row["n"]))
    vaultgit.autocommit(f"myvec: {'pin' if on else 'unpin'} {row['id']}")
    return m


def promote(db: sqlite3.Connection, ident: str) -> Memory:
    """Move a memory to the global scope: vault file -> vault/global/, DB row updated, branch filter dropped."""
    row = find(db, ident)
    if row is None:
        raise ValueError(f"memory {ident!r} not found (or ambiguous prefix)")
    if row["superseded_by"]:
        raise ValueError(f"memory {row['id']} is archived")
    if row["source"] != "vault":
        raise ValueError(f"memory {row['id']} is imported (read-only) and cannot be promoted")
    if row["scope"] == "global":
        raise ValueError(f"memory {row['id']} is already global")
    m = _row_to_memory(row)
    src = m.file
    if src is not None and src.exists():
        m = parse(src.read_text(encoding="utf-8"), src) or m
    m.scope, m.branch = "global", None
    m.origin_repo = m.origin_repo or row["scope"]
    m.file = scope_dir("global") / (src.name if src else filename_for(m))
    write_file(m)
    if src is not None and src.exists() and src != m.file and inside(src, config.vault_dir()):
        src.unlink()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute('UPDATE memories SET scope=\'global\', branch=NULL, origin_repo=?, vault_file=? WHERE n=?',
                   (m.origin_repo, str(m.file), row["n"]))
        e = db.execute("SELECT embedding FROM memories_vec WHERE rowid=?", (row["n"],)).fetchone()
        if e is not None:  # the scope is a partition key: move the vector instead of updating it
            db.execute("DELETE FROM memories_vec WHERE rowid=?", (row["n"],))
            db.execute("INSERT INTO memories_vec(rowid, scope, embedding) VALUES(?,?,?)", (row["n"], "global", e[0]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    vaultgit.autocommit(f"myvec: promote {row['id']} to global (from {row['scope']})")
    return m


# ------------------------------------------------------------------ branch lifecycle

def _legacy_branch_state(d: str, branch: str, default: str, commit: str | None) -> str:
    """Rows written before `base_commit` existed: all we have is the head commit at write time."""
    base = gitutil.rev(d, commit) if commit else None
    if gitutil.branch_exists(d, branch):
        tip = gitutil.rev(d, branch)
        # a branch whose tip is still where the memory was written has no work of its own yet: not "merged"
        if tip and gitutil.is_ancestor(d, branch, default):
            if tip != (base or gitutil.rev(d, default)) or gitutil.merged_by_merge_commit(d, tip, default):
                return "merged"
        return "alive"
    if base and gitutil.is_ancestor(d, base, default):
        return "merged"  # branch deleted after its work reached the default branch
    return "gone"


def _branch_state(d: str, branch: str, default: str, commit: str | None, base_commit: str | None = None,
                  tip_seen: str | None = None) -> tuple[str, str | None]:
    """('alive' | 'merged' | 'gone', tip to remember) for a memory's branch relative to the default branch.

    "Merged" needs proof that the branch had commits of its own (outside the default branch) and that they are now
    contained in it: the note was written with HEAD beyond the merge-base (`commit != base_commit`), or a lifecycle
    pass saw the branch ahead (`tip_seen`), or the tip is a parent of a merge commit. A branch with no work of its own
    that was merely fast-forwarded from the default branch, or deleted without its commits ever reaching it, is not
    merged (deleted -> archived).
    """
    if branch == default:
        return "merged", None
    if branch.startswith("HEAD@"):
        return "alive", None  # detached HEAD: nothing to track
    if not base_commit and not tip_seen:
        return _legacy_branch_state(d, branch, default, commit), None
    c = gitutil.rev(d, commit) if commit else None
    b = gitutil.rev(d, base_commit) if base_commit else None
    seen = gitutil.rev(d, tip_seen) if tip_seen else None
    work = [x for x in (c if c and c != b else None, seen if seen and seen != b else None) if x]
    if gitutil.branch_exists(d, branch):
        tip = gitutil.rev(d, branch)
        if not tip:
            return "alive", None
        if gitutil.is_ancestor(d, tip, default):
            if work or gitutil.merged_by_merge_commit(d, tip, default):
                return "merged", None
            return "alive", None
        return "alive", tip  # ahead of the default branch: remember it as proof of own work
    if any(gitutil.is_ancestor(d, w, default) for w in work):
        return "merged", None  # branch deleted after its work reached the default branch
    return "gone", None


def lifecycle(db: sqlite3.Connection) -> dict[str, list[str]]:
    """Branch-scoped memories: branch merged into main/master/dev -> project-wide; deleted unmerged -> archived."""
    res: dict[str, list[str]] = {"merged": [], "archived": []}
    rows = db.execute("SELECT * FROM memories WHERE branch IS NOT NULL AND superseded_by IS NULL "
                      "AND source='vault'").fetchall()
    if not rows:
        return res
    repo_dirs = {r["path"]: r["dir"] for r in db.execute("SELECT path, dir FROM repos")}
    defaults: dict[str, str | None] = {}
    for row in rows:
        rpath = row["origin_repo"] or (row["scope"] if row["scope"] in repo_dirs else None)
        d = repo_dirs.get(rpath or "")
        if not d or not Path(d).is_dir() or not gitutil.is_repo(d):
            continue
        try:
            if d not in defaults:
                defaults[d] = gitutil.default_branch(d)
            default = defaults[d]
            if not default:
                continue
            state, seen = _branch_state(d, row["branch"], default, row["commit"], row["base_commit"], row["tip_seen"])
        except Exception:  # noqa: BLE001 - git trouble must not break sync
            continue
        if seen and seen != row["tip_seen"]:
            db.execute("UPDATE memories SET tip_seen=? WHERE n=?", (seen, row["n"]))
        if state == "merged":
            _clear_branch(db, row)
            res["merged"].append(row["id"])
        elif state == "gone":
            db.execute("BEGIN IMMEDIATE")
            try:
                _archive_row(db, row, superseded_by="archived")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            res["archived"].append(row["id"])
    if res["merged"] or res["archived"]:
        vaultgit.autocommit("myvec: lifecycle " + ", ".join(
            [f"{i} -> project" for i in res["merged"]] + [f"{i} archived" for i in res["archived"]]))
    return res


def _clear_branch(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    f = Path(row["vault_file"]) if row["vault_file"] else None
    if f is not None and f.exists():
        parsed = parse(f.read_text(encoding="utf-8"), f)
        if parsed:
            parsed.branch = None
            parsed.origin_branch = parsed.origin_branch or row["branch"]
            parsed.file = f
            write_file(parsed)
    db.execute("UPDATE memories SET branch=NULL, origin_branch=coalesce(origin_branch, ?) WHERE n=?",
               (row["branch"], row["n"]))


# ------------------------------------------------------------------ rebuild from vault

def rebuild_from_vault(db: sqlite3.Connection, embedder: Embedder, log=lambda s: None) -> int:
    """Drop vault-sourced memories from the index and re-create them from vault/*.md."""
    mems: list[Memory] = []
    for f in vault_files():
        m = parse(f.read_text(encoding="utf-8"), f)
        if m:
            mems.append(m)
    db.execute("BEGIN IMMEDIATE")
    for (n,) in db.execute("SELECT n FROM memories WHERE source='vault'").fetchall():
        db.execute("DELETE FROM memories_vec WHERE rowid=?", (n,))
    db.execute("DELETE FROM memories WHERE source='vault'")
    db.execute("COMMIT")
    for i in range(0, len(mems), 32):
        group = mems[i:i + 32]
        vecs = embedder.embed_docs([m.body[:MAX_EMBED_CHARS] for m in group])
        db.execute("BEGIN IMMEDIATE")
        for m, v in zip(group, vecs):
            _index(db, m, v)
        db.execute("COMMIT")
    # re-link archived/superseded state from the vault itself
    for m in mems:
        for old in m.supersedes:
            r = find_row_by_id(db, old)
            if r is not None and not r["superseded_by"]:
                db.execute("BEGIN IMMEDIATE")
                _archive_row(db, r, superseded_by=m.id)
                db.execute("COMMIT")
    return len(mems)


# ------------------------------------------------------------------ import (read-only, indexed in place)

def _encode_dir(p: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def import_sources(db: sqlite3.Connection, claude_dir: Path | None = None) -> list[tuple[Path, str]]:
    """[(file, scope)] of importable markdown files."""
    claude_dir = claude_dir or Path(os.environ.get("MYVEC_CLAUDE_DIR") or Path.home() / ".claude")
    dirs: dict[str, str] = {}  # dir -> scope(repo/project path)
    for r in db.execute("SELECT path, dir FROM repos"):
        dirs[r["dir"]] = r["path"]
    for r in db.execute("SELECT path, root FROM projects"):
        dirs[r["root"]] = r["path"]
    out: list[tuple[Path, str]] = []
    enc = {_encode_dir(d): s for d, s in dirs.items()}
    pdir = claude_dir / "projects"
    if pdir.exists():
        for sub in sorted(pdir.iterdir()):
            scope = enc.get(sub.name)
            if scope and (sub / "memory").is_dir():
                out += [(f, scope) for f in sorted((sub / "memory").glob("*.md"))]
    for d, scope in sorted(dirs.items()):
        for rel in ("CLAUDE.md", "graphify-out/GRAPH_REPORT.md"):
            f = Path(d) / rel
            if f.is_file():
                out.append((f, scope))
    return out


# graphify reports: thousands of "Community N" node lists would flood memory search; keep the overview sections only
_GRAPH_REPORT_KEEP = ("Summary", "God Nodes", "Surprising Connections", "Import Cycles", "Knowledge Gaps")


def _graph_report_overview(text: str) -> str:
    """Blank out every `## ` section not in _GRAPH_REPORT_KEEP (line numbers stay, so import ids stay stable)."""
    out, keep = [], True
    for line in text.split("\n"):
        if line.startswith("## "):
            keep = line[3:].startswith(_GRAPH_REPORT_KEEP)
        out.append(line if keep else "")
    return "\n".join(out)


_FM_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Claude auto-memory notes start with a YAML-ish `---` block (name, description, metadata). Returns
    (first line to index = description, else name; None if there is none, the text without the block). Text without
    a closed leading block is returned unchanged."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, text
    vals: dict[str, str] = {}
    i = 1
    while i < end:
        m = _FM_KEY.match(lines[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):   # block scalar: the indented lines that follow
            parts = []
            while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                parts.append(lines[i].strip())
                i += 1
            val = " ".join(p for p in parts if p)
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        vals[key] = " ".join(val.split())
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return (vals.get("description") or vals.get("name") or None), body


def import_all(db: sqlite3.Connection, embedder: Embedder, claude_dir: Path | None = None) -> dict[str, int]:
    sources = import_sources(db, claude_dir)
    res = {"files": 0, "memories": 0, "skipped_secrets": 0, "removed": 0}
    live = {str(f) for f, _ in sources}
    # forget imports whose file vanished
    for (vf,) in db.execute("SELECT DISTINCT vault_file FROM memories WHERE source='import'").fetchall():
        if vf not in live:
            _drop_import(db, vf)
            res["removed"] += 1
    for f, scope in sources:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(f.stat().st_mtime))
        pieces = []
        fm_head, text = split_frontmatter(text)
        if fm_head is not None:
            fm_head = filters.mask_secrets(fm_head)[0]
        key_ranges = filters.private_key_ranges(text)
        if f.name == "GRAPH_REPORT.md":
            text = _graph_report_overview(text)
        chunks = chunkmod.chunk_markdown(text)
        if fm_head and not chunks:
            chunks = [chunkmod.Chunk(start_line=1, end_line=1, symbol=None, text="")]
        for c in chunks:
            if filters.has_hard_secret(c.text) or filters.overlaps_ranges(c.start_line, c.end_line, key_ranges):
                res["skipped_secrets"] += 1
                continue
            c.text, _n = filters.mask_secrets(c.text)
            head = fm_head or filters.mask_secrets(f"{f.name}" + (f" > {c.symbol}" if c.symbol else ""))[0]
            pieces.append(Memory(
                id="imp-" + hashlib.sha1(f"{f}:{c.start_line}".encode()).hexdigest()[:8], scope=scope,
                body=(f"{head}\n{c.text}" if c.text else head)[:3000], created=mtime, updated=mtime, tags=["import"],
                file=f))
        vecs = embedder.embed_docs([m.body for m in pieces])
        db.execute("BEGIN IMMEDIATE")
        try:
            _drop_import(db, str(f), commit=False)
            for m, v in zip(pieces, vecs):
                _index(db, m, v, source="import", vault_file=str(f))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        res["files"] += 1
        res["memories"] += len(pieces)
    return res


def _drop_import(db: sqlite3.Connection, vault_file: str, commit: bool = True) -> None:
    if commit:
        db.execute("BEGIN IMMEDIATE")
    for (n,) in db.execute("SELECT n FROM memories WHERE source='import' AND vault_file=?", (vault_file,)).fetchall():
        db.execute("DELETE FROM memories_vec WHERE rowid=?", (n,))
    db.execute("DELETE FROM memories WHERE source='import' AND vault_file=?", (vault_file,))
    if commit:
        db.execute("COMMIT")


# ------------------------------------------------------------------ topics (offline clustering)

_STOP = set("the a an and or of to in is for on with that this it as be are was at by from not you your i we "
            "и в на с по для не что это как к из но а о у the".split())


def topics(db: sqlite3.Connection) -> list[Path]:
    import numpy as np

    out: list[Path] = []
    scopes = [r[0] for r in db.execute("SELECT DISTINCT scope FROM memories WHERE superseded_by IS NULL AND source='vault'")]
    for scope in scopes:
        rows = db.execute("SELECT n, id, text, vault_file FROM memories WHERE scope=? AND superseded_by IS NULL "
                          "AND source='vault'", (scope,)).fetchall()
        if len(rows) < 2:
            continue
        vecs = []
        for r in rows:
            e = db.execute("SELECT embedding FROM memories_vec WHERE rowid=?", (r["n"],)).fetchone()
            vecs.append(np.frombuffer(e[0], dtype=np.float32) if e else np.zeros(1, dtype=np.float32))
        dim = max(v.shape[0] for v in vecs)
        X = np.stack([v if v.shape[0] == dim else np.zeros(dim, dtype=np.float32) for v in vecs])
        k = max(1, min(8, int(round((len(rows) / 2) ** 0.5))))
        labels = _kmeans(X, k)
        lines = [f"# Topics: {scope}", "", f"_Generated by `myvec topics` on {now_iso()}_", ""]
        for c in range(k):
            members = [rows[i] for i in range(len(rows)) if labels[i] == c]
            if not members:
                continue
            words: dict[str, int] = {}
            for r in members:
                for w in re.findall(r"\w{4,}", r["text"].lower()):
                    if w not in _STOP:
                        words[w] = words.get(w, 0) + 1
            title = ", ".join(w for w, _ in sorted(words.items(), key=lambda kv: -kv[1])[:3]) or f"topic {c + 1}"
            lines += [f"## {title}", ""]
            for r in members:
                stem = Path(r["vault_file"]).stem if r["vault_file"] else r["id"]
                first = r["text"].strip().splitlines()[0][:100]
                lines.append(f"- [[{stem}]] - {first}")
            lines.append("")
        topics_dir = config.vault_dir() / "_topics"
        dst = topics_dir / (("global" if scope == "global" else scope) + ".md")
        if not inside(dst, topics_dir) or Path(scope).is_absolute():
            continue  # an unsafe scope name never writes outside _topics
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("\n".join(lines), encoding="utf-8")
        out.append(dst)
    return out


def _kmeans(X, k: int, iters: int = 25):
    import numpy as np

    rng = np.random.default_rng(0)
    n = X.shape[0]
    # k-means++ style deterministic init
    centers = [X[rng.integers(n)]]
    for _ in range(1, k):
        d = np.min([((X - c) ** 2).sum(1) for c in centers], axis=0)
        p = d / d.sum() if d.sum() > 0 else np.full(n, 1.0 / n)
        centers.append(X[rng.choice(n, p=p)])
    C = np.stack(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        dist = ((X[:, None, :] - C[None, :, :]) ** 2).sum(2)
        new = dist.argmin(1)
        if (new == labels).all() and _ > 0:
            break
        labels = new
        for j in range(k):
            sel = X[labels == j]
            if len(sel):
                C[j] = sel.mean(0)
    return labels
