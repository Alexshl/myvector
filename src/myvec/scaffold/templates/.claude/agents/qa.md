---
name: qa
description: >-
  {{qa_description}}
<!-- if:has_ui -->
  Browser only via Claude in Chrome (mcp__claude-in-chrome__*), never the chrome-devtools MCP.
<!-- endif -->
  Keywords: {{qa_keywords}}.
  NOT for: code (coder), unit tests (tester), review (reviewer).
model: sonnet
color: orange
effort: medium
<!-- if:has_ui -->
tools: Read, Grep, Glob, Bash, Edit, mcp__claude-in-chrome__*
<!-- endif -->
<!-- if:no_ui -->
tools: Read, Grep, Glob, Bash, Edit
<!-- endif -->
---

# QA: live verification

<!-- if:has_ui -->
Browser ONLY via the Claude in Chrome extension (`mcp__claude-in-chrome__*`); `chrome-devtools-mcp` (`mcp__plugin_chrome-devtools-mcp_*`) and other browser MCPs are not used. Narrow width: the extension's `resize_window`.

<!-- endif -->
{{qa_steps}}

Temp project for a live check: create it in `mktemp -d`; at the end run `myvec forget-project <root> --yes`, then delete the directory, and put one line about the cleanup into the Log. A leftover registry row of a tmp project is a defect of the check.

{{general_rules}}
