"""Gemini CLI subprocess backend (`gemini-cli`).

Drives `gemini -p` via a CLI driver (`GeminiCLIModel`). Auth comes
from the user's existing `gemini` setup — env vars (GEMINI_API_KEY /
GOOGLE_API_KEY) or the OAuth flow that drops
`~/.gemini/oauth_creds.json`.

The backend adapter (`GeminiCliBackend`) handles PATH preflight,
credential gating, and model coercion; the driver (`GeminiCLIModel`)
is the `pydantic_ai.Model` subclass that actually spawns `gemini -p`
on each `request()`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import typer

from ..augment.agents import Client
from ._cli_driver import (
    SubprocessModel,
    _Invocation,
    _extract_json_object,
    _mcp_config_for,
    _SchemaValidationError,
    _tail,
    _validate_against_schema,
    _ValidationFailure,
)
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


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GeminiCLINotFound(RuntimeError):
    pass


class GeminiCLIError(RuntimeError):
    """Non-zero exits, malformed envelopes, or persistent validation failure."""


# ---------------------------------------------------------------------------
# GeminiCLIModel — `gemini -p` with prompt-embedded schema + validate/retry
# ---------------------------------------------------------------------------

class GeminiCLIModel(SubprocessModel):
    """`gemini -p` subprocess as a pydantic-ai Model.

    Differences from `ClaudeCLIModel` worth flagging:

    - **No `--json-schema` flag.** Gemini's CLI doesn't surface
      `responseSchema`, so we embed the schema in the prompt and
      validate client-side. One retry on validation failure with the
      error fed back; second failure raises.
    - **`-p` takes the prompt as an argv value**, not via stdin. The
      argv slot immediately after `-p` *must* be the prompt or gemini
      bails with "Not enough arguments".
    - **MCP injection** uses a temp `settings.json` pointed at by
      `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (highest layer in the
      precedence chain) plus `--allowed-mcp-server-names scr`.
    - **`GEMINI_CLI_TRUST_WORKSPACE=true`** is set unconditionally;
      without it `gemini -p` refuses to run from "untrusted" folders.
    """

    _provider_name = "gemini-cli"

    def __init__(
        self,
        *,
        model: str = "gemini-2.5-pro",
        gemini_path: str | None = None,
        max_validation_retries: int = 1,
    ) -> None:
        super().__init__(model=model, max_validation_retries=max_validation_retries)
        resolved = gemini_path or shutil.which("gemini")
        if not resolved:
            raise GeminiCLINotFound("`gemini` not on PATH")
        self._gemini = resolved
        self._settings_path: Path | None = None

    # ---- mcp config plumbing ---------------------------------------------

    def _ensure_settings(self) -> Path:
        """Materialise the system-settings file gemini will read.

        We pin the file rather than the directory because
        `GEMINI_CLI_SYSTEM_SETTINGS_PATH` expects a file path —
        pointing it at a directory raises EISDIR.
        """
        if self._settings_path is not None and self._settings_path.exists():
            return self._settings_path
        assert self._repo_tools is not None
        settings = {"mcpServers": {"scr": _mcp_config_for(self._repo_tools)}}
        fd, path = tempfile.mkstemp(prefix="scr-gemini-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
        self._settings_path = Path(path)
        return self._settings_path

    def _invalidate_mcp_artifacts(self) -> None:
        if self._settings_path is not None:
            try:
                self._settings_path.unlink()
            except OSError:
                pass
            self._settings_path = None

    # ---- prompt + argv ---------------------------------------------------

    @staticmethod
    def _build_prompt(
        *,
        system_text: str,
        user_text: str,
        submit_tool_name: str,
        schema: dict[str, Any],
        prior_error: str | None,
    ) -> str:
        """Compose the single text prompt fed to `gemini -p`.

        Anthropic's tool-use API receives the schema out-of-band; gemini's
        CLI doesn't surface that, so we paste the schema in as a fenced
        code block with an explicit "no prose" instruction. On retry, we
        also include the prior validation error so the model has a chance
        to self-correct.
        """
        parts: list[str] = []
        if system_text:
            parts.append(f"# System\n{system_text}")
        if user_text:
            parts.append(user_text)
        parts.append(
            "# Task\n"
            f"Reply with a single JSON object matching the schema for "
            f"`{submit_tool_name}` below. Do not include any prose, "
            "explanations, or code fences in your reply — output the JSON "
            "object directly.\n\n"
            "## Schema\n```json\n"
            f"{json.dumps(schema, indent=2, ensure_ascii=False)}\n```"
        )
        if prior_error:
            parts.append(
                "# Previous attempt failed\n"
                f"{prior_error}\n\n"
                "Try again with a valid response that matches the schema exactly."
            )
        return "\n\n".join(parts)

    def _build_invocation(
        self,
        *,
        system_text: str,
        user_text: str,
        schema: dict[str, Any],
        submit_tool_name: str,
        prior_error: str | None,
    ) -> _Invocation:
        prompt = self._build_prompt(
            system_text=system_text,
            user_text=user_text,
            submit_tool_name=submit_tool_name,
            schema=schema,
            prior_error=prior_error,
        )
        # NB: gemini's `-p` takes the prompt *as a CLI argument*, unlike
        # claude's `-p` which reads from stdin. Putting any other flag
        # immediately after `-p` makes gemini interpret that flag's name
        # as the prompt value and fail with "Not enough arguments".
        argv = [
            self._gemini, "-p", prompt,
            "--output-format", "json",
            "--skip-trust",
        ]
        if self._repo_tools is not None:
            argv += ["--allowed-mcp-server-names", "scr"]

        env = dict(os.environ)
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        if self._repo_tools is not None:
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(self._ensure_settings())

        return _Invocation(
            argv=argv,
            env=env,
            stdin=None,
            extra_log={"prompt_chars": len(prompt)},
        )

    # ---- envelope --------------------------------------------------------

    def _parse_envelope(
        self, *, stdout: bytes, stderr: bytes, returncode: int
    ) -> dict[str, Any]:
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            raise GeminiCLIError(
                "gemini -p produced empty stdout; stderr="
                f"{_tail(stderr.decode('utf-8', errors='replace'))}"
            )
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as e:
            raise GeminiCLIError(
                f"gemini -p envelope not JSON: {e}; stdout[:200]={text[:200]!r}"
            ) from e

        err = envelope.get("error")
        if err or returncode != 0:
            err_msg = (
                err.get("message") if isinstance(err, dict)
                else str(err) if err else ""
            ) or _tail(stderr.decode("utf-8", errors="replace"))
            err_str = (err_msg or "").lower()
            if "auth" in err_str or "credentials" in err_str or "unauthenticated" in err_str:
                raise GeminiCLIError(
                    "gemini is not authenticated. Set GEMINI_API_KEY "
                    "(or GOOGLE_API_KEY for Vertex), or run `gemini` once "
                    "interactively to complete the OAuth flow."
                )
            raise GeminiCLIError(
                f"gemini -p exited {returncode} with error: "
                f"{err_msg or '<no message>'}"
            )
        return envelope

    def _envelope_to_structured(
        self,
        *,
        envelope: dict[str, Any],
        schema: dict[str, Any],
        submit_tool_name: str,
    ) -> dict[str, Any]:
        response_text = envelope.get("response") or ""
        try:
            submit_input = _extract_json_object(response_text)
        except ValueError as e:
            raise _ValidationFailure(
                reason=f"could not extract JSON from response: {e}",
                detail=_tail(response_text, 400),
            ) from e
        try:
            _validate_against_schema(submit_input, schema)
        except _SchemaValidationError as e:
            raise _ValidationFailure(
                reason=f"validation failed: {e}",
                detail=json.dumps(submit_input)[:400],
            ) from e
        return submit_input

    def _envelope_to_usage(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Rewrite a gemini envelope's stats as anthropic-shaped usage.

        `_structured_to_response` reads the unified anthropic shape; we
        coerce here so that single helper handles both backends. Field map:
          tokens.input      → input_tokens
          tokens.candidates → output_tokens   (gemini calls outputs "candidates")
          tokens.cached     → cache_read_input_tokens
        `stats.models` is a dict keyed by model id (one entry per routed
        model), so we sum across all of them.
        """
        input_tokens = output_tokens = cache_read_tokens = 0
        stats = envelope.get("stats") or {}
        models = stats.get("models") if isinstance(stats, dict) else None
        if isinstance(models, dict):
            for model_stats in models.values():
                if not isinstance(model_stats, dict):
                    continue
                tokens = model_stats.get("tokens") or {}
                input_tokens += int(tokens.get("input", 0) or 0)
                output_tokens += int(tokens.get("candidates", 0) or 0)
                cache_read_tokens += int(tokens.get("cached", 0) or 0)
        return {
            "session_id": envelope.get("session_id", ""),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read_tokens,
            },
        }

    def _validation_exhausted_error(
        self, last_error: str | None, attempts: list[str]
    ) -> Exception:
        return GeminiCLIError(
            f"gemini -p produced no schema-conformant response after "
            f"{self._max_validation_retries + 1} attempts. Last error: "
            f"{last_error!r}. Last attempt[:400]: "
            f"{attempts[-1] if attempts else '<none>'!r}"
        )


__all__ = [
    "GeminiCLIError",
    "GeminiCLIModel",
    "GeminiCLINotFound",
    "GeminiCliBackend",
]
