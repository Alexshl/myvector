# myvec (MyVector)

A local CLI that gives Claude Code hybrid (vector + full-text) search over your projects and a two-level memory (global and per-project). Everything lives in one SQLite file (SQLite + sqlite-vec + FTS5); memory is also mirrored as Obsidian-compatible markdown files, which are the source of truth.

Full documentation is in Ukrainian: [README.uk.md](README.uk.md).

## Features

- Hybrid search (embeddings + FTS5, RRF ranking) over code, docs and tickets, with scopes per project or across all projects.
- Persistent memory (`myvec remember`) kept as markdown in a git vault; the database is derived and can be rebuilt (`myvec rebuild`).
- Secret filtering: secret-like files are never indexed, literal secrets are masked.
- Background daemon (`myvec serve`) with file watchers and a local web UI and JSON API.
- Claude Code integration: skill `/myvector`, session hooks, session and token-usage tracking.
- File-based ticket tracker and project scaffolding for an agent workflow.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- An embedding backend serving `embeddinggemma` (768 dims): [Ollama](https://ollama.com) or llama.cpp `llama-server` (see `contrib/llama-embed/`)
- macOS or Linux

## Install

```bash
git clone https://github.com/Alexshl/myvector && cd myvector && ./install.sh
```

`install.sh` is idempotent (re-running updates): it installs uv, Ollama with `embeddinggemma`, the `myvec` tool, the `/myvector` skill, hooks and the daemon. Flags: `--no-daemon`, `--no-hooks`.

Manual update: `uv tool install --reinstall --python 3.12 .` (`--reinstall` is required, otherwise uv reuses a cached build).

## Quick start

```bash
cd ~/projects/my-project
myvec init --name my-project                    # writes .myvector.toml, registers and indexes in the background
myvec search "where is VAT calculated"          # code + memory
myvec remember "Deploys go through pipeline X" --pin
myvec status
```

Development: `uv sync && uv run pytest -q`.

## License

[MIT](LICENSE)
