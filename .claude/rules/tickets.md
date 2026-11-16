# Tickets in this repo

The format canon (frontmatter, statuses, sections, Log, inbox, `myvec ticket` commands) is in [skill/myvector/tickets.md](../../skill/myvector/tickets.md) (ADR-006). Only the repo specifics are here.

- Layout: `docs/todo/E<id>-<slug>/index.md` (a group, `kind: group`) and `docs/todo/E<id>-<slug>/E<id>-<NN>-<slug>.md`; ticket id `E<id>-<NN>`. Few groups: thematic, not finer.
- Statuses in this repo's files are emoji (`⬜`, `✅ 2026-11-09`), ADR-004.
- Check: `uv run python scripts/tickets.py check`; index: `uv run python scripts/tickets.py index`.
- Local frontmatter field `decisions: []` holds ADR ids from `docs/decisions.md`.
- Who changes what: the planner fills in the plan; `status` is changed only by the orchestrator, then `index`. Secrets and real contents of `~/MyVector`/vault do not go into tickets and logs.
