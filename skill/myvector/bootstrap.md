# MyVector bootstrap: start a project with one command

Loaded on demand from SKILL.md (`/myvector` in an EMPTY folder, or in a folder with code: see "Existing code" at the end). In a new project all generated process documents (agents, rules, CLAUDE.md) are in English; do not ask about the language.

Split of work: you run the interview and decide; `myvec scaffold` only renders templates (deterministic, tested). Never write the process files by hand. The process files include the static search rule `.claude/rules/search.md` (no slots); a project that only needs it, without the full process, uses `myvec scaffold --adopt` (see SKILL.md, "Update the process").

## 1. Interview (grill style, no plugin needed)

Walk the decision tree by its frontier: every round is ONE `AskUserQuestion` call with up to 4 questions. Put the recommended answer first with "(Recommended)" and word each question so that "yes" means the recommendation. Find facts yourself instead of asking (OS, `uv`/`node`/`php` on PATH, git, `gh`).

- Round 1: what the project is (name = folder name by default, one sentence), the stack.
- Round 2 (depends on the stack): the test command, the linter (or none), whether there is a UI and how to check it live (URL), the machines the project runs on.
- Round 3: the first ticket group (id like `E1`, slug, title), the MyVector sync mode (Авто / Завжди / Вимк).
- Continue while the frontier is not empty (an answer opened a new branch). Do not ask what a default answers well.

## 2. Answers and plan

Write `answers.json` into a temporary directory (NOT into the project):

```json
{"project": "demo", "description": "One sentence.", "stack": "Python 3.12 + FastAPI",
 "test_cmd": "uv run pytest -q", "lint_cmd": "ruff check .",
 "ui": {"url": "http://127.0.0.1:8000", "check": "How to verify it live."},
 "machines": "laptop, server", "status_style": "emoji",
 "first_group": {"id": "E1", "slug": "foundation", "title": "Foundation"}, "slots": {}}
```

Required: `project`, `description`, `stack`, `test_cmd`. Everything else is optional; `slots` overrides project-specific lines of the agents and rules (rarely needed). Slots for what differs most between projects: `adr_path` (default `docs/decisions.md`; the scaffold creates the ADR file there), `parallel_lines` (a number, default `2`), `branch_pattern` (default `t/<ticket-id>-<slug>`, must contain `<ticket-id>`), `pitfalls` (the body of the CLAUDE.md pitfalls section: a string or a list of lines), `vcs_host` (`github|gitlab|none`, default `github`), `base_branch` (default `main`), `parallel_mode` (`worktree|none`, default `worktree`), `no_unrequested_commits` (bool: agents never commit unless asked). Command values (`test_cmd`, `lint_cmd`, `tickets_check_cmd`, `tickets_index_cmd`) are one bare command: no backticks and no explanation in parentheses (exit 2 otherwise); put the explanation elsewhere. Secrets in the answers are refused (exit 2), never put any there.

1. `myvec scaffold --answers <file> --root <folder> --dry-run --json`, show the plan to the user (created files, any `conflict`).
2. No conflicts: `myvec scaffold --answers <file> --root <folder>`. Exit 3 = conflicts and nothing was written; in an empty folder this should not happen. If it does, show the list and ask before using `--overwrite PATH` (one flag per file).

## 3. Connect

1. `git init` if the folder is not a repository.
2. `myvec init --name <name>`, then `myvec mode <auto|always|off>`. On `MarkerRewriteRefused` stop and tell the user; do not edit the marker.
3. Hooks and daemon once per machine, as in step 5 of the main flow.
4. `myvec ticket check --strict`.
5. First commit with an explicit list of files, NOT `git add -A` and never whole directories: exactly the paths printed by `myvec scaffold --json` (actions create/merge/overwrite, including `.myvec/scaffold.json`, the process version and answers state) plus `.myvector.toml` (a global gitignore may hide it: add it with `git add -f .myvector.toml`). Never add `.claude/settings.local.json` (personal permissions Claude Code may have created in this session). Message: `chore: bootstrap process (myvec scaffold)`.

## 4. GitHub (separate question)

Ask in its own `AskUserQuestion`, recommended answer "No, later". Only on an explicit "yes": `gh repo create <name> --private --source . --push`. Never public.

## 5. Report

Created files, branch and commit, the MyVector project path, the UI link http://127.0.0.1:7878, next step: plan the first ticket (`myvec ticket new --parent <group>`).

