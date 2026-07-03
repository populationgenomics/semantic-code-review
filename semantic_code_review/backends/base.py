"""`Backend` protocol — the seam behind a registered backend name.

One adapter per registered backend (`name` matches the key in
`ScrConfig.backends`). The CLI resolves a name → `Backend` instance via
the registry, then asks the instance for a `Client` to hand to the
augment pipeline. Credential resolution and CLI/SDK wiring live on the
adapter — `cli.py` no longer cares about backend type.
"""

from __future__ import annotations

import shlex
import subprocess
from abc import ABC, abstractmethod

import typer

from ..augment.agents import Client
from ..config import BackendDef


class Backend(ABC):
    """One registered backend. Owns its credential and Model wiring.

    Subclasses set `auto_priority` (lower = preferred) to opt in to
    `--backend=auto` resolution; the default `None` excludes the
    backend from auto entirely. `supports_auto` is asked only when
    `auto_priority is not None`.
    """

    auto_priority: int | None = None

    def __init__(self, name: str, bdef: BackendDef) -> None:
        self.name = name
        self.bdef = bdef

    @abstractmethod
    def resolve(self, *, model: str) -> Client:
        """Return the `Client` the augment pipeline should drive."""

    def supports_auto(self) -> bool:
        """Whether this backend can satisfy `--backend=auto` *right now*.

        Default false. Subclasses that participate override.
        """
        return False


def resolve_api_key(name: str, bdef: BackendDef) -> str:
    """Resolve a bearer credential from `api_key_env` or `api_key_command`.

    Order: env var (if set and non-empty) → command stdout (if set and
    exits 0 with non-empty output) → BadParameter. The command runs
    without shell interpretation (argv list); commands like
    `gh auth token` and `gcloud secrets versions access ...` are safe
    to embed in config.
    """
    if bdef.api_key_env:
        v = _env_get(bdef.api_key_env)
        if v:
            return v

    if bdef.api_key_command:
        cmd_str = " ".join(shlex.quote(p) for p in bdef.api_key_command)
        fallback_hint = f" (or set ${bdef.api_key_env} directly)" if bdef.api_key_env else ""
        try:
            proc = subprocess.run(
                list(bdef.api_key_command),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            raise typer.BadParameter(
                f"--backend={name}: api_key_command not on PATH: {bdef.api_key_command[0]}{fallback_hint}"
            ) from None
        except subprocess.TimeoutExpired:
            raise typer.BadParameter(f"--backend={name}: api_key_command timed out: {cmd_str}") from None
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            tail = f"\n{stderr}" if stderr else ""
            raise typer.BadParameter(
                f"--backend={name}: api_key_command exited {proc.returncode}: {cmd_str}{tail}{fallback_hint}"
            )
        key = (proc.stdout or "").strip()
        if not key:
            raise typer.BadParameter(
                f"--backend={name}: api_key_command produced empty output: {cmd_str}{fallback_hint}"
            )
        return key

    # Reached only when api_key_env was set but the var was empty and
    # there's no api_key_command fallback.
    raise typer.BadParameter(f"--backend={name} but ${bdef.api_key_env} is not set.")


def _env_get(name: str) -> str | None:
    import os

    return os.environ.get(name)


__all__ = ["Backend", "resolve_api_key"]
