"""Backend registry — name → adapter dispatch.

The CLI calls `get(name, config=...)` to obtain a `Backend` instance,
then `.resolve(model=...)` for the `Client` that drives the augment
pipeline. `resolve_auto(config=...)` picks a backend when the user
did not pass `--backend`.

The registry maps `BackendType` to a `Backend` subclass. Builtins
(`claude-api`, `claude-cli`, `gemini-api`, plus the
openai-compat presets) auto-register at import time; user-defined
`[backends.<name>]` entries reuse the same dispatch by virtue of
sharing a `BackendType`.
"""

from __future__ import annotations

import typer

from ..config import BackendType, ScrConfig
from .anthropic_sdk import AnthropicSdkBackend
from .base import Backend
from .claude_cli import ClaudeCliBackend
from .google_sdk import GoogleSdkBackend
from .openai_compat import OpenAICompatBackend


_HANDLERS: dict[BackendType, type[Backend]] = {
    BackendType.ANTHROPIC_SDK: AnthropicSdkBackend,
    BackendType.CLAUDE_CLI: ClaudeCliBackend,
    BackendType.GOOGLE_SDK: GoogleSdkBackend,
    BackendType.OPENAI_COMPAT: OpenAICompatBackend,
}


def get(name: str, *, config: ScrConfig) -> Backend:
    """Return the adapter registered for `name`.

    Raises `typer.BadParameter` with a list of valid choices if the
    name is unknown. "auto" must be resolved by the caller via
    `resolve_auto` first — passing it here is an error.
    """
    bdef = config.backends.get(name)
    if bdef is None:
        valid = sorted(["auto", *config.backends.keys()])
        raise typer.BadParameter(
            f"unknown backend {name!r}; expected one of: {', '.join(valid)}."
        )
    cls = _HANDLERS.get(bdef.type)
    if cls is None:
        raise typer.BadParameter(
            f"backend {name!r} has unknown type {bdef.type!r}"
        )
    return cls(name, bdef)


def resolve_auto(*, config: ScrConfig) -> str:
    """Walk registered backends and return the first name that can satisfy auto.

    Determinism: candidates are sorted by `(auto_priority, name)`.
    Each candidate's `supports_auto()` is evaluated only once, in
    that order.
    """
    candidates: list[tuple[int, str]] = []
    for name, bdef in config.backends.items():
        cls = _HANDLERS.get(bdef.type)
        if cls is None:
            continue
        priority = cls.auto_priority
        if priority is None:
            continue
        adapter = cls(name, bdef)
        if adapter.supports_auto():
            candidates.append((priority, name))
    if not candidates:
        raise typer.BadParameter(
            "No Anthropic credentials available: set ANTHROPIC_API_KEY "
            "(or ANTHROPIC_API_TOKEN in .env), install the `claude` CLI "
            "for subscription-based fallback, or pass --backend=gemini-api "
            "(Google SDK) to opt into a Gemini backend."
        )
    candidates.sort()
    return candidates[0][1]


def register_handler(btype: BackendType, cls: type[Backend]) -> None:
    """Register or replace the adapter class for a backend type.

    Intended for tests that want to swap a real adapter for a stub
    (e.g. avoid touching the network in `resolve_auto`). Production
    code should not call this.
    """
    _HANDLERS[btype] = cls


__all__ = [
    "Backend",
    "get",
    "register_handler",
    "resolve_auto",
]
