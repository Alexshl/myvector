# Search: MyVector first

In a folder with `.myvector.toml` (in it or above) every search for code, docs and tickets goes through `myvec search "<question or identifier>"` first; this applies to the main session and to every agent. Identifiers too.

- `--kind docs` - docs, rules, ADR, tickets; `--by-project` - "did we do this in another project".
- Read only the line ranges the results point to (`--full` when the snippets are too short).
- `grep`/`rg`/`find` (and the Grep/Glob tools) only when: `myvec` is unavailable (the command is missing or fails - say so in the report); you need exact lines in files the search already found; you need completeness (a count or a list of all occurrences).
- Output: when stdout is not a terminal (an agent's Bash) `myvec search` is compact by default, one `path:start-end symbol` line per hit; `--expand` brings the snippets back, `--json` is for scripts, `--full` prints whole chunks.
- For a single identifier the output has a `total in index` line and a ready `rg` command for completeness. Pass several `-q` in one call instead of several greps.
- Pass this rule into every sub-agent brief.
- Graphify is not used.
