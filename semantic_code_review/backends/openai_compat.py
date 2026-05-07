"""OpenAI-compatible HTTP backend (groq, github, ollama, ...).

One adapter family powers every `[backends.<name>] type = "openai-compat"`
entry. The endpoint and credential are entirely config-driven.
"""

from __future__ import annotations

import typer

from ..augment.agents import Client
from .base import Backend, resolve_api_key


class OpenAICompatBackend(Backend):
    def resolve(self, *, model: str) -> Client:
        bdef = self.bdef
        if not bdef.base_url:
            raise typer.BadParameter(
                f"--backend={self.name} (type=openai-compat) has no base_url; "
                f"set [backends.{self.name}] base_url in config."
            )

        if bdef.api_key_env or bdef.api_key_command:
            api_key = resolve_api_key(self.name, bdef)
        else:
            # Local servers (Ollama, llama.cpp) typically require *some*
            # non-empty bearer; use a sentinel that's clearly not a key.
            api_key = "not-needed"

        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return Client(
            model=OpenAIChatModel(
                model_name=model,
                provider=OpenAIProvider(base_url=bdef.base_url, api_key=api_key),
            ),
        )


__all__ = ["OpenAICompatBackend"]
