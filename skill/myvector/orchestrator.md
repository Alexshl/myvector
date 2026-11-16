# MyVector orchestrator policy

Loaded on demand from SKILL.md. Applies to the main session of a project that has `.claude/agents` (a pipeline with agents). Projects without it: ignore this file.

## Brief to an agent: 4 lines

1. Ticket file path.
2. Worktree and branch (or "main checkout").
3. Role (the agent reads `.claude/agents/<role>.md` and `.claude/rules/*` itself).
4. What is new since the ticket text (decisions, constraints). Nothing the agent can read itself.

Do not repeat the "myvec search first" rule in briefs to built-in Explore/general-purpose agents: the `SubagentStart` hook (`myvec hook subagent-start`) already injects it.

## Report from an agent: up to 40 words

Form: `status · tests · blockers`. Details only in the ticket `## Log` (`myvec ticket log`). Blockers must not be dropped to fit the limit.

## What the orchestrator does not read

- Whole large files, generated artifacts, mockups, long logs: ask the agent or read a line range.
- Series of screenshots: at most one per ticket; the verdict comes from qa/reviewer.
- Full test output: `<test cmd> 2>&1 | tail -1`, `git diff --stat`, `git status -sb`.

## Owner status

Only at decisions that are the owner's and at the end of a wave. No intermediate reports.

## Handoff threshold

At about 200k tokens of context, write `docs/handoff.md` (state of the lines, open PRs, decisions made, what is next) and continue in a new session. Do not ask the owner to restart: if the machine has a manager session (`cm` control), send it a request to start the new orchestrator ("<project> ~ orchestrator", handoff prompt on stdin) and then stop this one.

Measure it: `myvec tokens --session <sid> --context` prints the context size (input + cache read + cache write) of the last request of the main transcript; sub-agents are not counted. It is approximate: without `--no-scan` the command scans new transcript lines first; with `--no-scan` it lags by up to ~60 s (the daemon scan interval).

## Child session per ticket

The orchestrator does not run a ticket pipeline itself: one child session per ticket runs planner → coder → tester → reviewer → docs-writer and returns one status line (ADR-026). Applies to tickets of the pipeline; small fixes go straight to `main` by the orchestrator.

**Mechanism.** The orchestrator creates the worktree `.worktrees/<ID>` (branch `t/<ID>-<slug>`) and starts the child session there with the 4-line brief plus "you are the child session of this ticket, run the pipeline from planner to docs-writer". Use `start_session` where the environment has it (report comes back by `SendMessage` to the orchestrator id; the orchestrator answers the same way). Otherwise or to resume a child, use `cm` (ADR-010): `cm start --name "<project> ~ <ID>" <worktree>` (the session list shows only the name after the machine prefix, so it must carry the project, e.g. `vector-local ~ E0-08`; the same name for `cm resume --name`; give it at start: a later `/rename` changes the address `SendMessage` uses), `cm send <id> -` (text only on stdin, never argv), `cm resume`. Exit code 4 / `busy` (a dialog or permission prompt on screen) is not bypassed. Do not describe or assume `start_session` parameters that were not checked on the machine.

**Roles.** Child: calls the agents in `.claude/agents/` (effort ≤ medium, no Fable), loops reviewer → coder until no Critical/Important, commits in its branch. The plan and other working questions go to the orchestrator (`SendMessage`), never to the owner: the orchestrator approves the plan itself. Only the UI design, money/external accounts and irreversible actions go to the owner through `AskUserQuestion` (owner decision 2026-11-11, replaces "the child approves its plan with the owner"). Child does not: change `status`, push, open a PR, merge, reinstall `myvec`, start other child sessions. Orchestrator: creates the worktree, changes status (🟦 at start, 🟨 at PR, ✅ at done), pushes, opens the PR, squash-merges, removes the worktree, checks the facts (`git diff --stat`, `pytest -q | tail -1`, `tickets.py check`, `measure:` line in Log) and writes the cost at done.

**Stops.** The child sends a status line and waits for the answer at: (a) plan ready (the orchestrator approves it, state `wait-orch`); (b) UI design; (c) ready for PR; (d) a blocker; (e) live qa needed - the orchestrator gives the qa slot (one daemon, no parallel qa); (f) child context about 200k tokens (`myvec tokens --session SID --context`): handoff into the ticket `## Log`, the orchestrator restarts it.

**Status line** (up to 40 words, one line):
`<ID> · <stage: plan|code|test|review|docs|qa|ready> · <ok|wait-orch|wait-owner|wait-qa|blocked> · tests <N passed | fail N | -> · <PR-ready|-> · blockers: <… | none>`
Details only in `## Log`. The orchestrator does not read the child transcript or `cm logs`, except a blocker (tail of at most 30 lines). Silence: check `cm who`/`cm ls`; an undelivered message is a blocker for the owner.

**At most 2 lines.** A line = a child session = a worktree. Before a start count `git worktree list` (`.worktrees/*`) and tickets 🟦/🟨; never start a third. Run in parallel only tickets with no overlap in "Володіння файлами" and no dependency.

**Rollout.** Projects get this policy through `install.sh` (the skill); their own `workflow.md` mentions it only after the templates are updated (E0-07).

## Lessons back-channel

A general lesson found in one project (process, agents, templates, `myvec`; not only that project) travels one way, "proposal -> decision", through the inbox of the lessons project (`lessons_project` in the config, default `vector-local`; ADR-027). The inbox is local to the machine: a lesson proposed on a machine is handled on the same machine.

**Source project.** Only the orchestrator proposes, after writing the lesson into its own `docs/lessons.md`: `printf '%s\n' "<Symptom / Cause / Rule>" | myvec ticket lesson --title "..." -`. Text only on stdin (never argv), one proposal = one lesson, title one line up to 120 chars. No code, data, machine paths, vault contents or secrets: a hard secret or a home/vault path is refused (exit 2), literals are masked; exit 3 = queue full (50), wait. Agents and child sessions do not propose.

**vector-local.** The notification is the hook line `lesson<-<project>` (no text). The orchestrator in the main checkout reads the file and decides: a general orchestrator policy -> this file; a process rule for projects -> a scaffold template plus the etalon fixture (ADR-021, `tests/test_scaffold_etalon.py`) via a ticket or a small fix; a lesson only for vector-local -> `docs/lessons.md` (docs-writer); otherwise reject. The decision is one commit line: `lesson from <project>: accepted -> <file>` or `lesson from <project>: rejected: <reason>`; then delete the file ("handled" = the file is gone). Child sessions and agents do not process inbox files.
