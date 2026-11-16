"""`myvec scaffold --update`: re-render the process with the current templates and merge it 3-way into the project
(ADR-035). ours = the file on disk, base = `files[rel].base` of the project state (ADR-028), theirs = the new render.
No typer; the transactional write is `scaffold.apply` (ADR-023), unchanged."""
from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from myvec import filters
from myvec import ticketops as ops
from myvec import scaffold as S
from myvec.scaffold import ScaffoldError, state as st

MERGE_TIMEOUT = 30
_WRITE_ACTS = ("create", "update", "merge", "overwrite")


def merge3(ours: str, base: str, theirs: str) -> tuple[str, bool]:
    """3-way merge through `git merge-file -p` with an isolated environment: (text, clean). A conflicting result
    keeps the markers in `text` - callers never write it."""
    git = shutil.which("git")
    if not git:
        raise ScaffoldError("`git` was not found: the 3-way merge needs it (install git and repeat)")
    tmp = tempfile.mkdtemp(prefix="myvec-merge-")
    try:
        os.chmod(tmp, 0o700)
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(tmp, name)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf8"))
            paths.append(p)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and k not in ("HOME", "XDG_CONFIG_HOME")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", HOME=tmp, XDG_CONFIG_HOME=tmp,
                   GIT_CEILING_DIRECTORIES=os.path.dirname(tmp))
        try:
            r = subprocess.run([git, "merge-file", "-p", "-q", "-L", "ours", "-L", "base", "-L", "template", *paths],
                               capture_output=True, env=env, timeout=MERGE_TIMEOUT, cwd=tmp)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ScaffoldError(f"git merge-file failed: {type(e).__name__}") from None
        if r.returncode == 0 or 1 <= r.returncode <= 127:
            return r.stdout.decode("utf8", "replace"), r.returncode == 0
        raise ScaffoldError(f"git merge-file failed (exit {r.returncode})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class UpdatePlan:
    rows: list[tuple[str, str]]          # for display, with the owner's choices applied
    apply_plan: S.Plan                   # what `scaffold.apply` gets (conflict rows carry the choices)
    texts: dict[str, str]                # the `rendered` argument of `apply`: final text per path (+ the state)
    conflicts: list[str]
    write: list[str]
    diffs: dict[str, dict] = field(default_factory=dict)
    from_version: int = 0
    to_version: int = S.PROCESS_VERSION
    overwrite: set[str] = field(default_factory=set)
    keep: set[str] = field(default_factory=set)
    a: S.Answers | None = None


def _regen_block(disk: str, merged: str) -> str:
    """Puts the generated block of the disk text (INDEX.md, group tables) back into the merged text."""
    s, e = disk.find(ops.START), disk.find(ops.END)
    if s == -1 or e <= s:
        return merged
    ms, me = merged.find(ops.START), merged.find(ops.END)
    if ms == -1 or me <= ms:
        return merged
    return merged[:ms] + disk[s:e + len(ops.END)] + merged[me + len(ops.END):]


def _snap(p: Path) -> tuple[str | None, str]:
    """(text, fingerprint) of one path from a single read: the fingerprint is the hash of the very bytes that are
    merged, so a file edited later cannot pass the `apply` check with a stale merge (ADR-023, ADR-035). The text is
    None for a missing/non-regular/undecodable path."""
    if p.is_symlink() or (p.exists() and not p.is_file()):
        return None, S._NONFILE
    if not p.exists():
        return None, S._ABSENT
    try:
        data = p.read_bytes()
    except OSError:
        return None, S._NONFILE
    fp = hashlib.sha256(data).hexdigest()
    try:
        return data.decode("utf8"), fp
    except UnicodeDecodeError:
        return None, fp


def _classify(root: Path, rel: str, entry: dict | None, new: str, snap: tuple[str | None, str]) -> tuple[str, str]:
    """(action, final text) for one rendered path. `entry`: the state entry or None (a file the state lacks);
    `snap`: `_snap` of the path (the one read of its bytes)."""
    p = S._safe(root, rel)
    disk, fp = snap
    is_file = fp not in (S._ABSENT, S._NONFILE)
    if entry is None:
        if rel.startswith(S.TICKETS_PREFIX):
            return "skip", ""
        if not p.exists():
            twin = p.parent.is_dir() and any(e.name.lower() == p.name.lower() for e in p.parent.iterdir())
            return ("conflict", "") if twin else ("create", new)
        if disk is None:
            return "conflict", ""
        return ("same", "") if S._norm(rel, disk) == S._norm(rel, new) else ("conflict", "")
    if entry["mode"] == "keep" and (not is_file or disk is None):
        return "keep", ""                          # a kept file the owner never created / not text: nothing to merge
    if not is_file:
        return "conflict", ""                      # vanished (or not a regular file): the owner decides
    if disk is None:
        return "conflict", ""
    o, b, t = (S._norm(rel, x) for x in (disk, entry["base"], new))
    if o == t:
        return "same", ""
    if t == b:
        return "unchanged", ""
    if o == b:
        return "update", _regen_block(disk, t)
    text, clean = merge3(o, b, t)
    if not clean:
        return "conflict", ""
    if text == o:
        return "same", ""
    return "merge", _regen_block(disk, text)


def _masked_diff(old: str, new: str, label: str) -> dict:
    if filters.has_hard_secret(old):
        return {"diff_withheld": True}
    a, b = filters.mask_secrets(old)[0], filters.mask_secrets(new)[0]
    return {"diff": "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{label}", f"b/{label}"))}


