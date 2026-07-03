"""Claude CLI subprocess backend (`claude-cli`).

Drives `claude -p` via a CLI driver (`ClaudeCLIModel`). No API key
needed — uses the user's Claude Code CLI session. Auto-resolution
picks this when `claude` is on PATH and no Anthropic API key is
available.

The backend adapter (`ClaudeCliBackend`) handles PATH preflight and
constructs the driver; the driver (`ClaudeCLIModel`) is the
`pydantic_ai.Model` subclass that actually spawns `claude -p` on each
`request()`.
"""

from __future__ import annotations

import contextlib
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
    _mcp_config_for,
    _tail,
)
from .base import Backend

_FALLBACK_WARNED = False


class ClaudeCliBackend(Backend):
    auto_priority = 1

    def resolve(self, *, model: str) -> Client:
        if not shutil.which("claude"):
            raise typer.BadParameter(
                f"--backend={self.name} but `claude` is not on PATH (install Claude Code CLI or set ANTHROPIC_API_KEY)."
            )
        _warn_once()
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
        "Annotation quality is the same as the SDK path (same model, prompts, "
        "and repo tools via MCP), but expect slower runs: subprocess startup "
        "per hunk plus Claude Code subscription rate limits. If the hunk pass "
        "hits a rate limit on the primary model, --fallback-model silently "
        "demotes that call (opus → sonnet by default).\n"
    )
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClaudeCLINotFound(RuntimeError):
    pass


class ClaudeCLIError(RuntimeError):
    """Non-zero exits or malformed envelopes from `claude -p`.

    The string form includes common transient markers ("rate",
    "overloaded") when stderr suggests them, so retry predicates
    upstream can pick them up.
    """


# ---------------------------------------------------------------------------
# ClaudeCLIModel — `claude -p` with --json-schema
# ---------------------------------------------------------------------------


