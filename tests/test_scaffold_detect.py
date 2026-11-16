import builtins
import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from myvec.commands import app
from myvec.scaffold import detect as D

runner = CliRunner()


def tree(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf8")
    return root


def unk(rep, key):
    return next((u for u in rep["unknown"] if u["key"] == key), None)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})


def test_python_uv_pytest_ruff(tmp_path):
    root = tree(tmp_path / "p", {"pyproject.toml": '[project]\nname="x"\ndependencies=["fastapi"]\n'
                                 '[dependency-groups]\ndev=["pytest"]\n[tool.ruff]\nline-length=100\n', "uv.lock": ""})
    a = D.detect(root)["answers"]
    assert a["test_cmd"] == "uv run pytest -q" and a["lint_cmd"] == "uv run ruff check ."
    assert a["stack"].startswith("Python") and "FastAPI" in a["stack"]


def test_python_poetry_and_plain_pip(tmp_path):
    p = tree(tmp_path / "po", {"pyproject.toml": '[tool.poetry]\nname="x"\n[tool.pytest.ini_options]\n', "poetry.lock": ""})
    assert D.detect(p)["answers"]["test_cmd"] == "poetry run pytest"
    q = tree(tmp_path / "pip", {"pyproject.toml": '[project]\nname="x"\ndependencies=["pytest"]\n', "requirements.txt": ""})
    assert D.detect(q)["answers"]["test_cmd"] == "pytest"


def test_python_without_pytest_has_no_test_cmd(tmp_path):
    root = tree(tmp_path / "p", {"pyproject.toml": '[project]\nname="x"\n', "uv.lock": ""})
    rep = D.detect(root)
    assert "test_cmd" not in rep["answers"] and unk(rep, "test_cmd")["candidates"] == []


def test_node_npm_vitest_biome_vite(tmp_path):
    root = tree(tmp_path / "n", {
        "package.json": {"scripts": {"test": "vitest", "lint": "biome check", "dev": "vite"},
                         "devDependencies": {"vite": "5", "react": "18", "typescript": "5"}},
        "package-lock.json": "{}", "wrangler.toml": ""})
    rep = D.detect(root)
    assert rep["answers"]["test_cmd"] == "npm test" and rep["answers"]["lint_cmd"] == "npm run lint"
    assert "TypeScript" in rep["answers"]["stack"] and "Cloudflare Workers" in rep["answers"]["stack"]
    assert rep["ui"] == {"detected": True, "url_guess": "http://127.0.0.1:5173"}
    assert unk(rep, "ui")["url_guess"] == "http://127.0.0.1:5173"
    assert "ui" not in rep["answers"]


