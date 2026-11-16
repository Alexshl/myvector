"""Static guard for cloud/ (E4-02): reads files only, needs no node."""
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLOUD = ROOT / "cloud"


def test_dependencies_are_exact_versions():
    pkg = json.loads((CLOUD / "package.json").read_text())
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert deps
    for name, ver in deps.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", ver), f"{name}: {ver} is not an exact version"
    assert pkg["private"] is True


def test_lockfile_committed_and_ignores():
    assert (CLOUD / "package-lock.json").is_file()
    lines = (ROOT / ".gitignore").read_text().split()
    for entry in ("cloud/node_modules/", "cloud/.wrangler/", "cloud/.dev.vars"):
        assert entry in lines


def test_wrangler_toml_local_only():
    text = (CLOUD / "wrangler.toml").read_text()
    conf = tomllib.loads(text)
    for key in ("account_id", "routes", "route", "workers_dev"):
        assert key not in conf, key
    assert "ACCESS_DEV_BYPASS" not in conf.get("vars", {})
    assert not re.search(r"^\s*(?!#).*ACCESS_DEV_BYPASS", text, re.M)
    assert conf.get("env", {}) == {}


def test_migrations_naming():
    files = sorted(p.name for p in (CLOUD / "migrations").iterdir())
    assert files
    for name in files:
        assert re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", name), name
