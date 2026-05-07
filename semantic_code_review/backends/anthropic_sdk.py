"""Anthropic SDK backend (`claude-api`).

Reaches the Anthropic API via pydantic-ai's `AnthropicModel`. The
provider is constructed with an explicit `api_key` so the resolver
does not mutate `os.environ` — the key only lives on the model.
"""

from __future__ import annotations

import os

import typer

from ..augment.agents import Client
from .base import Backend, resolve_api_key


class AnthropicSdkBackend(Backend):
    """`claude-api`: Anthropic SDK via pydantic-ai."""

    auto_priority = 0

    def resolve(self, *, model: str) -> Client:
        api_key = self._resolve_key()
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return Client(
            model=AnthropicModel(
                model_name=model,
                provider=AnthropicProvider(api_key=api_key),
            ),
        )

    def supports_auto(self) -> bool:
        try:
            self._resolve_key()
        except typer.BadParameter:
            return False
        return True

    def _resolve_key(self) -> str:
        bdef = self.bdef
        if not (bdef.api_key_env or bdef.api_key_command):
            # Backwards-compat: a builtin without explicit env/command
            # still expects ANTHROPIC_API_KEY in the parent env.
            v = os.environ.get("ANTHROPIC_API_KEY")
            if not v:
                raise typer.BadParameter(
                    f"--backend={self.name} but $ANTHROPIC_API_KEY is not "
                    "set (load a .env or export the variable)."
                )
            return v
        return resolve_api_key(self.name, bdef)


__all__ = ["AnthropicSdkBackend"]
