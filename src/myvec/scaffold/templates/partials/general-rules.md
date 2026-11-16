## General rules (all agents)

- Start with `docs/lessons.md`, `CLAUDE.md` (section {{pitfalls_section}}) and the ticket file (the path is given by the orchestrator); rules: `.claude/rules/workflow.md`, `.claude/rules/tickets.md`.
- When done, append your report to the ticket's `## Log`: date, agent, what was done, files, results (commands and numbers).
- Return to the orchestrator ONLY a status of up to 40 words: status · tests · blockers. Details go only in the Log.
- Effort no higher than medium. Do not use the Fable model. Do not ask the user questions (you have no `AskUserQuestion`): put open questions into the status to the orchestrator.
- Do not change the ticket status (frontmatter `status`): only the orchestrator does.
- Do not touch the real {{protected_paths}}. Do not print secrets (tokens, keys, `.env*` contents) or put them in tickets, logs or reports.
- Project check: `{{test_cmd}}`; tickets: `{{tickets_check_cmd}}`.
- Search: `myvec search` first for code, docs and tickets (`--kind docs`, `--by-project`); grep/rg/find only when myvec is unavailable, for exact lines in files the search already found, or for a full count; details `.claude/rules/search.md`.
