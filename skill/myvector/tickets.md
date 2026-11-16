# MyVector tickets: canon v1

Loaded on demand from SKILL.md. Parser: `myvec.tickets`; CLI: `myvec ticket`. Section names in ticket bodies: see "Body" (Ukrainian, Russian and English).

## Layout

- Tree root: `docs/todo` (or `--dir` / `[tickets] dir` in `.myvector.toml`). Files: `INDEX.md`, `ticket-template.md`, tickets.
- Folder layout is free. The tree is defined by `parent` (any depth); a section is a ticket with `kind: group`.
- `inbox/` is outside the tree (not parsed), see Inbox.
- Generated parts of `INDEX.md` sit between `<!-- generated:start -->` and `<!-- generated:end -->`; never edit by hand, run `myvec ticket index`.

## Frontmatter

One `---` block of flat YAML, fields in this order:

```yaml
id: E1-01
title: Short title
kind: ticket       # ticket or group
status: todo       # see Statuses
parent: E1         # id of the group
size: S            # S, M or L
deps: []           # ids this ticket depends on
blocks: []         # ids waiting for this one (symmetric to deps)
branch:
pr:                # number or link
summary:
updated: 2026-11-09
```

- `deps`/`blocks` are symmetric: A.deps has B <=> B.blocks has A (`ticket new --deps` does it).
- Legacy `epic:` is read as `parent` with a warning. Other keys are project-local fields (e.g. `decisions`).

## Statuses

| key | emoji | meaning |
|---|---|---|
| todo | ⬜ | not started |
| in_progress | 🟦 | being worked on |
| review | 🟨 | PR open, review or checks |
| waiting | ⏳ | waiting for something |
| blocked | ⛔ | blocked (reason in Log) |
| partial | 🟧 | partly done, rest postponed |
| done | ✅ | finished, with date |
| cancelled | 🚫 | cancelled |
| frozen | ❄️ | frozen |

`done` carries a date: `✅ 2026-11-09` or `done 2026-11-09`. Keys and emoji are equal; keep one form per project.

Percent: done 100, todo 0, cancelled is left out of the average, the rest is the share of checked Acceptance criteria boxes; a group is the plain average of its children. In `INDEX.md` the percent is therefore weighted (a ticket in progress adds the share of its checked boxes), while the counter `(k/n done)` next to it counts only finished leaves: `75% (0/1)` is not a mismatch. The overall line has the same form: `Overall progress: 40% (3/7)`.

## Body

Sections (see `ticket-template.md`). Section names come in three languages; either works, mixing them in one ticket is fine. The parser reads only the goal, `Acceptance criteria`, `Log` and `Comments` (the rest is for people and agents). The goal heading is recognized in all three languages; the language of `INDEX.md` follows the project's `ticket-template.md` (`## In one pass` -> English, `## За один заход` -> Russian, otherwise Ukrainian).

| Ukrainian | Russian | English |
|---|---|---|
| За один захід | За один заход | In one pass |
| Контекст | Контекст | Context |
| Рішення — кроки | Решение — шаги | Plan - steps |
| Володіння файлами | Владение файлами | File ownership |
| Acceptance criteria | Acceptance criteria | Acceptance criteria |
| Перевірка наживо | Проверка вживую | Live check |
| Log | Log | Log |
| Comments | Comments | Comments |

New projects (`myvec scaffold`) get the English names; this repo keeps the Ukrainian ones.

`## Comments` is optional, before `## Log`, append-only, written by the orchestrator: `### YYYY-MM-DD HH:MM author (source)` (time optional) + the text as is.

## Log

Append-only. Add entries only with `myvec ticket log ID --agent ROLE "text"`: it masks secrets, refuses keys/tokens and updates `updated`. Never edit old entries by hand.

Token usage: agents do not count or write it. When marking a ticket ✅ the orchestrator runs `myvec ticket log ID --agent оркестратор --tokens ["what ate the most"]`: the entry starts with one line from the Claude Code transcripts (`tokens: <total> · output <n> · <agent> <share>% · <models> · <effort> · запусків <n>`, ADR-013), then the optional conclusion. Tester and qa paste test-run measurements as described in "Test runs" below.

