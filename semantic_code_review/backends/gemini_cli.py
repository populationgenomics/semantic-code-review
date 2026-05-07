"""Gemini CLI subprocess backend (`gemini-cli`).

Drives `gemini -p` via the SubprocessModel. Auth comes from the user's
existing `gemini` setup — env vars (GEMINI_API_KEY / GOOGLE_API_KEY)
or the OAuth flow that drops `~/.gemini/oauth_creds.json`.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import typer

from ..augment.agents import Client
from .base import Backend


_DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"


_FALLBACK_WARNED = False


class GeminiCliBackend(Backend):
    def resolve(self, *, model: str) -> Client:
        if not shutil.which("gemini"):
            raise typer.BadParameter(
                f"--backend={self.name} but `gemini` is not on PATH "
                "(install via `npm install -g @google/gemini-cli`)."
            )
        if not _has_gemini_credentials():
            raise typer.BadParameter(
                f"--backend={self.name} but no Gemini credentials found. Set "
                "GEMINI_API_KEY (AI Studio) or GOOGLE_API_KEY (Vertex), "
                "or run `gemini` once interactively to complete the "
                "OAuth flow."
            )
        _warn_once()
        gem_model = _coerce_gemini_model(model, self.bdef.default_model)
        from ..augment.cli_models import GeminiCLIModel

        return Client(
            model=GeminiCLIModel(model=gem_model),
            is_subprocess_backend=True,
        )


def _has_gemini_credentials() -> bool:
    return bool(
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or (Path.home() / ".gemini" / "oauth_creds.json").exists()
    )


def _coerce_gemini_model(model: str, default_model: str | None) -> str:
    if model.startswith("claude"):
        return default_model or _DEFAULT_GEMINI_MODEL
    return model


def _warn_once() -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    sys.stderr.write(
        "scr: using `gemini -p` subprocess backend. Note: no prompt caching, "
        "no JSON-schema-constrained output (we validate client-side and retry "
        "once on failure), reduced concurrency.\n"
    )
    sys.stderr.flush()


__all__ = ["GeminiCliBackend"]
