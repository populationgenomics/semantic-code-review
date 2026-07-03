"""The `scr` command-line interface.

This module owns the Typer `app` and the top-level `_main` callback.
Each command lives in its own module under `cli/`; importing them at
the bottom of this file triggers their `@app.command()` decorators so
they register against `app`. Shared helpers — config loading, backend
selection, prompt resolution, logging setup — live in `_shared.py`.
"""

from __future__ import annotations

import typer

from ._shared import _reset_config_cache, get_config, load_dotenv

app = typer.Typer(
    help="Semantic Code Review — LLM-augmented PR diff viewer.",
    # Typer's default rich tracebacks are noisy for end-users. Plain
    # Python tracebacks still print on unexpected errors; expected ones
    # (missing key, claude not logged in) are surfaced as short messages.
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version

        typer.echo(version("semantic-code-review"))
        raise typer.Exit


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """Semantic Code Review — LLM-augmented PR diff viewer."""


load_dotenv()


# Importing the command modules below triggers their @app.command()
# decorators, which is what actually registers each command with the
# Typer app above. Order is irrelevant. The `noqa: F401` markers
# acknowledge the imports are intentionally side-effect-only.
from . import (  # noqa: E402  # side-effect imports must follow the `app` definition above
    augment,
    config_cmd,
    fetch,
    lint,
    pr,
    review,
    runs_cmd,
    show,
    strip,
)

__all__ = ["_reset_config_cache", "app", "get_config"]


if __name__ == "__main__":
    app()
