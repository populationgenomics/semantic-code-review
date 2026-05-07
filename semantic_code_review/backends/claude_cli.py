"""Claude CLI subprocess backend (`claude-cli`).

Drives `claude -p` via the SubprocessModel. No API key needed — uses
the user's Claude Code CLI session. Auto-resolution picks this when
`claude` is on PATH and no Anthropic API key is available.
"""

from __future__ import annotations

import shutil
import sys

import typer

from ..augment.agents import Client
from .base import Backend


_FALLBACK_WARNED = False


class ClaudeCliBackend(Backend):
    auto_priority = 1

    def resolve(self, *, model: str) -> Client:
        if not shutil.which("claude"):
            raise typer.BadParameter(
                f"--backend={self.name} but `claude` is not on PATH "
                "(install Claude Code CLI or set ANTHROPIC_API_KEY)."
            )
        _warn_once()
        from ..augment.cli_models import ClaudeCLIModel

        return Client(
            model=ClaudeCLIModel(model=model),
            is_subprocess_backend=True,
        )

    def supports_auto(self) -> bool:
        return shutil.which("claude") is not None


def _warn_once() -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    sys.stderr.write(
        "scr: no ANTHROPIC_API_KEY; falling back to `claude -p` subprocess. "
        "Note: no prompt caching, reduced concurrency, no in-loop repo tools "
        "(annotation quality will be lower).\n"
    )
    sys.stderr.flush()


__all__ = ["ClaudeCliBackend"]