The owner's personal rules (effort, models, questions only through `AskUserQuestion`, browser extension) live in the global `~/.claude/CLAUDE.md`; they are not copied into the project `CLAUDE.md`. Project skills are not generated; adding one later is a manual step.

## Existing code

A folder with code (maybe with a marker, an old process of its own, tickets in another format, a dirty git tree). Facts come from the CLI, decisions from you; the same `myvec scaffold` renders the files. Agents, rules and CLAUDE.md are in English also for an existing project; tickets stay in the project's language and are not rewritten.

1. `myvec scaffold --detect --json --root <folder>`. Show the draft in one block: stack, tests, linter, UI, marker, process, tickets (`none|canon|foreign`), git (branch, `dirty`). Detection reads only a whitelist of files, never `.env*` or `.dev.vars`.
2. Short interview, by the rules of the rounds above: ask only the `unknown` items (`test_cmd` with its `candidates`, `ui` - `url_guess` is only a hint, `description` - `readme_title` is only a hint) plus the project's own differences when detected (ADR path, parallel-line limit, branch pattern, pitfalls) and always the git rules: hosting (`vcs_host`: GitHub / GitLab / no remote), base branch (`base_branch`: from `git` facts, e.g. `main` or `dev`), parallel lines in worktrees or not (`parallel_mode`), and whether agents may commit on their own (`no_unrequested_commits`). Do NOT ask for the first ticket group and do not pass `first_group`: an existing project keeps its own tickets and the scaffold creates no group. Language of tickets is the project's own language, taken from `docs_language_detected` and always confirmed with a question: for `uk`/`ru` ask "<detected> (Recommended) / English", for `en` just confirm, for `mixed` ask without a recommendation (`none`: ask, recommend English). Pass the choice as `tickets_language` (`en|uk|ru`) in the answers; the section names in tickets (`sec_*` slots) and the INDEX language follow `myvec ticket migrate` (E5-01), until then leave the slots as they are. Process documents stay English.
3. `myvec scaffold --answers <file> --root <folder> --dry-run --diff --json`. For every `conflict` ask one `AskUserQuestion` (up to 4 files per call): "keep mine (Recommended for CLAUDE.md)" / "take the template (.bak is kept)" / "append the process section (CLAUDE.md only)". For an agent/rule file that differs: recommend the template if the diff shows no project-specific rules, otherwise keep mine and offer to move those lines into `slots`. Never overwrite without an answer. Then run without `--dry-run` with `--keep PATH` (also for a `create` row: the file is then not created) / `--overwrite PATH` / `--append CLAUDE.md` per answer (one flag per file). Order that is safe: show the diff, get the answers, and run at once; do not edit files in between (and repeat `--dry-run` if time passed). The guard is only inside one process: plan and apply in the same run compare fingerprints, and exit 3 = the folder changed between them (or an unresolved conflict), nothing was written. A change made between a separate `--dry-run` and the run is NOT detected: `--overwrite` replaces it and leaves only `.bak-<ts>`.
4. `tickets: foreign` → the scaffold already skips `docs/todo/**` (`skip-foreign`) and does not plan the first group; pass `--no-tickets` explicitly anyway and offer, as a separate step, `myvec ticket migrate`. `migrate --detect` and `--check` also warn (`legacy_script`: file and line only) about project scripts (package.json scripts, Makefile, `scripts/*`) that mention `tickets` and the old fields `epic`/`estimate`: tell the owner to replace them with `myvec ticket check`/`index`. `migrate --fix --write` sets the status of a group header it has to create from its children (all done → done with the latest date, any in progress → in progress, otherwise todo) and never touches a valid existing group status.
5. A marker exists → do NOT call `myvec init` (only `myvec status`). No marker → as for an empty folder (section 3; exclude big foreign trees, see step 3 of SKILL.md).
6. Git: `dirty > 0` → warn how many files are dirty and that other people's changes are not committed. If `dirty_scaffold_paths` is not empty, those files are NOT put into the automatic commit (just say so). Commit with a pathspec: `git add -- <paths>` then `git commit -m "chore: bootstrap process (myvec scaffold)" -- <paths>` (staged foreign changes stay out). Paths: only actions create/merge/overwrite/append from `--json` (not keep/same/skip; this includes the `.myvec/scaffold.json` row), plus `.myvector.toml` if it was just created. If the current branch is `main`/`master` and the tree is dirty, ask (recommended "yes") whether to commit on a separate branch `chore/myvec-process`.
