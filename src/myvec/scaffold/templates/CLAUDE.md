# {{project}}

{{description}}

## Stack and commands

- Stack: {{stack}}
- Tests: `{{test_cmd}}`
<!-- if:has_lint -->
- Lint: `{{lint_cmd}}`
<!-- endif -->
<!-- if:has_ui -->
- Live UI check: {{ui_url}} - {{ui_check}}
<!-- endif -->
<!-- if:has_machines -->
- Machines: {{machines}}
<!-- endif -->

## Pitfalls

Non-obvious things that cost time. Keep this a short list; details and history go to `docs/lessons.md`.

{{pitfalls}}

## Development process

Details: `.claude/rules/workflow.md` (pipeline, git, parallel lines) and `.claude/rules/tickets.md` (tickets). Agents: `.claude/agents/`. Tickets: `docs/todo/` (`INDEX.md`), check `{{tickets_check_cmd}}`, index `{{tickets_index_cmd}}`.

- Search: MyVector first (`.claude/rules/search.md`); pass this rule into every agent brief.
- The main session is the orchestrator: it plans, decides, verifies by facts (`git diff --stat`, the output of `{{test_cmd}}`) and delegates the rest to agents. Pipeline: planner → coder → tester → reviewer → docs-writer; bugs: debugger → coder → tester; live check: qa. Small fixes go without the pipeline.
- Git (hybrid): pipeline tasks get a branch `{{branch_pattern}}` and {{claude_review}}, small fixes go straight to `{{base_branch}}`.
<!-- if:is_worktree -->
- Parallel lines: up to {{parallel_lines}}, in worktrees `.worktrees/<ticket-id>`; created and merged only by the orchestrator.
<!-- endif -->
<!-- if:no_worktree -->
- Parallel lines: none, no worktrees; work runs in the current branch, one ticket at a time.
<!-- endif -->
<!-- if:no_unrequested_commits -->
- Nobody commits, pushes, merges or opens {{pr_a}} without an explicit request from the user.
<!-- endif -->
- The user approves only the plan and the UI design; after that the ticket runs autonomously, {{pr_plural}} are not approved separately.
- The ticket status is changed only by the orchestrator. Agents: effort no higher than `medium`, do not use Fable, questions to the user only through `AskUserQuestion` (sub-agents have none, they return the question to the orchestrator).
- Secrets and private user data are not copied into tickets, {{pr_plural}}, logs or agent reports.
- Lessons and pitfalls: `docs/lessons.md` (read first); the "Pitfalls" section above stays a short list.
