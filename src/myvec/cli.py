"""Console entrypoint.

Kept import-light: the `hook` subcommands must return in < 100 ms, so we
dispatch them (and `measure`) before importing typer / sqlite-vec / httpx / tree-sitter.
"""
from __future__ import annotations

import sys


def app() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "hook":
        from myvec import hooks

        sys.exit(hooks.main(argv[1:]))
    if argv and argv[0] == "measure":
        from myvec import measure

        sys.exit(measure.main(argv[1:]))
    from myvec.commands import app as typer_app

    typer_app()
