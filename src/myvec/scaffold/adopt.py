"""`myvec scaffold --adopt` (ADR-041): put only the search rule into a project that has no scaffold state.

No typer. The one file goes through the usual `scaffold.plan` / `scaffold.apply` (create via `os.link`, fingerprint check),
the state `.myvec/scaffold.json` is NOT written: a state built from the current files would make the next `--update`
see "disk = base" and overwrite custom agents with the template.
"""
from __future__ import annotations

from pathlib import Path

from myvec import filters
from myvec import problems
from myvec import scaffold as S
from myvec.scaffold import state as st


def run(root: Path, write: bool) -> dict:
    """{status: preview|written|current|foreign, files: [{path, action: create|update|same|foreign}], write: [paths], text,
    version (the old render's PROCESS_VERSION for `update`), backups}.
    Raises ScaffoldError (exit 2: wrong state, no/linked `.claude`, symlink on the path) and ScaffoldChanged (exit 3)."""
    root = Path(root)
    rep = st.read(root)
    if rep["status"] in ("current", "outdated"):
        raise S.ScaffoldError("the project has a scaffold state: use `myvec scaffold --update` (it creates the search rule itself)")
    if rep["status"] in ("newer", "invalid"):
        raise S.ScaffoldError(f"the scaffold state is {rep['status']}: {rep['reason'] or 'update myvec'}")
    claude = root / ".claude"
    if claude.is_symlink() or not claude.is_dir():
        raise S.ScaffoldError("no .claude/ directory here (or it is a symbolic link): nothing to adopt")
    text = S._read_template(S.SEARCH_RULE)
    if "{{" in text or filters.has_hard_secret(text):
        raise S.ScaffoldError(f"{S.SEARCH_RULE}: the template is not static or contains a secret")
    rendered = {S.SEARCH_RULE: text}
    pl = S.plan(root, rendered, None)
    action = dict(pl)[S.SEARCH_RULE]
    version = None
    if action == "same":
        status, rows = "current", [(S.SEARCH_RULE, "same")]
    elif action == "conflict" and pl.fp.get(S.SEARCH_RULE) in problems.SEARCH_RULE_OLD:
        # byte-identical to an earlier myvec render (ADR-046): refresh it; the plan's fingerprint guards the race
        version = problems.SEARCH_RULE_OLD[pl.fp[S.SEARCH_RULE]]
        rule = root / S.SEARCH_RULE
        had = {q.name for q in rule.parent.glob(rule.name + ".bak-*")}
        if write:
            S.apply(root, pl, rendered, {S.SEARCH_RULE})
            status = "written"
        else:
            status = "preview"
        rows = [(S.SEARCH_RULE, "update")]
    elif action == "conflict":
        status, rows = "foreign", [(S.SEARCH_RULE, "foreign")]  # edited or own file: never replaced, no --overwrite
    elif write:
        S.apply(root, pl, rendered, set())
        status, rows = "written", [(S.SEARCH_RULE, "create")]
    else:
        status, rows = "preview", [(S.SEARCH_RULE, "create")]
    backups = []
    if status == "written" and rows[0][1] == "update":   # only the backup this run made, not older ones
        backups = sorted({q.name for q in rule.parent.glob(rule.name + ".bak-*")} - had)
    return {"status": status, "version": version if rows[0][1] == "update" else None, "backups": backups, "files": [{"path": r, "action": a} for r, a in rows],
            "write": [S.SEARCH_RULE] if status == "written" else [], "text": text if status == "preview" else None}
