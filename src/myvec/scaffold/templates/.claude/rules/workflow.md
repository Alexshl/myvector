# Workflow

## Roles

The main session (orchestrator) plans, decides, verifies. Agents (`.claude/agents/`): planner (opus), coder, tester, docs-writer, debugger, qa (sonnet), reviewer (opus, read-only). Every agent reads `docs/lessons.md` and the ticket file, appends a report to the ticket's `## Log` and returns to the orchestrator a status of up to 40 words: status · tests · blockers (details only in the Log). Effort no higher than medium; do not use Fable.

## Ticket pipeline

1. **planner** - steps, risks, file ownership, acceptance criteria, live check (into the ticket). The plan and the UI design are approved by the user (the orchestrator asks through `AskUserQuestion`); after that it runs autonomously, with no separate approval of each {{pr_word}}.
2. **coder** - implementation in the branch `{{branch_pattern}}`.
3. **tester** - tests for the diff, `{{test_cmd}}`.
4. **reviewer** - independent review of `git diff {{base_branch}}...HEAD`. Critical/Important go back coder → tester → reviewer until none is left. Minor - at discretion.
5. **docs-writer** - {{workflow_docs}}; reconciles tickets and the index (`{{tickets_check_cmd}}` and `{{tickets_index_cmd}}`).
6. **qa** - live check (below), if the ticket requires it.

Bugs: **debugger** (root cause) → coder → tester. Small fixes (a typo, a rename, a comment) go without the pipeline.

<!-- if:has_live_check -->
## Live check (qa)

{{live_check_trigger}}
{{live_check_steps}}

<!-- endif -->
## Status lifecycle

<!-- if:is_emoji -->
⬜ → 🟦 (the orchestrator starts the coder) → 🟨 ({{pr_open}}, review/checks) → ✅ date (after merge and fact check). ⛔ - blocked (reason in Log), 🚫 - cancelled, ❄️ - frozen. Only the orchestrator changes the status, then `{{tickets_index_cmd}}`.
<!-- endif -->
<!-- if:is_keys -->
todo → in_progress (the orchestrator starts the coder) → review ({{pr_open}}, review/checks) → done date (after merge and fact check). blocked - blocked (reason in Log), cancelled, frozen. Only the orchestrator changes the status, then `{{tickets_index_cmd}}`.
<!-- endif -->

Orchestrator check before done: `git diff --stat`, the output of `{{test_cmd}}`, `{{tickets_check_cmd}}`, the reports in `## Log`, for live tickets the qa report. At done the orchestrator appends the cost: `myvec ticket log ID --agent {{orchestrator_name}} --tokens ["what ate the most"]` (a line from the transcripts{{token_note}}; agents do not count cost themselves). Tests of tester/qa are measured through `myvec measure` (rules: `{{canon_path}}`, "Test runs"); before done check that the tester's Log (and qa's, if there was one) has a `measure:` line with `exit 0`.

## Git: hybrid

- Pipeline tasks: a branch from `{{base_branch}}` named `{{branch_pattern}}` (e.g. `{{branch_example}}`), {{pr_via}}; {{pr_body}} the path to the ticket file (`docs/todo/.../E1-02-....md`), what was done, the result of `{{test_cmd}}`. Before merging - review without Critical/Important and green tests. Merge (squash) and branch deletion are done by the orchestrator; {{git_tail}}.
- Small fixes without a ticket go straight to `{{base_branch}}`.
<!-- if:has_remote_machines -->
- Several machines ({{machines}}): `git pull` at the start of work, `git push` at the end; do not leave unpushed work.
<!-- endif -->
- Agents do not commit or push themselves until the orchestrator says so.
<!-- if:no_unrequested_commits -->
- Project rule: nobody commits, pushes, merges or opens {{pr_a}} without an explicit request from the user; the steps above describe the flow once the user has asked.
<!-- endif -->

## Parallel lines

<!-- if:is_worktree -->
- Up to {{parallel_lines}} at a time, each in a worktree `.worktrees/<ticket-id>` (`git worktree add .worktrees/<ticket-id> -b {{branch_pattern}} {{base_branch}}`). Add `.worktrees/` to `.gitignore`.
- Only tickets without overlapping "{{sec_files}}" and without a dependency run at the same time. The worktree is created, merged and removed only by the orchestrator; an agent works inside its worktree.
<!-- endif -->
<!-- if:no_worktree -->
- No worktrees: the work runs in the current branch of the project checkout, one ticket at a time. Only the orchestrator switches branches and merges; an agent works in the branch it was given and does not touch other tickets' files.
<!-- endif -->
{{parallel_note}}

## Orchestrator context

The orchestrator keeps its context small: the policy is in the `/myvector` skill (`orchestrator.md`). A brief to an agent is 4 lines (ticket, worktree or branch, role, what is new); the agent reads the rules itself. The orchestrator does not read whole large files, artifacts or series of screenshots (at most one screenshot per ticket; the verdict comes from qa/reviewer) and runs quiet commands: `{{test_cmd}} 2>&1 | tail -1`, `git diff --stat`. The owner gets a status only at decisions and at the end of a wave. At about 200k tokens of context (`myvec tokens --session SID --context`) write `docs/handoff.md` and continue in a new session.
<!-- if:has_install_rule -->

## Updating the installation

{{install_rule}}
<!-- endif -->