### Test runs

Tester and qa run every test run whose result goes into the report like this: `myvec measure --ticket ID --agent ROLE [--label TEXT] -- <test command>` (`--label` tells runs apart: `full`, `node`, `rerun`).

- `myvec measure` prints one line to stderr; paste it **as is** (one per run) into the Log entry (`myvec ticket log ID --agent ROLE "..."`) and into the status to the orchestrator. Example: `measure: uv run pytest -q · 34,5 с · CPU 23,9 с · 0,7/1,3 ядра · 138 МБ · exit 0`. The exit code of `measure` is the exit code of the command.
- Do not pipe the output through `| tail` without `set -o pipefail` (the exit code is lost); do not cut stderr (`2>/dev/null`).
- `measure` writes to the REAL run journal of the machine (`~/MyVector`): use it only for real test runs of the ticket, never for `true` or experiments.
- Where `myvec` is not installed or is old (`myvec measure -- true` fails; do not try it for nothing, check `myvec measure --help`), use `uv run myvec measure ...`; if that is impossible too, run the tests without the wrapper and write "measure unavailable" in the Log (the passive detector may still catch a run of 5 s or more).
- Small single-test runs while debugging (coder, debugger) need no wrapper: the passive detector sees them. Never nest wrappers (`measure -- measure ...`).

## Inbox

`docs/todo/inbox/<YYYY-MM-DD>-<id>-<HHMMSS>[-n].md` in the main checkout (ADR-019). Add the line `docs/todo/inbox/` to the project's root `.gitignore` (then the watcher stays quiet); the node also writes `inbox/.gitignore` with `*`, so git and the index never see the files. Agents and nodes do not edit existing files; the UI (`POST /api/tickets/<project>/ticket/<id>/inbox`) only creates them. Format: flat YAML front matter, then the text:

```
---
ticket: E3-01
kind: comment            # comment | status | lesson (lesson: ticket is the fixed `lesson`, plus `from: <project>`)
requested_status: review # only for kind: status; a status key, not an emoji
created: 2026-11-10T15:31:00+02:00
node: devbox
source: ui               # ui | cli (cli: `myvec ticket lesson`)
---

Text of the comment (may be empty for kind: status).
```

Limits (enforced by the node): text <= 4000 chars, no control characters; request body <= 32 KB; at most 50 pending files; a hard secret is refused, literals are masked. A file is created once (`O_EXCL`), never overwritten.

At the start of a turn the orchestrator moves each file into the ticket's `## Comments` (`### YYYY-MM-DD HH:MM author (ui)` + text) or Log, deletes the file, and for `kind: status` changes the status itself (for `done` it sets the date). The UI shows "надіслано" while the file exists and "оброблено" once it is gone.

Notification (ADR-022): the session hooks (`session-start`, and `session-event` on UserPromptSubmit) print `[myvec] inbox: N new in <dir> (E3-01 comment, E2-04 status->done)` into the context at session start and when new files appear; only ids, kinds and the requested status, never the comment text. Only in the main checkout (a linked worktree and sub-agents get nothing). Only the orchestrator handles it, at the start of its turn: read the file, move the text into `## Comments` (`### YYYY-MM-DD HH:MM author (ui)`) or `myvec ticket log`; for `kind: status` change the status (orchestrator only; `done` with the date) and run `myvec ticket index`; delete the file. Child sessions and agents do not process inbox files and never delete them.

## Commands