def plan_update(root, data: dict, a: S.Answers, raw: dict, overwrite=(), keep=()) -> UpdatePlan:
    """The merge plan for a project whose state is `data` (from `state.load`). `a`/`raw`: the answers (from the state
    or `--answers`). Raises ScaffoldError (exit 2). Writes nothing."""
    root = Path(root)
    overwrite, keep = set(overwrite), set(keep)
    if not shutil.which("git"):
        raise ScaffoldError("`git` was not found: the 3-way merge needs it (install git and repeat)")
    rendered = S.render(a, with_group=False)
    files = data["files"]
    acts: dict[str, tuple[str, str]] = {}
    snaps: dict[str, tuple[str | None, str]] = {}
    for rel in files.keys() | rendered.keys():
        if rel == st.STATE or rel in (S.GITIGNORE, S.SETTINGS):
            continue
        S._safe(root, rel)
        if rel in rendered:
            snaps[rel] = _snap(S._safe(root, rel))
            acts[rel] = _classify(root, rel, files.get(rel), rendered[rel], snaps[rel])
        else:
            acts[rel] = ("orphan", "")
    gs_fp: dict[str, str] = {}
    gs: dict[str, tuple[str, str | None, str | None]] = {}   # .gitignore / settings.json: (action, merged, old)
    for rel in (S.GITIGNORE, S.SETTINGS):
        p = S._safe(root, rel)
        old, gfp = _snap(p)
        if old is None and gfp not in (S._ABSENT, S._NONFILE):
            raise ScaffoldError(f"{rel}: not valid UTF-8")
        gs_fp[rel] = gfp
        try:
            merged = S._merge_gitignore(old, a) if rel == S.GITIGNORE else S._merge_settings(old, S.settings_allow(a))
            gs[rel] = ("create" if old is None else ("same" if merged is None else "merge"), merged, old)
        except ValueError:
            gs[rel] = ("conflict", None, old)
    kept = {r for r in files if files[r]["mode"] == "keep" and r in rendered and r in acts}   # merged like owned (ADR-038)
    known = {r for r, (act, _) in acts.items() if act == "conflict"} | kept | \
        {r for r, (act, _, _) in gs.items() if act == "conflict"}
    for flag, paths in (("--overwrite", overwrite), ("--keep", keep)):
        if st.STATE in paths:
            raise ScaffoldError(f"{flag}: {st.STATE} is written by the scaffold itself, it takes no choices")
        bad = sorted(paths - known)
        if bad:
            raise ScaffoldError(f"{flag}: not a conflict (or a kept file) of this update: {', '.join(bad)}")
    both = sorted(overwrite & keep)
    if both:
        raise ScaffoldError(f"{', '.join(both)}: given to both --overwrite and --keep")

    pl = S.Plan()
    rows: list[tuple[str, str]] = []
    texts: dict[str, str] = {}
    modes: dict[str, str] = {}
    diffs: dict[str, dict] = {}
    conflicts: list[str] = []
    write: list[str] = []
    for rel in sorted(acts):
        act, text = acts[rel]
        entry = files.get(rel)
        p = root / rel
        sn = snaps.get(rel)
        disk = "" if sn is None or sn[1] in (S._ABSENT, S._NONFILE) else sn[0]
        if act == "conflict":
            if rel in overwrite:
                act = "overwrite"
            elif rel in keep:
                act = "keep"
            else:
                conflicts.append(rel)
        elif rel in kept:
            if rel in keep and act in ("update", "merge"):
                act = "keep"
            elif rel in overwrite and act != "same":
                act = "overwrite"
        # shown row and the apply row
        rows.append((rel, act))
        if act in ("update", "merge", "create", "overwrite") or rel in conflicts:
            pl.fp[rel] = sn[1] if sn else S._fingerprint(S._safe(root, rel))
            pl.append((rel, {"update": "merge", "overwrite": "conflict"}.get(act, act)))
        if act in _WRITE_ACTS:
            write.append(rel)
            texts[rel] = text if act in ("update", "merge") else rendered[rel]
        # state mode
        if act in ("same", "unchanged", "update", "merge", "create", "overwrite", "keep") and rel in rendered:
            if rel in kept and rel in overwrite:
                mode = "owned"
            elif act == "keep":
                mode = "keep" if (entry is None or entry["mode"] == "keep" or not p.is_file()) else entry["mode"]
            elif act == "overwrite":
                mode = "owned"
            else:
                mode = entry["mode"] if entry else "owned"
            modes[rel] = mode
        # diffs
        if disk is None:
            diffs[rel] = {"diff_withheld": True, "note": "the file cannot be read as UTF-8 text or is not a regular file"}
        elif act in ("update", "merge"):
            diffs[rel] = _masked_diff(disk, text, rel)
        elif act == "overwrite":
            diffs[rel] = _masked_diff(disk, rendered[rel], rel)
        elif act == "conflict":
            base = entry["base"] if entry else ""
            d = _masked_diff(base, rendered[rel], rel)
            diffs[rel] = {"template_diff": d["diff"]} if "diff" in d else d
        elif act == "keep" and rel in kept and entry["base"] != rendered[rel]:
            d = _masked_diff(entry["base"], rendered[rel], rel)
            diffs[rel] = {"template_diff": d["diff"]} if "diff" in d else d
        if rel in kept:
            diffs.setdefault(rel, {})["mode"] = "keep"
    # .gitignore and settings.json: merged idempotently as in scaffold.plan
    for rel, (act, _, old) in gs.items():
        pl.fp[rel] = gs_fp[rel]
        if act == "conflict":
            if rel in overwrite:
                act = "overwrite"
            elif rel in keep:
                act = "keep"
            else:
                conflicts.append(rel)
        rows.append((rel, act))
        if act in ("create", "merge", "overwrite") or rel in conflicts:
            pl.append((rel, "conflict" if act == "overwrite" else act))
        if act in _WRITE_ACTS:
            write.append(rel)
            diffs[rel] = _masked_diff(old or "", S._new_text(root, rel, texts, a, False), rel)

    # the state row, always last
    stext = st.build_modes(raw, rendered, modes, S.PROCESS_VERSION, S._norm)
    if filters.has_hard_secret(stext):
        raise ScaffoldError("the scaffold state would contain a secret, refusing to write it")
    sp = S._safe(root, st.STATE)
    if sp.exists() and not sp.is_file():
        raise ScaffoldError(f"{st.STATE}: exists and is not a regular file")
    sdisk, pl.fp[st.STATE] = _snap(sp)
    texts[st.STATE] = stext
    act = "same" if sdisk == stext else ("create" if not sp.exists() else "merge")
    rows.append((st.STATE, act))
    if act != "same":
        pl.append((st.STATE, act))
        write.append(st.STATE)
        diffs[st.STATE] = {"diff_withheld": True, "note": "scaffold state, rewritten by the scaffold itself"}
    return UpdatePlan(rows, pl, texts, sorted(conflicts), write, diffs, data.get("version", 0), S.PROCESS_VERSION,
                      overwrite, keep, a)


def status(up: UpdatePlan, written: bool = False) -> str:
    if up.conflicts:
        return "conflict"
    if not up.write:
        return "current"
    return "written" if written else "preview"


def execute(root, up: UpdatePlan) -> list[str]:
    """Transactional write through `scaffold.apply` (ScaffoldChanged -> exit 3 in the caller). Returns the backups
    (`*.bak-<ts>`) it left next to the replaced files."""
    root = Path(root)
    if up.conflicts or not up.write:
        return []

    def baks():
        return {str(b.relative_to(root)) for rel in up.write for b in (root / rel).parent.glob(Path(rel).name + ".bak-*")}

    before = baks()
    S.apply(root, up.apply_plan, up.texts, up.overwrite, up.a, up.keep)
    return sorted(baks() - before)


def report(up: UpdatePlan, st_: str, backups: list[str] | None = None) -> dict:
    """The `--json` object (ADR-035)."""
    files = []
    for rel, act in up.rows:
        d = {"path": rel, "action": act}
        d.update(up.diffs.get(rel, {}))
        files.append(d)
    out = {"status": st_, "from_version": up.from_version, "to_version": up.to_version, "files": files,
           "conflicts": up.conflicts, "write": up.write}
    if backups:
        out["backups"] = backups
    return out
