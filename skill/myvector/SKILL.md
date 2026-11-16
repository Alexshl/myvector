---
name: myvector
description: "Local semantic search + two-level memory (global and per-project) over the user's code projects via the `myvec` CLI. Use when the user types /myvector or 'MyVector' (set up the current folder). In any folder with a .myvector.toml up the tree: use `myvec search` FIRST for questions about the project ('де ми робили…', 'где у нас…', 'як влаштовано…', 'how is X implemented', 'що ми вирішили про…', 'чому так зроблено') before broad grep or reading many files; and save durable decisions/facts with `myvec remember` on your own initiative. Also when working with tickets in `docs/todo` ('тікет', create/update a ticket). Not for exact strings you already know — use rg for those."
---

# MyVector

`myvec` = local index (SQLite + sqlite-vec + FTS5, embeddings `embeddinggemma` via Ollama) + memory as Markdown notes in the Obsidian vault `~/MyVector/vault/` (a git repo, source of truth). A folder with `.myvector.toml` is a project; nested git repos are its sub-repos (`project-b/site`). The index is branch-aware. A daemon (`myvec serve`, http://127.0.0.1:7878) keeps active projects in sync and shows status, journal, memory and search in a web UI.

## /myvector — set up the current folder

1. `command -v myvec` — if missing: `uv tool install --reinstall --python 3.12 ~/projects/myvector` (always `--reinstall`: the version number does not change between commits, and without it uv reuses a cached old build). `ollama list` must contain `embeddinggemma`; else `ollama pull embeddinggemma`.
2. Three cases. (a) `.myvector.toml` in cwd or above AND `.claude/agents` present → `myvec status`, report in 2–3 lines and add one phrase: "the process is already here; to bring it to the current template say 'update the process'" - no questions; on that request follow the section "Update the process / convention" below. (b) An EMPTY folder (no files except hidden ones) → read `bootstrap.md` and follow it instead of steps 3–6 below. (c) A folder with code (marker or not) and no `.claude/agents` → `bootstrap.md`, section "Existing code".
3. Otherwise ONE AskUserQuestion call (detected defaults first, marked Recommended):
   - Project name (folder name; or `<parent>/<folder>` if a parent marker exists above).
   - What to index: all git repos found (list them with branches: `find . -maxdepth 4 -name .git -type d`) / exclude some (user types names — typical: copies, backups, generated dirs).
   - Background sync mode: Авто (active 2 h after any use) / Завжди / Вимк.
4. `myvec init --name … [--exclude …]` then `myvec mode <auto|always|off>`. Indexing runs in the background (`myvec status` shows progress; ~35–60 chunks/s).
5. Once per machine — if `~/.claude/settings.json` has no `myvec hook` entries or `launchctl list | grep com.myvec.serve` is empty, ask once (AskUserQuestion), then `myvec install-hooks` and `myvec install-daemon`.
6. Report: project path, repos + branches, excluded dirs, mode, UI link http://127.0.0.1:7878.

## Update the process / convention

Triggers: "онови конвенцію/процес", "подтяни новый процес", "підтягни новий процес", "update the process". The project updates itself, no vector-local orchestrator needed.

1. `myvec scaffold --state --json`: `none` → there is no state: for a full process read `bootstrap.md` next to this file, section "Existing code" (it creates the state), then stop; if the owner only wants the search rule, `myvec scaffold --adopt --json` (plan), then `--adopt --write` after consent (writes only `.claude/rules/search.md`, no state; `update` = the file is an unchanged older myvec render, `--write` refreshes it and keeps a `.bak-<ts>`; `foreign` = owner edits or the project's own file, left as is, tell the owner to merge by hand), then stop; `newer` → tell the owner to update myvec; `invalid` → show `reason`, stop; `current` → say "already up to date", stop; `outdated` → continue.
2. `myvec scaffold --update --json` (exit 0 or 3; it writes nothing without `--write`). Show the plan in a few lines: `update`/`merge`/`create` files, `conflict` files, `orphan` (left on disk, dropped from the state). Never print `diff_withheld` files' content.
3. For every `conflict` (up to 4 per `AskUserQuestion` call) ask once: "merge by hand (Recommended when both changes matter)" / "take the template (a .bak-<ts> stays)" / "keep mine". By hand = the owner edits the file, then it goes in with `--keep PATH`. A `conflict` on the project's own `.claude/rules/search.md` (the rule is new in process v3): recommend "keep mine" (`--keep .claude/rules/search.md`) unless the diff shows it lacks the `myvec search` rule. For a conflict row with `"mode": "keep"` (a file the owner chose to keep at bootstrap) recommend "keep mine" instead: `--keep PATH` also moves the base, so the question is not repeated until the template changes again. A `keep` row is a file the owner never created or deleted: do not recreate it unless asked (`--overwrite PATH` creates it).
4. At once, with no edits in between: `myvec scaffold --update --write [--overwrite PATH ...] [--keep PATH ...]`. Exit 3 = a conflict is left or the folder changed since the plan: repeat from step 2. Conflict markers are never written.
5. Run the project tests (`test_cmd` from the saved answers) and `myvec ticket check`.
6. Commit with a pathspec, not `git add -A`: only the paths in `write` of the `--write --json` run (include `.myvec/scaffold.json`), never `*.bak-*` or `.claude/settings.local.json`: `git add -- <paths>` then `git commit -m "chore: update process (myvec scaffold --update)" -- <paths>`.

## Search

- `myvec search "<question in any language>"` → 10 hits. When stdout is not a terminal (an agent's Bash) the output is compact: one `path:start-end symbol` line per hit, no snippets; in a terminal each hit has a score and the top 3 are expanded. `--expand` forces snippets, `--compact` forces the short form, `--full` prints whole chunks. Read only those line ranges, not whole files.
- Several questions in one call: `myvec search -q "first" -q "second"` (up to 5; blocks `# 1/2`, `# 2/2`; `--json` gives a list of `{query, results}`).
- A one-word query (an identifier, at least 3 characters, not `--kind memory`) ends with `total in index: N lines in M files (word/prefix matches) · all: rg -n -F -- '<q>' <root>` (a freshness note may follow): a count of word/prefix matches; matches in the middle of a longer word are not counted, so run the `rg` command when you need every occurrence.
- Natural-language query → vector route; a single identifier (`calcVatEu`, `vat_rate`, `Foo::bar`) → hybrid route. Override with `--mode vector|hybrid`.
- `--scope all` searches other projects too; `--kind memory` searches only decisions/notes.
- A note `(індекс оновлюється у фоні)` means the branch or files changed; results may lag a few seconds.

## Docs: what we decided and why

Questions about decisions, reasons or where something is described ("що ми вирішили щодо…", "чому так зроблено", "де описано…", "что решили…", "почему не…", "what did we decide", "why", ADR, ticket) → run `myvec search --kind docs "<the question in words>"` first. It searches only documentation files (`.md`, `.markdown`, `.mdx`, `.rst`, `.adoc`: README, CLAUDE.md, docs, ADRs, tickets), no code and no memory. Then Read only the found line ranges, not whole files.

- `rg`/`grep` over `.md` only for an exact string or id (`ADR-029`, `E8-03`); a ticket by id → `myvec ticket`.
- Works with `--scope all` (other projects) and `--json`.

## Cross-project: "did we do this somewhere else?"

Triggers: "чи робили ми…", "де ми вже…", "є в нас десь…", "як ми це зробили в іншому проєкті", "did we ever…", "have we done this elsewhere", "мы уже делали…", "где у нас…", "есть у нас где-то…".

1. `myvec search --by-project "<the gist, in words>"` → projects ranked by best score, with code/memory counts and 2 top hits each (`--json` available). There is no threshold: judge by the scores and the hits, a vector search always returns something.
2. Pick 1-2 projects, then drill down: `myvec search --project <name> "<refinement>"` (name or path; code and memory of that project only, no global memory).

Rules: tell the owner which projects and files the idea comes from; carry the idea over, do not copy code blindly (other dependencies and conventions); a project with `shared = false` in `.myvector.toml` is hidden on purpose - never read its files directly by path to get around it.

## Memory — write it yourself

Call `myvec remember "<1–2 self-contained sentences>"` without being asked whenever this session settles something durable: a decision and its reason, a convention, an environment or deployment detail, a gotcha that cost time, where something non-obvious lives.
- Default scope = current project; branch and commit are recorded automatically as metadata.
- `--branch` only when the fact is true only on this branch (it becomes project-wide when the branch is merged, is archived if the branch is deleted unmerged).
- `--global` only for facts true across all projects: the user's preferences and working style, shared machines/servers/access (no secrets), tooling and process identical everywhere (myvec, cm, uv, agent rules). Anything that mentions the code, paths, architecture, a decision or deployment of one project stays in that project.
- Before `remember` in a new project: `myvec search --kind memory --scope all "<the gist>"`. A project memory of another project that is true everywhere → `myvec promote <id>`, do not duplicate it.
- Do not `remember` what the project's `CLAUDE.md` already says (it is imported into memory by itself); imported memories are read-only, `promote` refuses them.
- Never store code, secrets, passwords, tokens, or what git history already says. If myvec reports it updated a similar memory — that is dedup, fine.
- Outdated → `myvec forget <id>` (archives). Important → `myvec pin <id>`. Cross-project → `myvec promote <id>`.
- Composition check: `myvec memory-audit` (aggregates only). `--candidates` lists per-memory signals - for the owner, locally.

## Tickets

If the project has `docs/todo` with tickets: read `tickets.md` next to this file before creating or editing a ticket.
- Create with `myvec ticket new`; append to Log only with `myvec ticket log`.
- Only the orchestrator changes `status` and processes `docs/todo/inbox/`.
- Run `myvec ticket check` after edits.
- Old format (another header shape, a single `todo.md`): `myvec ticket migrate --detect`, then `--brief` for the agent (see `tickets.md`, "Old format").
- Tester/qa run tests via `myvec measure --ticket ID --agent ROLE -- <cmd>` and paste its `measure:` line into the Log (see `tickets.md`, "Test runs").

## Orchestrating

If the project has `.claude/agents` and you are the main (orchestrator) session: read `orchestrator.md` next to this file (4-line briefs, reports up to 40 words, quiet commands, handoff at about 200k tokens of context). Projects without `.claude/agents`: do not apply it.

## Troubleshooting

- `model/dims mismatch` → `myvec reembed`.
- `sync already running` → another session or the daemon is syncing.
- Stale results → `myvec sync` (foreground), retry.
- Daemon down → search still works directly; `myvec install-daemon` restarts it. Logs: `~/MyVector/logs/`.