- `myvec ticket new --title T --slug s [--parent ID] [--kind ticket|group] [--size S|M|L] [--deps A,B]`
- `myvec ticket check [--strict]` (health advice is printed but does not fail `--strict`)
- `myvec ticket index`
- `myvec ticket log ID --agent ROLE "text"` (orchestrator at ✅: `--tokens ["conclusion"]` adds the usage line; `myvec tokens --ticket ID --summary` prints it)
- `myvec ticket claim ID [--release]`
- `myvec ticket lesson --title T [--to NAME] [--from NAME] -` (a general lesson from stdin into another project's inbox, `kind: lesson`; see orchestrator.md, "Lessons back-channel")
- `myvec ticket migrate [--dir D] [--detect [--json] | --brief [--out FILE] | --check | --fix [--write] [--add-goal-heading] | --split [--write]]`, `myvec ticket index [--index-name N]` (old format -> canon, see "Old format")

A session is tied to a ticket by branch `t/<id>-<slug>` or by `claim`.

## Health advice

`myvec ticket check` prints advice with the `HEALTH` tag and counts it separately ("K порад"). It never fails `--strict` and never blocks `index`. Leaf tickets in todo/in_progress/review/waiting/blocked/partial only (groups and done/cancelled/frozen are skipped). The UI/API also adds `no_session`. The texts of `check` (advice and the summary line) follow the language of the project's tickets (see the table above; Ukrainian by default).

- `too_big` (size L): split into smaller tickets.
- `no_ac`: add Acceptance criteria checkboxes.
- `stale`: todo not updated for 30 days, in_progress/review for 7: refresh, freeze or cancel.
- `no_session` (UI only): in_progress for 1+ day with no live session: start a coder session or move the status back.
- `blocked_ready`: blocked but all deps are done: unblock (orchestrator changes status).
- `dep_cancelled`: a dep is cancelled: fix `deps` or cancel/replan the ticket.

## Who changes what

The planner fills the body; `status` is changed only by the orchestrator, then `index`. Secrets and vault contents never go into tickets or logs.

## Old format

Headers of another shape, tables or a single `todo.md` are not shown by the tracker. `myvec ticket migrate` (ADR-024) is deterministic and read-only except `--fix --write` and `--brief --out`:

- `--detect [--json]`: finds candidates (`docs/todo` anywhere up to depth 4, `docs/todo*.md`, `todo.md`; skips `node_modules`, `vendor`, `.worktrees`, marker `exclude`, deny-list) and reports format (`canon`, `near_canon`, `frontmatter`, `single_file`, `notes`), counts, status form, section language, git root. Header keys, status values and section names only; ticket text is never read into the report.
- `--brief [--dir D] [--out FILE]`: a brief for the project's agent with the field map, the status map (`?` = the owner decides) and the rules below.
- `--check [--dir D]`: what is left (`not_ticket`, `legacy_epic`, `legacy_group`, `no_goal`, `mixed_status_form`, `leftover_todo`, `check` errors); exit 0 only when nothing is left. `--detect` and `--check` also warn with `legacy_script` (file and line only, never contents) about project scripts (`package.json` scripts, `Makefile`, `scripts/*`) that mention `tickets` and the old fields `epic`/`estimate`: replace them with `myvec ticket check`/`index`. These warnings do not change the exit code.
- `--fix [--dir D]`: dry-run of the mechanical, lossless header fixes (`epic` -> `parent`; `id`/`kind: group`/`status` for synthetic groups `index.md`, whose status comes from the children: all done -> done with the latest date, any in progress/review -> in progress, otherwise todo; an existing group status is never touched); `--fix --write` applies them, bodies stay byte-for-byte, a second run prints `nothing to fix`. It also assigns `id` to headers without one (numeric prefix of the file name, else the file name; `<group>-NN` only on a collision, then rename the file by hand), inserts keys only at key boundaries (multi-line values stay whole), and `--add-goal-heading` (the only option that changes bodies) inserts the goal heading. `--split [--dir FILE ...] [--write]` turns a single-file `todo.md` into a folder verbatim (dry-run by default, the old file becomes a pointer, repeating is a no-op). `ticket index --index-name N` writes the index under another name when `INDEX.md` clashes (case-insensitively) with a foreign `index.md`; the name is not remembered.

The agent rewrites the rest by the brief:

- History: one branch and one PR in the project; first a commit with only `git mv`, then the edits; never delete + create the same ticket; a split `todo.md` becomes new files and a short pointer file (text moved verbatim).
- Do not lose: existing `## Log`, comments, project-local header fields and historical sections stay; one migration line via `myvec ticket log` in the project's migration ticket.
- Keep the project's status form (emoji or keys; keys when there were none) and the language of the sections; the goal heading must be one from the table above.
- Finish: `--fix --write` -> edits -> `myvec ticket migrate --check` until green -> `myvec ticket check --strict` -> `myvec ticket index` -> PR.
