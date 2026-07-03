"""Google SDK backend (`gemini-api`).

Two transports inside one adapter, mirroring pydantic-ai's surface:

- AI-Studio (paid tier or free quota) via an explicit api_key
- Vertex via Application Default Credentials, when
  `GOOGLE_CLOUD_PROJECT` is set (no bearer token).

Vertex short-circuits credential resolution entirely — setting
`GOOGLE_CLOUD_PROJECT` is the user's signal that they want ADC.
"""

from __future__ import annotations

import os

import typer

from ..augment.agents import Client
from .base import Backend, resolve_api_key

_DEFAULT_GEMINI_API_MODEL = "gemini-2.5-pro"


class GoogleSdkBackend(Backend):
    def resolve(self, *, model: str) -> Client:
        gem_model = _coerce_gemini_model(model, self.bdef.default_model)
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        if os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return Client(
                model=GoogleModel(
                    model_name=gem_model,
                    provider=GoogleProvider(vertexai=True),
                ),
            )

        api_key = self._resolve_key()
        return Client(
            model=GoogleModel(
                model_name=gem_model,
                provider=GoogleProvider(api_key=api_key),
            ),
        )

    def _resolve_key(self) -> str:
        bdef = self.bdef
        if bdef.api_key_env or bdef.api_key_command:
            return resolve_api_key(self.name, bdef)
        for env in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            v = os.environ.get(env)
            if v:
                return v
        raise typer.BadParameter(
            f"--backend={self.name} but no Gemini credentials found. "
            "Set GEMINI_API_KEY (AI Studio), GOOGLE_API_KEY, or "
            "GOOGLE_CLOUD_PROJECT (Vertex via ADC)."
        )


def _coerce_gemini_model(model: str, default_model: str | None) -> str:
    """Substitute backend default if a Claude model id leaked through.

    Happens when `[model] default = "claude-..."` is set globally and
    the user picks `--backend=gemini-api` — the Claude id would 404.
    """
    if model.startswith("claude"):
        return default_model or _DEFAULT_GEMINI_API_MODEL
    return model


__all__ = ["GoogleSdkBackend"]