class ClaudeCLIModel(SubprocessModel):
    """`claude -p` subprocess as a pydantic-ai Model.

    Used when no `ANTHROPIC_API_KEY` is set but the `claude` CLI is on
    PATH (the user has a Claude Code subscription). Each `request()`
    call spawns a `claude -p` subprocess with `--json-schema` set to
    the output_type's JSON schema, parses the structured result, and
    returns a `ModelResponse` carrying a single `ToolCallPart` for the
    output tool.

    If `set_repo_tools()` has bound a `RepoTools`, a stdio MCP server
    is also injected via `--mcp-config`, so the model can explore the
    worktree during the call. Without it, the client runs single-shot
    and the model answers from the prompt alone.

    No client-side validation retry: `--json-schema` already enforces
    shape server-side, so we run with `max_validation_retries=0`.
    """

    _provider_name = "claude-cli"

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        claude_path: str | None = None,
        fallback_model: str | None = "claude-sonnet-4-6",
        max_turns_single_shot: int = 3,
        max_turns_with_mcp: int = 20,
        console_effort: str | None = "high",
    ) -> None:
        super().__init__(model=model, max_validation_retries=0)
        resolved = claude_path or shutil.which("claude")
        if not resolved:
            raise ClaudeCLINotFound("`claude` not on PATH")
        self._claude = resolved
        self._fallback_model = fallback_model
        self._max_turns_single_shot = max_turns_single_shot
        self._max_turns_with_mcp = max_turns_with_mcp
        # Reasoning depth for the free-form review console only (`--effort`,
        # passed to `claude -p` in _build_text_invocation). On adaptive-
        # thinking models (Opus 4.7+) this is the lever that turns reasoning
        # up; without it the console answers at the CLI's bare default and
        # feels shallow. The structured augment passes deliberately don't set
        # it — their cost/latency profile is tuned separately. None omits the
        # flag. "high" is supported on both Opus 4.8 and the Sonnet fallback.
        self._console_effort = console_effort
        self._mcp_config_path: Path | None = None

    # ---- mcp config plumbing ---------------------------------------------

    def _ensure_mcp_config(self) -> Path:
        if self._mcp_config_path is not None and self._mcp_config_path.exists():
            return self._mcp_config_path
        assert self._repo_tools is not None
        # `claude -p` launches stdio MCP servers as subprocesses itself; it
        # owns stdin/stdout of the child.
        config = {
            "mcpServers": {
                "scr": {"type": "stdio", **_mcp_config_for(self._repo_tools)},
            }
        }
        fd, path = tempfile.mkstemp(prefix="scr-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        self._mcp_config_path = Path(path)
        return self._mcp_config_path

    def _invalidate_mcp_artifacts(self) -> None:
        if self._mcp_config_path is not None:
            with contextlib.suppress(OSError):
                self._mcp_config_path.unlink()
            self._mcp_config_path = None

    # ---- prompt + argv ---------------------------------------------------

    @staticmethod
    def _build_prompt(user_text: str, submit_tool_name: str) -> str:
        """The text fed via stdin to `claude -p`.

        Trailing instruction nudges the model to emit the JSON object
        directly without prose / fences. `--json-schema` constrains the
        shape; the prompt nudge avoids the "I'd like to call submit_X"
        detour some models still try.
        """
        parts = [user_text] if user_text else []
        parts.append(
            f"# Task\nReply with a single JSON object matching the schema for "
            f"`{submit_tool_name}`. Do not include any prose or code fences."
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
        prompt = self._build_prompt(user_text, submit_tool_name)
        schema_json = json.dumps(schema, ensure_ascii=False)

        mcp_active = self._repo_tools is not None
        max_turns = self._max_turns_with_mcp if mcp_active else self._max_turns_single_shot

        # NOTE: do NOT pass --bare here. Its docs are explicit:
        # "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
        #  via --settings (OAuth and keychain are never read)."
        # Our entire reason to shell out to `claude -p` is that the user
        # lacks an API key but has OAuth/keychain credentials — --bare
        # would defeat the point and always return "Not logged in".
        # We pick the useful pieces of --bare individually below.
        argv = [
            self._claude,
            "-p",
            "--model",
            self._model,
            "--system-prompt",
            system_text,
            "--json-schema",
            schema_json,
            "--tools",
            "",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
            "--max-turns",
            str(max_turns),
        ]
        if self._fallback_model:
            argv += ["--fallback-model", self._fallback_model]
        if mcp_active:
            argv += [
                "--mcp-config",
                str(self._ensure_mcp_config()),
                "--strict-mcp-config",
            ]

        return _Invocation(
            argv=argv,
            stdin=prompt.encode("utf-8"),
            extra_log={"mcp": mcp_active, "max_turns": max_turns},
        )

    # ---- free-form (console) ---------------------------------------------

    def _build_text_invocation(self, *, system_text: str, user_text: str) -> _Invocation:
        """`claude -p` in plain-text mode for a free-form console turn.

        Same shape as `_build_invocation` minus `--json-schema` and the
        emit-JSON prompt nudge: the user text goes in verbatim and we ask
        for `--output-format json` only to recover the envelope's `result`
        text and usage. MCP is wired exactly as in the structured path, so
        the model can explore the worktrees mid-answer.
        """
        mcp_active = self._repo_tools is not None
        max_turns = self._max_turns_with_mcp if mcp_active else self._max_turns_single_shot
        argv = [
            self._claude,
            "-p",
            "--model",
            self._model,
            "--system-prompt",
            system_text,
            "--tools",
            "",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
            "--max-turns",
            str(max_turns),
        ]
        # Turn reasoning up for the console (see __init__). The structured
        # path omits this on purpose.
        if self._console_effort:
            argv += ["--effort", self._console_effort]
        if self._fallback_model:
            argv += ["--fallback-model", self._fallback_model]
        if mcp_active:
            argv += [
                "--mcp-config",
                str(self._ensure_mcp_config()),
                "--strict-mcp-config",
            ]

        return _Invocation(
            argv=argv,
            stdin=user_text.encode("utf-8"),
            extra_log={
                "mcp": mcp_active,
                "max_turns": max_turns,
                "free_form": True,
                "effort": self._console_effort,
            },
        )

    def _envelope_to_text(self, *, envelope: dict[str, Any]) -> str:
        self._raise_if_error(envelope)
        result = envelope.get("result")
        if not result:
            stop = envelope.get("stop_reason")
            turns = envelope.get("num_turns")
            raise ClaudeCLIError(
                "claude -p returned an empty result for a free-form turn "
                f"(stop_reason={stop!r}, num_turns={turns}). This usually "
                "means the model exhausted --max-turns mid-tool-loop — bump "
                "--max-turns or simplify the question."
            )
        return result

    # ---- envelope --------------------------------------------------------

    def _parse_envelope(self, *, stdout: bytes, stderr: bytes, returncode: int) -> dict[str, Any]:
        # `claude -p` frequently writes the real failure into the stdout
        # envelope (is_error=true, result=<message>) and exits non-zero
        # with empty stderr — e.g. when not logged in. So we parse stdout
        # first regardless of exit code, and only decorate with the
        # returncode if the parse itself fails.
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            if returncode != 0:
                raise ClaudeCLIError(
                    f"claude -p exited {returncode}: {_tail(stderr.decode('utf-8', errors='replace')) or '<no stderr>'}"
                )
            raise ClaudeCLIError(
                f"claude -p produced empty stdout; stderr={_tail(stderr.decode('utf-8', errors='replace'))}"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            if returncode != 0:
                raise ClaudeCLIError(
                    f"claude -p exited {returncode}: {_tail(stderr.decode('utf-8', errors='replace')) or '<no stderr>'}"
                ) from e
            raise ClaudeCLIError(f"claude -p envelope not JSON: {e}; stdout[:200]={text[:200]!r}") from e

    @staticmethod
    def _raise_if_error(envelope: dict[str, Any]) -> None:
        """Map an error envelope to a `ClaudeCLIError`, else return.

        `claude -p` frequently writes the real failure into the stdout
        envelope (`is_error=true`, `result=<message>`) and exits non-zero
        with empty stderr — e.g. when not logged in. Both the structured
        and free-form paths funnel error envelopes through here.
        """
        if not envelope.get("is_error"):
            return
        message = (envelope.get("result") or "").strip()
        lower = message.lower()
        if "not logged in" in lower or "please run /login" in lower:
            raise ClaudeCLIError(
                "claude is not logged in; run `claude /login` (or "
                "`claude setup-token` for a long-lived token) before "
                "using --backend=claude-cli."
            )
        raise ClaudeCLIError(
            f"claude -p returned error "
            f"(stop_reason={envelope.get('stop_reason')!r}, "
            f"num_turns={envelope.get('num_turns')}): {message or '<empty>'}"
        )

    def _envelope_to_structured(
        self,
        *,
        envelope: dict[str, Any],
        schema: dict[str, Any],
        submit_tool_name: str,
    ) -> dict[str, Any]:
        # `_parse_envelope` always returns a parsed dict; here we map
        # error envelopes and missing payloads to ClaudeCLIError. We
        # don't have access to returncode, but `is_error` plus an empty
        # `structured_output` covers both the "exit 1, envelope says
        # not-logged-in" path and clean-exit logic failures.
        self._raise_if_error(envelope)

        # When --json-schema is set, the validated object is returned in
        # `structured_output` (already parsed). `result` is empty in that
        # case. Fall back to parsing `result` as JSON for older `claude`
        # versions that emitted it there.
        structured = envelope.get("structured_output")
        if structured is not None:
            return structured
        result_text = envelope.get("result") or ""
        if not result_text:
            stop = envelope.get("stop_reason")
            turns = envelope.get("num_turns")
            raise ClaudeCLIError(
                "claude -p returned no structured_output and no result "
                f"(stop_reason={stop!r}, num_turns={turns}). This usually "
                "means the model tried to call a tool instead of emitting "
                "the JSON payload — bump --max-turns or reinforce the "
                "no-tool instruction in the prompt."
            )
        try:
            return json.loads(result_text)
        except json.JSONDecodeError as e:
            stop = envelope.get("stop_reason")
            turns = envelope.get("num_turns")
            raise ClaudeCLIError(
                f"claude -p result is not valid JSON (schema mode); "
                f"stop_reason={stop!r} num_turns={turns}: {e}; "
                f"result[:400]={result_text[:400]!r}"
            ) from e

    def _validation_exhausted_error(self, last_error: str | None, attempts: list[str]) -> Exception:
        # Unreachable — claude doesn't retry — but the abstract method
        # demands an implementation.
        return ClaudeCLIError(f"claude -p validation retries exhausted: {last_error!r}")


__all__ = [
    "ClaudeCLIError",
    "ClaudeCLIModel",
    "ClaudeCLINotFound",
    "ClaudeCliBackend",
]
