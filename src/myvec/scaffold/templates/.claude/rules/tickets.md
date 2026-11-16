# Tickets in this repo

The format canon (frontmatter, statuses, sections, Log, inbox, `myvec ticket` commands) is in {{canon_ref}}. Only the repo specifics are here.

- Layout: `docs/todo/E<id>-<slug>/index.md` (a group, `kind: group`) and `docs/todo/E<id>-<slug>/E<id>-<NN>-<slug>.md`; ticket id `E<id>-<NN>`. Few groups: thematic, not finer.
- Statuses in this repo's files are {{status_style_text}}{{status_note}}.
- Check: `{{tickets_check_cmd}}`; index: `{{tickets_index_cmd}}`.
- Local frontmatter field `decisions: []` holds ADR ids from `{{adr_path}}`.
- Who changes what: the planner fills in the plan; `status` is changed only by the orchestrator, then `index`. Secrets and {{private_data}} do not go into tickets and logs.
