"""`myvec scaffold --detect`: facts about a folder with code (ADR-023). Writes nothing, asks nothing.

Reads ONLY a whitelist of files by exact name (manifests, lock files for presence, README/CLAUDE.md/docs/*.md for the
language, `docs/todo` for the ticket format). Everything else - `.env*`, `.dev.vars`, keys - is never opened. No
symlinks, no files over 256 KB, no network, no project tools are run (only `git status`).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from myvec import filters, projects
from myvec import scaffold as sc

MAX_BYTES = 256 * 1024
_CYR = re.compile(r"[а-яА-ЯіїєґІЇЄҐыэъёЫЭЪЁ]")
_UK = re.compile(r"[іїєґІЇЄҐ]")
_RU = re.compile(r"[ыэъёЫЭЪЁ]")
_LETTER = re.compile(r"[^\W\d_]")


def _plain(root: Path, rel: str) -> bool:
    """No component of `rel` below root is a symlink (O_NOFOLLOW covers only the last one)."""
    cur = root
    for part in PurePosixPath(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            return False
    return True


def _read(root: Path, rel: str) -> str | None:
    """The single place where file contents are opened. None: denied, missing, symlink, too big, not utf8."""
    if filters.is_denied_path(PurePosixPath(rel).name) or not _plain(root, rel):
        return None
    try:
        fd = os.open(root / rel, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(MAX_BYTES + 1).decode("utf8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def _has(root: Path, rel: str) -> bool:
    return os.path.lexists(root / rel)


def _names(root: Path, rel: str) -> list[str]:
    if not _plain(root, rel):
        return []
    try:
        return sorted(e.name for e in os.scandir(root / rel) if not e.is_symlink())
    except OSError:
        return []


def _json(root: Path, rel: str) -> dict:
    t = _read(root, rel)
    try:
        d = json.loads(t) if t else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _toml(root: Path, rel: str) -> dict:
    t = _read(root, rel)
    try:
        return tomllib.loads(t) if t else {}
    except tomllib.TOMLDecodeError:
        return {}


def _make_targets(root: Path) -> set[str]:
    t = _read(root, "Makefile") or ""
    return set(re.findall(r"^(test|lint):", t, re.M))


# ---------------------------------------------------------------- stacks

def _python(root: Path) -> dict | None:
    if not _has(root, "pyproject.toml"):
        return None
    d = _toml(root, "pyproject.toml")
    tool = d.get("tool") if isinstance(d.get("tool"), dict) else {}
    blob = json.dumps([d.get("project", {}), d.get("dependency-groups", {}), tool.get("poetry", {}),
                       tool.get("uv", {})]).lower()
    ev = ["pyproject.toml"]
    if _has(root, "uv.lock"):
        pm, run = "uv", "uv run "
        ev.append("uv.lock")
    elif _has(root, "poetry.lock") or "poetry" in tool:
        pm, run = "poetry", "poetry run "
        ev.append("poetry.lock" if _has(root, "poetry.lock") else "tool.poetry")
    else:
        pm, run = "pip", ""
        ev += [n for n in _names(root, ".") if n.startswith("requirements") and n.endswith(".txt")]
    test = ""
    if "pytest" in blob or any(k.startswith("pytest") for k in tool):
        test = f"{run}pytest -q" if pm == "uv" else f"{run}pytest"
    lint = f"{run}ruff check ." if ("ruff" in tool or _has(root, "ruff.toml") or _has(root, ".ruff.toml")) else ""
    fw = [n for n in ("FastAPI", "Django", "Flask") if n.lower() in blob]
    return {"lang": "Python", "framework": " + ".join(fw), "pkg_manager": pm, "evidence": ev,
            "test_cmd": test, "lint_cmd": lint, "ui": False, "url_guess": ""}


def _node(root: Path) -> dict | None:
    if not _has(root, "package.json"):
        return None
    d = _json(root, "package.json")
    scripts = d.get("scripts") if isinstance(d.get("scripts"), dict) else {}
    deps = {}
    for k in ("dependencies", "devDependencies"):
        if isinstance(d.get(k), dict):
            deps.update(d[k])
    ev = ["package.json"]
    pm = "npm"
    for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"),
                       ("package-lock.json", "npm")):
        if _has(root, lock):
            pm = name
            ev.append(lock)
            break
    ts = "typescript" in deps or _has(root, "tsconfig.json")
    fw = []
    for key, label in (("next", "Next.js"), ("vite", "Vite"), ("react", "React"), ("vue", "Vue"), ("svelte", "Svelte"),
                       ("hono", "Hono"), ("express", "Express")):
        if key in deps:
            fw.append(label)
    if any(n.startswith("wrangler.") for n in _names(root, ".")):
        fw.append("Cloudflare Workers")
        ev.append("wrangler.*")
    test_script = scripts.get("test")
    has_test = isinstance(test_script, str) and "no test specified" not in test_script
    test = ("bun run test" if pm == "bun" else f"{pm} test") if has_test else ""
    lint = f"{pm} run lint" if isinstance(scripts.get("lint"), str) else ""
    ui = "dev" in scripts or "vite" in deps or "next" in deps
    guess = "http://127.0.0.1:3000" if "next" in deps else "http://127.0.0.1:5173" if ui else ""
    return {"lang": "TypeScript" if ts else "JavaScript", "framework": " + ".join(fw), "pkg_manager": pm,
            "evidence": ev, "test_cmd": test, "lint_cmd": lint, "ui": ui, "url_guess": guess}


def _php(root: Path) -> dict | None:
    if not _has(root, "composer.json"):
        return None
    d = _json(root, "composer.json")
    req = {k for key in ("require", "require-dev") if isinstance(d.get(key), dict) for k in d[key]}
    names = _names(root, ".")
    ev = ["composer.json"]
    laravel = _has(root, "artisan")
    if laravel:
        ev.append("artisan")
    test = ""
    if laravel:
        test = "php artisan test"
    elif any(n.startswith("phpunit.xml") for n in names):
        test = "vendor/bin/phpunit"
        ev.append("phpunit.xml")
    lint = ""
    if "laravel/pint" in req:
        lint = "vendor/bin/pint --test"
    elif any(n.startswith("phpstan.neon") for n in names) or "phpstan/phpstan" in req:
        lint = "vendor/bin/phpstan analyse"
    if "laravel/sail" in req and (_has(root, "compose.yaml") or _has(root, "docker-compose.yml")
                                  or _has(root, "compose.yml") or _has(root, "docker-compose.yaml")):
        # the project runs in Sail containers: the host php/vendor are usually not the ones that work
        ev.append("laravel/sail")
        sail = "./vendor/bin/sail"
        if test == "php artisan test":
            test = f"{sail} artisan test"
        elif test:
            test = f"{sail} bin phpunit"
        if lint.startswith("vendor/bin/"):
            lint = f"{sail} bin {lint[len('vendor/bin/'):]}"
    return {"lang": "PHP", "framework": "Laravel" if laravel else "", "pkg_manager": "composer", "evidence": ev,
            "test_cmd": test, "lint_cmd": lint, "ui": laravel, "url_guess": "http://127.0.0.1:8000" if laravel else ""}


def _go(root: Path) -> dict | None:
    if not _has(root, "go.mod"):
        return None
    cfg = any(n.startswith(".golangci.") and n.rsplit(".", 1)[-1] in ("yml", "yaml") for n in _names(root, "."))
    return {"lang": "Go", "framework": "", "pkg_manager": "go", "evidence": ["go.mod"], "test_cmd": "go test ./...",
            "lint_cmd": "golangci-lint run" if cfg else "", "ui": False, "url_guess": ""}


def _rust(root: Path) -> dict | None:
    if not _has(root, "Cargo.toml"):
        return None
    return {"lang": "Rust", "framework": "", "pkg_manager": "cargo", "evidence": ["Cargo.toml"],
            "test_cmd": "cargo test", "lint_cmd": "cargo clippy", "ui": False, "url_guess": ""}


def _stacks(root: Path) -> list[dict]:
    found = [c for c in (_python(root), _node(root), _php(root), _go(root), _rust(root)) if c]
    if not any(c["test_cmd"] for c in found) and "test" in _make_targets(root):
        found.append({"lang": "make", "framework": "", "pkg_manager": "make", "evidence": ["Makefile"],
                      "test_cmd": "make test", "lint_cmd": "make lint" if "lint" in _make_targets(root) else "",
                      "ui": False, "url_guess": ""})
    for c in found:
        c["stack"] = c["lang"] + (f" ({c['framework']})" if c["framework"] else "")
    return found


# ---------------------------------------------------------------- state

def _marker(root: Path) -> dict:
    try:
        m = projects.find_marker(root)
    except projects.InvalidMarker as e:
        return {"present": True, "valid": False, "error": str(e)}
    if m is None:
        return {"present": False}
    return {"present": True, "valid": True, "name": m.path or m.name, "here": Path(m.root).resolve() == root,
            "root": str(m.root)}


def _process(root: Path) -> dict:
    settings = _json(root, ".claude/settings.json")
    return {
        "claude_md": _has(root, "CLAUDE.md"),
        "agents": [n for n in _names(root, ".claude/agents") if n.endswith(".md")],
        "rules": [n for n in _names(root, ".claude/rules") if n.endswith(".md")],
        "settings": _has(root, ".claude/settings.json"),
        "settings_keys": sorted(settings),
    }


def _tickets(root: Path) -> dict:
    base = root / "docs" / "todo"
    if not base.is_dir() or not _plain(root, "docs/todo"):
        return {"state": "none", "files": 0}
    from myvec import tickets as T

    try:
        files = [f for f in base.rglob("*.md") if f.name not in T.SKIP_FILES and not f.is_symlink()]
        if not files:
            return {"state": "none", "files": 0}
        ts = T.load_dir(root, "docs/todo")
        errors = [i for i in T.check(ts) if i.severity == "error" or i.code.startswith("legacy")]
    except Exception:  # a foreign tree must not break the detector
        return {"state": "foreign", "files": 0}
    ok = bool(ts.tickets) and not errors
    return {"state": "canon" if ok else "foreign", "files": len(files)}


def _git(root: Path) -> dict:
    def run(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.decode("utf8", "replace") if r.returncode == 0 else None

    if run("rev-parse", "--is-inside-work-tree") is None:
        return {"is_repo": False, "branch": "", "dirty": 0, "dirty_scaffold_paths": []}
    prefix = (run("rev-parse", "--show-prefix") or "").strip()
    branch = (run("branch", "--show-current") or "").strip()
    out = run("status", "--porcelain=v1", "-z", "-uall") or ""
    parts = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        paths.append(e[3:])
        if e[0] in "RC":  # the next entry is the old name
            i += 1
    if prefix:
        paths = [p[len(prefix):] for p in paths if p.startswith(prefix)]
    own = set(sc.STATIC) | {sc.INDEX, sc.GITIGNORE, sc.SETTINGS}
    dirty_own = sorted(p for p in paths if p in own or p.startswith(sc.TICKETS_PREFIX) and p.endswith("/index.md"))
    return {"is_repo": True, "branch": branch, "dirty": len(paths), "dirty_scaffold_paths": dirty_own}


def _empty(root: Path) -> bool:
    return not any(not n.startswith(".") for n in _names(root, "."))


def _language(root: Path) -> tuple[str, list[str], dict[str, int]]:
    """(language, evidence files, percent of Cyrillic letters per evidence file)."""
    rels = ["CLAUDE.md", *(n for n in _names(root, ".") if n.upper().startswith("README")),
            *[f"docs/{n}" for n in _names(root, "docs") if n.endswith(".md")][:20], "docs/todo/ticket-template.md"]
    letters = cyr = uk = ru = 0
    ev = []
    shares: dict[str, int] = {}
    for rel in rels:
        t = _read(root, rel)
        if not t:
            continue
        ev.append(rel)
        shares[rel] = round(100 * len(_CYR.findall(t)) / max(1, len(_LETTER.findall(t))))
        letters += len(_LETTER.findall(t))
        cyr += len(_CYR.findall(t))
        uk += len(_UK.findall(t))
        ru += len(_RU.findall(t))
    if letters < 20:
        return "none", ev, shares
    if cyr / letters < 0.1:
        return "en", ev, shares
    if uk >= 2 * ru:
        return "uk", ev, shares
    if ru >= 2 * uk:
        return "ru", ev, shares
    return "mixed", ev, shares


def _readme_title(root: Path) -> str:
    for n in _names(root, "."):
        if n.upper().startswith("README") and n.lower().endswith(".md"):
            for line in (_read(root, n) or "").splitlines():
                if line.startswith("# "):
                    t = line[2:].strip()[:120]
                    return "" if filters.has_hard_secret(t) else t
    return ""


# ---------------------------------------------------------------- entry point

def detect(root) -> dict:
    root = Path(root).resolve()
    cands = _stacks(root)
    marker = _marker(root)
    answers: dict = {"project": marker["name"] if marker.get("name") else root.name}
    unknown: list[dict] = []
    withtest = [c for c in cands if c["test_cmd"]]
    if cands:
        answers["stack"] = " + ".join(c["stack"] for c in cands)
    else:
        unknown.append({"key": "stack", "reason": "no known manifest found"})
    if len(withtest) == 1:
        answers["test_cmd"] = withtest[0]["test_cmd"]
        if withtest[0]["lint_cmd"]:
            answers["lint_cmd"] = withtest[0]["lint_cmd"]
    else:
        unknown.append({"key": "test_cmd", "reason": "several stacks with tests" if withtest else "no test command found",
                        "candidates": [c["test_cmd"] for c in withtest]})
        lints = [c["lint_cmd"] for c in cands if c["lint_cmd"]]
        if lints:
            unknown.append({"key": "lint_cmd", "reason": "several linters" if len(lints) > 1 else "not tied to one stack",
                            "candidates": lints})
    guesses = [c["url_guess"] for c in cands if c["url_guess"]]
    ui = {"detected": any(c["ui"] for c in cands), "url_guess": guesses[0] if guesses else ""}
    if ui["detected"]:
        unknown.append({"key": "ui", "reason": "describe how to check the UI live", "url_guess": ui["url_guess"]})
    unknown.append({"key": "description", "reason": "one sentence is written by a person", "readme_title": _readme_title(root)})
    for k, v in list(answers.items()):
        try:
            sc._check_value(v, k)
        except sc.ScaffoldError:
            del answers[k]
            unknown.append({"key": k, "reason": "the value looked like a secret and was dropped"})
    lang, lang_ev, lang_shares = _language(root)
    return {
        "answers": answers,
        "unknown": unknown,
        "candidates": [{k: c[k] for k in ("lang", "framework", "pkg_manager", "evidence", "test_cmd", "lint_cmd")} for c in cands],
        "ui": ui,
        "state": {"marker": marker, "process": _process(root), "tickets": _tickets(root), "git": _git(root),
                  "empty": _empty(root)},
        "docs_language_detected": lang,
        "docs_language_evidence": lang_ev,
        "docs_language_cyrillic_percent": lang_shares,
    }
