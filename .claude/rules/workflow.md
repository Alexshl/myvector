# Workflow

## Roles

The main session (orchestrator) plans, decides, verifies. Agents (`.claude/agents/`): planner (opus), coder, tester, docs-writer, debugger, qa (sonnet), reviewer (opus, read-only). Every agent reads `docs/lessons.md` and the ticket file, appends a report to the ticket's `## Log` and returns to the orchestrator a status of up to 40 words: status · tests · blockers (details only in the Log). Effort no higher than medium; do not use Fable.

## Ticket pipeline

1. **planner** - steps, risks, file ownership, acceptance criteria, live check (into the ticket). The plan and the UI design are approved by the user (the orchestrator asks through `AskUserQuestion`); after that it runs autonomously, with no separate approval of each PR.
2. **coder** - implementation in the branch `t/<ticket-id>-<slug>`.
3. **tester** - tests for the diff, `uv run pytest -q`.
4. **reviewer** - independent review of `git diff main...HEAD`. Critical/Important go back coder → tester → reviewer until none is left. Minor - at discretion.
5. **docs-writer** - README/CLAUDE.md/`docs/lessons.md`/`docs/decisions.md`/`bench/REPORT.md` (if search or the model was touched); reconciles tickets and the index (`uv run python scripts/tickets.py check` and `uv run python scripts/tickets.py index`).
6. **qa** - live check (below), if the ticket requires it.

Bugs: **debugger** (root cause) → coder → tester. Small fixes (a typo, a rename, a comment) go without the pipeline.

## Live check (qa)

When the ticket touches the installed tool, the daemon, search or the UI:
1. `uv tool install --reinstall --python 3.12 .` (or `./install.sh`), then `myvec install-daemon` (restart). Without `--reinstall` uv installs the cached old build.
2. Live search in 2-3 projects: a natural-language query (uk/ru) and an identifier; `--scope all`.
3. UI: http://127.0.0.1:7878 - only the Claude in Chrome extension (`mcp__claude-in-chrome__*`), do not use the chrome-devtools MCP. Light and dark theme, narrow width (`resize_window`).
4. After changes to `filters.py`/secrets - scan the DB for PEM/AKIA/tokens. `sync already running` - wait, do not kill the daemon's processes.

The real `~/MyVector` is read-only unless the ticket requires otherwise.

## Status lifecycle

⬜ → 🟦 (the orchestrator starts the coder) → 🟨 (PR open, review/checks) → ✅ date (after merge and fact check). ⛔ - blocked (reason in Log), 🚫 - cancelled, ❄️ - frozen. Only the orchestrator changes the status, then `uv run python scripts/tickets.py index`.

Orchestrator check before done: `git diff --stat`, the output of `uv run pytest -q`, `uv run python scripts/tickets.py check`, the reports in `## Log`, for live tickets the qa report. At done the orchestrator appends the cost: `myvec ticket log ID --agent оркестратор --tokens ["what ate the most"]` (a line from the transcripts, ADR-013; agents do not count cost themselves). Tests of tester/qa are measured through `myvec measure` (rules: `skill/myvector/tickets.md`, "Test runs"); before done check that the tester's Log (and qa's, if there was one) has a `measure:` line with `exit 0`.

## Git: hybrid

- Pipeline tasks: a branch from `main` named `t/<ticket-id>-<slug>` (e.g. `t/E1-02-short-slug`), a PR via `gh pr create`; the PR body has the path to the ticket file (`docs/todo/.../E1-02-....md`), what was done, the result of `uv run pytest -q`. Before merging - review without Critical/Important and green tests. Merge (squash) and branch deletion are done by the orchestrator; the orchestrator also pushes and opens the PR.
- Small fixes without a ticket go straight to `main`.
- Several machines (Mac, devbox): `git pull` at the start of work, `git push` at the end; do not leave unpushed work.
- Agents do not commit or push themselves until the orchestrator says so.

## Parallel lines

- Up to 2 at a time, each in a worktree `.worktrees/<ticket-id>` (`git worktree add .worktrees/<ticket-id> -b t/<ticket-id>-<slug> main`). Add `.worktrees/` to `.gitignore`.
- Only tickets without overlapping "Володіння файлами" and without a dependency run at the same time. The worktree is created, merged and removed only by the orchestrator; an agent works inside its worktree.
- Tests are isolated through `MYVEC_HOME`, but the daemon and the real index are one: do not run parallel qa runs.

## Orchestrator context

The orchestrator keeps its context small: the policy is in the `/myvector` skill (`orchestrator.md`). A brief to an agent is 4 lines (ticket, worktree or branch, role, what is new); the agent reads the rules itself. The orchestrator does not read whole large files, artifacts or series of screenshots (at most one screenshot per ticket; the verdict comes from qa/reviewer) and runs quiet commands: `uv run pytest -q 2>&1 | tail -1`, `git diff --stat`. The owner gets a status only at decisions and at the end of a wave. At about 200k tokens of context (`myvec tokens --session SID --context`) write `docs/handoff.md` and continue in a new session.

## Updating the installation

Reinstalling `myvec` on the machines and restarting the daemon happen only on the user's explicit command. Agents do not touch the real `~/MyVector`, `~/.claude`, LaunchAgents/systemd.