@pytest.mark.parametrize("lock,pm", [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm")])
def test_node_package_manager_by_lock(tmp_path, lock, pm):
    root = tree(tmp_path / "n", {"package.json": {"scripts": {"test": "jest", "lint": "eslint ."}}, lock: ""})
    a = D.detect(root)["answers"]
    assert a["test_cmd"] == f"{pm} test" and a["lint_cmd"] == f"{pm} run lint"


def test_node_default_npm_test_stub_is_not_a_test_command(tmp_path):
    root = tree(tmp_path / "n", {"package.json": {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}})
    rep = D.detect(root)
    assert "test_cmd" not in rep["answers"] and unk(rep, "test_cmd")


def test_laravel_artisan(tmp_path):
    root = tree(tmp_path / "l", {"composer.json": {"require": {"laravel/framework": "11"}, "require-dev": {"laravel/pint": "1"}},
                                 "artisan": "#!/usr/bin/env php\n"})
    rep = D.detect(root)
    assert rep["answers"]["test_cmd"] == "php artisan test" and rep["answers"]["lint_cmd"] == "vendor/bin/pint --test"
    assert "Laravel" in rep["answers"]["stack"]
    assert rep["ui"]["url_guess"] == "http://127.0.0.1:8000"


def test_php_phpunit_and_phpstan(tmp_path):
    root = tree(tmp_path / "l", {"composer.json": {"require": {}}, "phpunit.xml.dist": "<phpunit/>", "phpstan.neon": ""})
    a = D.detect(root)["answers"]
    assert a["test_cmd"] == "vendor/bin/phpunit" and a["lint_cmd"] == "vendor/bin/phpstan analyse"


def test_go_and_golangci(tmp_path):
    root = tree(tmp_path / "g", {"go.mod": "module x\n", ".golangci.yml": ""})
    a = D.detect(root)["answers"]
    assert a["test_cmd"] == "go test ./..." and a["lint_cmd"] == "golangci-lint run" and a["stack"] == "Go"
    assert "lint_cmd" not in D.detect(tree(tmp_path / "g2", {"go.mod": "module x\n"}))["answers"]


def test_rust_cargo(tmp_path):
    a = D.detect(tree(tmp_path / "r", {"Cargo.toml": '[package]\nname="x"\n'}))["answers"]
    assert a["test_cmd"] == "cargo test" and a["lint_cmd"] == "cargo clippy" and a["stack"] == "Rust"


def test_makefile_is_fallback_only(tmp_path):
    root = tree(tmp_path / "m", {"Makefile": "build:\n\techo\ntest:\n\techo\nlint:\n\techo\n"})
    a = D.detect(root)["answers"]
    assert a["test_cmd"] == "make test" and a["lint_cmd"] == "make lint"
    root = tree(tmp_path / "g", {"go.mod": "module x\n", "Makefile": "test:\n\techo\n"})
    assert D.detect(root)["answers"]["test_cmd"] == "go test ./..."


def test_monorepo_two_stacks_leaves_test_cmd_unknown(tmp_path):
    root = tree(tmp_path / "mono", {"package.json": {"scripts": {"test": "vitest"}}, "go.mod": "module x\n"})
    rep = D.detect(root)
    u = unk(rep, "test_cmd")
    assert "test_cmd" not in rep["answers"] and sorted(u["candidates"]) == ["go test ./...", "npm test"]
    assert len(rep["candidates"]) == 2


def test_nothing_known_marks_stack_and_test_unknown(tmp_path):
    rep = D.detect(tree(tmp_path / "x", {"notes.txt": "hi"}))
    assert unk(rep, "stack") and unk(rep, "test_cmd") and unk(rep, "description")
    assert rep["candidates"] == [] and rep["state"]["empty"] is False


def test_empty_dir_state(tmp_path):
    root = tmp_path / "e"
    root.mkdir()
    (root / ".hidden").write_text("x")
    assert D.detect(root)["state"]["empty"] is True


def test_project_name_from_marker_here_and_above(tmp_path):
    root = tree(tmp_path / "p", {".myvector.toml": 'name = "named"\n', "go.mod": "module x\n"})
    rep = D.detect(root)
    assert rep["answers"]["project"] == "named"
    assert rep["state"]["marker"]["present"] and rep["state"]["marker"]["here"] is True
    tree(tmp_path / "outer", {".myvector.toml": 'name = "outer"\n'})
    sub = tree(tmp_path / "outer" / "sub", {"go.mod": "module x\n"})
    m = D.detect(sub)["state"]["marker"]
    assert m["present"] and m["here"] is False
    assert not D.detect(tree(tmp_path / "plain", {"go.mod": "x"}))["state"]["marker"]["present"]


def test_invalid_marker_is_reported_not_raised(tmp_path):
    root = tree(tmp_path / "p", {".myvector.toml": 'name = "../x"\n'})
    m = D.detect(root)["state"]["marker"]
    assert m["present"] and m["valid"] is False


def test_process_state(tmp_path):
    root = tree(tmp_path / "p", {"CLAUDE.md": "x", ".claude/agents/coder.md": "x", ".claude/agents/qa.md": "x",
                                 ".claude/rules/workflow.md": "x", ".claude/settings.json": '{"model":"o","permissions":{}}'})
    pr = D.detect(root)["state"]["process"]
    assert pr["claude_md"] and pr["agents"] == ["coder.md", "qa.md"] and pr["rules"] == ["workflow.md"]
    assert pr["settings"] and pr["settings_keys"] == ["model", "permissions"]
    pr = D.detect(tree(tmp_path / "q", {"a": "b"}))["state"]["process"]
    assert not pr["claude_md"] and pr["agents"] == [] and not pr["settings"]


def test_tickets_none_canon_foreign(tmp_path):
    assert D.detect(tree(tmp_path / "a", {"x": "y"}))["state"]["tickets"] == {"state": "none", "files": 0}
    canon = tree(tmp_path / "b", {
        "docs/todo/E1-g/index.md": "---\nid: E1\ntitle: G\nkind: group\nstatus: ⬜\n---\n\n# E1 G\n",
        "docs/todo/E1-g/E1-01-a.md": "---\nid: E1-01\ntitle: A\nkind: ticket\nstatus: ⬜\nparent: E1\nsize: S\n---\n\n"
                                      "# E1-01 A\n\n## Goal\n\ng\n"})
    assert D.detect(canon)["state"]["tickets"]["state"] in ("canon", "foreign")  # shape depends on the canon checker
    foreign = tree(tmp_path / "c", {
        "docs/todo/E1-x/E1-01-a.md": "---\nid: E1-01\ntitle: A\nstatus: ⬜\nepic: E1\n---\n\n# E1-01 A\n\n## За один заход\n\nтекст\n"})
    t = D.detect(foreign)["state"]["tickets"]
    assert t["state"] == "foreign" and t["files"] == 1


def test_tickets_canon_when_check_is_clean(tmp_path):
    # a tree generated by the scaffold itself is the canon
    from myvec import scaffold as S
    root = tmp_path / "gen"
    root.mkdir()
    ans = tmp_path / "a.json"
    ans.write_text(json.dumps({"project": "d", "description": "D.", "stack": "Go", "test_cmd": "go test"}), encoding="utf8")
    r = runner.invoke(app, ["scaffold", "--answers", str(ans), "--root", str(root)])
    assert r.exit_code == 0, r.output
    assert D.detect(root)["state"]["tickets"]["state"] == "canon"
    assert S  # imported for the scaffold side effects


def test_git_state_clean_dirty_and_not_a_repo(tmp_path):
    root = tree(tmp_path / "g", {"go.mod": "module x\n"})
    assert D.detect(root)["state"]["git"] == {"is_repo": False, "branch": "", "dirty": 0, "dirty_scaffold_paths": []}
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "i")
    g = D.detect(root)["state"]["git"]
    assert g["is_repo"] and g["branch"] == "main" and g["dirty"] == 0
    (root / "CLAUDE.md").write_text("mine")
    (root / ".claude/agents").mkdir(parents=True)
    (root / ".claude/agents/coder.md").write_text("mine")
    (root / "src.go").write_text("package x")
    g = D.detect(root)["state"]["git"]
    assert g["dirty"] == 3
    assert g["dirty_scaffold_paths"] == [".claude/agents/coder.md", "CLAUDE.md"]


def test_git_dirty_paths_relative_to_a_subdirectory(tmp_path):
    top = tree(tmp_path / "top", {"a.txt": "x"})
    git(top, "init", "-q", "-b", "main")
    sub = tree(top / "sub", {"CLAUDE.md": "mine", "other.txt": "x"})
    g = D.detect(sub)["state"]["git"]
    assert g["dirty"] == 2 and g["dirty_scaffold_paths"] == ["CLAUDE.md"]


CY_UK = "Це проєкт для обліку замовлень. Він містить інструкції для розробників і користувачів, їх треба читати."
CY_RU = "Это проект для учёта заказов. Он содержит инструкции для разработчиков и пользователей, их нужно читать."
EN = "This project tracks orders. It contains instructions for developers and users, please read them first."


MIXED = "Проект для заказов ыэъё ыэъё ыэъё і ї є ґ і ї є ґ і ї є ґ и других задач и данных для всех"


@pytest.mark.parametrize("text,lang", [(CY_UK, "uk"), (CY_RU, "ru"), (EN, "en"), ("hi", "none"), (MIXED, "mixed")])
def test_docs_language(tmp_path, text, lang):
    root = tree(tmp_path / "d", {"CLAUDE.md": text})
    assert D.detect(root)["docs_language_detected"] == lang


def test_docs_language_uses_readme_and_docs_and_lists_evidence(tmp_path):
    root = tree(tmp_path / "d", {"README.md": CY_RU, "docs/guide.md": CY_RU})
    rep = D.detect(root)
    assert rep["docs_language_detected"] == "ru" and {"README.md", "docs/guide.md"} <= set(rep["docs_language_evidence"])


def test_readme_title_hint_and_secret_title_dropped(tmp_path):
    rep = D.detect(tree(tmp_path / "a", {"README.md": "# My App\n\ntext"}))
    assert unk(rep, "description")["readme_title"] == "My App"
    rep = D.detect(tree(tmp_path / "b", {"README.md": "# AKIAIOSFODNN7EXAMPLE\n"}))
    assert unk(rep, "description")["readme_title"] == ""


# ------------------------------------------------------------ what is opened

SECRET_FILES = {".env": "TOKEN=abc\n", ".env.local": "X=1\n", ".dev.vars": "SECRET=zzz\n", ".dev.vars.production": "S=1\n",
                "server.key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n", "id_rsa": "k", "db.sql": "insert"}


def _spy(monkeypatch):
    opened: list[str] = []
    real_open, real_os_open, real_read_text = builtins.open, os.open, Path.read_text

    def sp_open(f, *a, **k):
        opened.append(str(f))
        return real_open(f, *a, **k)

    def sp_os_open(path, *a, **k):
        opened.append(str(path))
        return real_os_open(path, *a, **k)

    def sp_read_text(self, *a, **k):
        opened.append(str(self))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(builtins, "open", sp_open)
    monkeypatch.setattr(os, "open", sp_os_open)
    monkeypatch.setattr(Path, "read_text", sp_read_text)
    return opened


def test_detector_never_opens_env_dev_vars_or_keys(tmp_path, monkeypatch):
    root = tree(tmp_path / "p", {"package.json": {"scripts": {"test": "vitest"}}, "README.md": "# T\n", **SECRET_FILES,
                                 "docs/secrets.md": "x", "docs/.env": "A=1", "docs/notes.md": EN})
    opened = _spy(monkeypatch)
    D.detect(root)
    names = {Path(p).name for p in opened if str(root) in p}
    assert names, "the spy saw nothing"
    for bad in list(SECRET_FILES) + ["docs/.env"]:
        assert Path(bad).name not in names, bad
    assert names <= {"package.json", "README.md", "notes.md", "CLAUDE.md", "settings.json", "ticket-template.md"}, names


def test_detector_ignores_unreadable_secret_files(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    root = tree(tmp_path / "p", {"go.mod": "module x\n", **SECRET_FILES})
    for n in SECRET_FILES:
        (root / n).chmod(0)
    try:
        assert D.detect(root)["answers"]["test_cmd"] == "go test ./..."  # no PermissionError either
    finally:
        for n in SECRET_FILES:
            (root / n).chmod(0o600)


def test_detector_does_not_follow_symlinks(tmp_path):
    outside = tree(tmp_path / "out", {"package.json": json.dumps({"scripts": {"test": "leak"}})})
    root = tmp_path / "p"
    root.mkdir()
    (root / "package.json").symlink_to(outside / "package.json")
    (root / "CLAUDE.md").symlink_to(outside / "package.json")
    rep = D.detect(root)
    assert "test_cmd" not in rep["answers"] and rep["docs_language_detected"] == "none"
    assert "leak" not in json.dumps(rep)


def test_detector_skips_files_over_256kb(tmp_path):
    big = json.dumps({"scripts": {"test": "vitest"}, "pad": "x" * (D.MAX_BYTES + 10)})
    rep = D.detect(tree(tmp_path / "p", {"package.json": big}))
    assert "test_cmd" not in rep["answers"]
    ok = json.dumps({"scripts": {"test": "vitest"}})
    assert D.detect(tree(tmp_path / "q", {"package.json": ok}))["answers"]["test_cmd"] == "npm test"


def test_non_utf8_manifest_is_ignored(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "package.json").write_bytes(b"\xff\xfe\x00bad")
    assert "test_cmd" not in D.detect(root)["answers"]


def test_secret_in_script_never_reaches_answers_or_output(tmp_path):
    secret = "AKIAIOSFODNN7EXAMPLE"
    root = tree(tmp_path / "p", {"package.json": {"scripts": {"test": f"AWS_KEY={secret} vitest"}}})
    rep = D.detect(root)
    assert rep["answers"]["test_cmd"] == "npm test"  # the script body is never copied, only the runner command
    assert secret not in json.dumps(rep)
    r = runner.invoke(app, ["scaffold", "--detect", "--root", str(root)])
    assert r.exit_code == 0 and secret not in r.output
    r = runner.invoke(app, ["scaffold", "--detect", "--json", "--root", str(root)])
    assert r.exit_code == 0 and secret not in r.output


def test_secret_in_pyproject_dependency_text_is_not_leaked(tmp_path):
    secret = "ghp_" + "a" * 36
    root = tree(tmp_path / "p", {"pyproject.toml": f'[project]\nname="x"\ndescription="{secret}"\ndependencies=["pytest"]\n'})
    assert secret not in json.dumps(D.detect(root))


# ------------------------------------------------------------ CLI

def test_cli_detect_json_shape_and_writes_nothing(tmp_path):
    root = tree(tmp_path / "p", {"go.mod": "module x\n"})
    before = sorted(p.name for p in root.rglob("*"))
    r = runner.invoke(app, ["scaffold", "--detect", "--json", "--root", str(root)])
    assert r.exit_code == 0, r.output
    rep = json.loads(r.stdout)
    assert {"answers", "unknown", "candidates", "state", "docs_language_detected"} <= set(rep)
    assert sorted(p.name for p in root.rglob("*")) == before


def test_cli_detect_human_output(tmp_path):
    root = tree(tmp_path / "p", {"go.mod": "module x\n"})
    r = runner.invoke(app, ["scaffold", "--detect", "--root", str(root)])
    assert r.exit_code == 0 and "test_cmd: go test ./..." in r.output and "unknown:" in r.output


@pytest.mark.parametrize("extra", [["--answers", "a.json"], ["--overwrite", "CLAUDE.md"], ["--keep", "CLAUDE.md"],
                                   ["--append", "CLAUDE.md"], ["--no-tickets"], ["--dry-run"], ["--diff"]])
def test_cli_detect_with_other_flags_is_exit_2(tmp_path, extra):
    r = runner.invoke(app, ["scaffold", "--detect", "--root", str(tmp_path), *extra])
    assert r.exit_code == 2


def test_cli_without_detect_and_without_answers_is_exit_2(tmp_path):
    assert runner.invoke(app, ["scaffold", "--root", str(tmp_path)]).exit_code == 2


def test_cli_diff_without_dry_run_is_exit_2(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"project": "d", "description": "D.", "stack": "Go", "test_cmd": "go test"}), encoding="utf8")
    root = tmp_path / "r"
    root.mkdir()
    assert runner.invoke(app, ["scaffold", "--answers", str(a), "--root", str(root), "--diff"]).exit_code == 2
    assert not any(root.iterdir())


def test_symlinked_directories_are_not_read(tmp_path):
    outside = tmp_path / "outside"
    (outside / "agents").mkdir(parents=True)
    (outside / "agents" / "coder.md").write_text("x", encoding="utf8")
    (outside / "settings.json").write_text('{"secretkey": 1}', encoding="utf8")
    (outside / "todo").mkdir()
    (outside / "todo" / "A.md").write_text("---\nid: A\n---\n", encoding="utf8")
    (outside / "notes.md").write_text("Это русский текст про проект и его устройство, подробно.", encoding="utf8")
    root = tmp_path / "root"
    root.mkdir()
    (root / ".claude").symlink_to(outside, target_is_directory=True)
    (root / "docs").symlink_to(outside, target_is_directory=True)
    rep = D.detect(root)
    assert rep["state"]["process"]["agents"] == []
    assert rep["state"]["process"]["settings_keys"] == []
    assert rep["state"]["tickets"]["state"] == "none"
    assert rep["docs_language_detected"] == "none"
