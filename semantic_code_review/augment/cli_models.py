"""`pydantic_ai.Model` subclasses for the CLI subprocess backends.

These wrap `claude -p` / `gemini -p` directly: each `request()` spawns
a subprocess, parses its envelope, and returns a `ModelResponse` that
the Agent's loop sees as a single tool-call submission. The CLI
subprocess itself runs whatever multi-turn tool exploration the model
does — pydantic-ai's loop is degenerate for these models.

`function_tools` registered on the agent (the repo `read_file` /
`grep` etc. functions) reach the CLI subprocess out-of-band through
the MCP server we spawn via `--mcp-config` (claude) or
`GEMINI_CLI_SYSTEM_SETTINGS_PATH` (gemini); pydantic-ai itself
never sees the intermediate tool turns.

`SubprocessModel` owns the request loop, message flattening, retry
control, subprocess spawning, and `ModelResponse` construction.
Subclasses provide only the differences: argv shape, prompt format,
envelope parsing and error mapping, and usage normalisation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from .tools import RepoTools


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared message → flat-text translation
# ---------------------------------------------------------------------------

def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def _flatten_messages(messages: list[ModelMessage]) -> tuple[str, str]:
    """Translate pydantic-ai messages → (system_text, user_text).

    System prompts go to a separate channel (claude `--system-prompt`,
    gemini prompt header). Everything else — user prompts and any
    replayed tool calls / results from prior turns — gets flattened
    into a single `user_text` string. Both CLI subprocesses run
    single-shot per pydantic-ai request; the loop doesn't iterate.
    """
    system_chunks: list[str] = []
    user_chunks: list[str] = []

    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, SystemPromptPart):
                    system_chunks.append(part.content)
                elif isinstance(part, UserPromptPart):
                    content = part.content
                    if isinstance(content, str):
                        user_chunks.append(f"# user\n{content}")
                    else:
                        for item in content:
                            text = (
                                getattr(item, "text", None)
                                or (item if isinstance(item, str) else None)
                            )
                            if text:
                                user_chunks.append(f"# user\n{text}")
                elif isinstance(part, ToolReturnPart):
                    user_chunks.append(
                        f"# tool_result {part.tool_call_id}\n{_stringify(part.content)}"
                    )
                elif isinstance(part, RetryPromptPart):
                    user_chunks.append(
                        f"# retry {part.tool_call_id or ''}\n"
                        f"{_stringify(part.content)}"
                    )
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    user_chunks.append(f"# assistant\n{part.content}")
                elif isinstance(part, ToolCallPart):
                    user_chunks.append(
                        f"# assistant\n[tool_use: {part.tool_name} "
                        f"input={json.dumps(part.args_as_dict(), ensure_ascii=False)}]"
                    )

    return "\n\n".join(system_chunks), "\n\n".join(user_chunks)


def _output_tool(mrp: ModelRequestParameters) -> ToolDefinition:
    """The `output_type=ToolOutput(...)` tool the Agent expects us to call."""
    if not mrp.output_tools:
        raise RuntimeError(
            "CLI Model requires an output_type-driven Agent — "
            "model_request_parameters.output_tools is empty."
        )
    return mrp.output_tools[0]


def _instructions_to_system(mrp: ModelRequestParameters) -> str:
    """Concatenate `Agent(instructions=...)` parts into a system block.

    Pydantic-ai puts `Agent(instructions=...)` strings in
    `model_request_parameters.instruction_parts`, NOT as a
    `SystemPromptPart` in the messages — so a plain message walk
    misses them.
    """
    chunks: list[str] = []
    for ip in (mrp.instruction_parts or []):
        text = getattr(ip, "content", None) or getattr(ip, "text", None) or ""
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def _tail(text: str, n: int = 400) -> str:
    t = text.strip()
    if len(t) <= n:
        return t
    return "..." + t[-n:]


def _mcp_config_for(rt: RepoTools) -> dict[str, Any]:
    """Build the JSON the CLIs use to spawn our stdio MCP server.

    Both clients accept this shape (claude reads it via `--mcp-config`,
    gemini via `mcpServers` in `settings.json`). Ensures the child
    Python finds `semantic_code_review` even when the parent CLI was
    launched from an unrelated cwd.
    """
    import semantic_code_review as _pkg
    pkg_root = str(Path(_pkg.__file__).resolve().parent.parent)
    existing_pp = os.environ.get("PYTHONPATH", "")
    pythonpath = (
        f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
    )
    return {
        "command": sys.executable,
        "args": [
            "-m", "semantic_code_review.augment.mcp_server",
            "--head-worktree", str(rt.head_worktree),
            "--repo-git", str(rt.repo_git),
            "--base-sha", rt.base_sha,
            "--head-sha", rt.head_sha,
        ],
        "env": {"PYTHONPATH": pythonpath},
    }


def _structured_to_response(
    structured: dict[str, Any],
    *,
    tool_name: str,
    envelope: dict[str, Any],
    model_name: str,
    provider_name: str,
) -> ModelResponse:
    usage_src = envelope.get("usage") or {}
    usage = RequestUsage(
        input_tokens=int(usage_src.get("input_tokens", 0) or 0),
        output_tokens=int(usage_src.get("output_tokens", 0) or 0),
        cache_write_tokens=int(usage_src.get("cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(usage_src.get("cache_read_input_tokens", 0) or 0),
    )
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name=tool_name,
                args=structured,
                tool_call_id=envelope.get("session_id", "") or "",
            ),
        ],
        usage=usage,
        model_name=model_name,
        provider_name=provider_name,
        finish_reason="tool_call",
    )


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


class GeminiCLINotFound(RuntimeError):
    pass


class GeminiCLIError(RuntimeError):
    """Non-zero exits, malformed envelopes, or persistent validation failure."""


class _SchemaValidationError(ValueError):
    pass


class _ValidationFailure(Exception):
    """Internal: a parse / schema-validation error that the request loop
    should retry rather than surface. Subclasses raise this from
    `_envelope_to_structured` for retry-eligible failures."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# SubprocessModel — request loop / spawn / retry skeleton
# ---------------------------------------------------------------------------

@dataclass
class _Invocation:
    """One subprocess spawn. `stdin=None` → DEVNULL; bytes → piped."""

    argv: list[str]
    env: dict[str, str] | None = None
    stdin: bytes | None = None
    extra_log: dict[str, Any] = field(default_factory=dict)


class SubprocessModel(Model, ABC):
    """Base for `claude -p` / `gemini -p` style backends.

    Owns: message flattening, retry loop, subprocess spawn, response
    assembly. Subclasses provide the differences via the hooks below.
    """

    is_subprocess_backend = True
    _provider = None  # type: ignore[assignment]
    _provider_name: str = ""  # subclass overrides; used as `system`.

    def __init__(self, *, model: str, max_validation_retries: int = 0) -> None:
        super().__init__()
        self._model = model
        self._max_validation_retries = max_validation_retries
        self._repo_tools: RepoTools | None = None

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def system(self) -> str:
        return self._provider_name

    def set_repo_tools(self, repo_tools: RepoTools | None) -> None:
        """Bind/unbind `RepoTools` for MCP-backed repo access.

        Called by the augment pipeline once it has resolved the run dir.
        Setting to None reverts to single-shot mode.
        """
        self._repo_tools = repo_tools
        self._invalidate_mcp_artifacts()

    async def aclose(self) -> None:
        # Pipeline calls this in a try/finally so any temp config file
        # is removed deterministically at end-of-pass. Idempotent.
        self._invalidate_mcp_artifacts()

    # ---- subclass hooks --------------------------------------------------

    def _invalidate_mcp_artifacts(self) -> None:
        """Drop any temp files materialised for the current `_repo_tools`."""

    @abstractmethod
    def _build_invocation(
        self,
        *,
        system_text: str,
        user_text: str,
        schema: dict[str, Any],
        submit_tool_name: str,
        prior_error: str | None,
    ) -> _Invocation: ...

    @abstractmethod
    def _parse_envelope(
        self, *, stdout: bytes, stderr: bytes, returncode: int
    ) -> dict[str, Any]:
        """Raw output → JSON envelope. Hard failures raise the typed error."""

    @abstractmethod
    def _envelope_to_structured(
        self,
        *,
        envelope: dict[str, Any],
        schema: dict[str, Any],
        submit_tool_name: str,
    ) -> dict[str, Any]:
        """Envelope → submit-tool input. Raise `_ValidationFailure` to retry."""

    def _envelope_to_usage(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Coerce the parsed envelope into anthropic-shaped usage.

        Default: identity (claude already emits this shape). Gemini
        overrides to walk `stats.models` and synthesise the keys.
        """
        return envelope

    @abstractmethod
    def _validation_exhausted_error(
        self, last_error: str | None, attempts: list[str]
    ) -> Exception: ...

    # ---- core request loop ------------------------------------------------

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        sys_text, user_text = _flatten_messages(messages)
        instructions = _instructions_to_system(model_request_parameters)
        system_text = "\n\n".join(x for x in (instructions, sys_text) if x)

        tool_def = _output_tool(model_request_parameters)
        schema = tool_def.parameters_json_schema

        last_error: str | None = None
        attempts: list[str] = []
        for attempt in range(self._max_validation_retries + 1):
            inv = self._build_invocation(
                system_text=system_text,
                user_text=user_text,
                schema=schema,
                submit_tool_name=tool_def.name,
                prior_error=last_error,
            )
            log.info(
                "%s invoking: model=%s attempt=%d submit=%s%s",
                self._provider_name, self._model, attempt + 1, tool_def.name,
                "".join(f" {k}={v}" for k, v in inv.extra_log.items()),
            )
            stdout, stderr, returncode = await self._spawn(inv)

            envelope = self._parse_envelope(
                stdout=stdout, stderr=stderr, returncode=returncode,
            )
            try:
                structured = self._envelope_to_structured(
                    envelope=envelope, schema=schema, submit_tool_name=tool_def.name,
                )
            except _ValidationFailure as e:
                last_error = e.reason
                attempts.append(e.detail)
                continue
            return _structured_to_response(
                structured,
                tool_name=tool_def.name,
                envelope=self._envelope_to_usage(envelope),
                model_name=self._model,
                provider_name=self._provider_name,
            )

        raise self._validation_exhausted_error(last_error, attempts)

    async def _spawn(self, inv: _Invocation) -> tuple[bytes, bytes, int]:
        stdin_arg = (
            asyncio.subprocess.PIPE if inv.stdin is not None
            else asyncio.subprocess.DEVNULL
        )
        proc = await asyncio.create_subprocess_exec(
            *inv.argv,
            stdin=stdin_arg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=inv.env,
        )
        stdout, stderr = await proc.communicate(inv.stdin)
        rc = proc.returncode if proc.returncode is not None else -1
        log.info(
            "%s exit=%d stdout=%d stderr=%d",
            self._provider_name, rc, len(stdout), len(stderr),
        )
        return stdout, stderr, rc


# ---------------------------------------------------------------------------
# JSON helpers (gemini-side; also reusable)
# ---------------------------------------------------------------------------

def _validate_against_schema(value: Any, schema: dict[str, Any]) -> None:
    """Minimal JSON-Schema check: required-keys + top-level type.

    Shallow on purpose — pydantic-ai will validate the model's output
    against the full Pydantic schema once we hand it back. This layer
    just distinguishes "wrong shape, retry" from "fine JSON, downstream
    contract bug" so the retry path is bounded.
    """
    if schema.get("type") == "object" and not isinstance(value, dict):
        raise _SchemaValidationError(
            f"top-level type must be object, got {type(value).__name__}"
        )
    required = schema.get("required") or []
    missing = [k for k in required if k not in value]
    if missing:
        raise _SchemaValidationError(f"missing required keys: {missing}")


def _extract_json_object(response_text: str) -> dict[str, Any]:
    """Pull a JSON object out of the model's reply.

    Tolerant of stray prose and code fences — we ask the model not to
    emit them, but instruction-following isn't perfect. Strategy: try
    a clean parse first; on failure, look for the outermost balanced
    `{...}` and parse that.
    """
    text = response_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError(f"top-level value is {type(obj).__name__}, want object")
        return obj
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ValueError("no '{' found in response")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    break
                break
    raise ValueError("could not locate a balanced JSON object in response")


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
    ) -> None:
        super().__init__(model=model, max_validation_retries=0)
        resolved = claude_path or shutil.which("claude")
        if not resolved:
            raise ClaudeCLINotFound("`claude` not on PATH")
        self._claude = resolved
        self._fallback_model = fallback_model
        self._max_turns_single_shot = max_turns_single_shot
        self._max_turns_with_mcp = max_turns_with_mcp
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
            try:
                self._mcp_config_path.unlink()
            except OSError:
                pass
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
        max_turns = (
            self._max_turns_with_mcp if mcp_active else self._max_turns_single_shot
        )

        # NOTE: do NOT pass --bare here. Its docs are explicit:
        # "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
        #  via --settings (OAuth and keychain are never read)."
        # Our entire reason to shell out to `claude -p` is that the user
        # lacks an API key but has OAuth/keychain credentials — --bare
        # would defeat the point and always return "Not logged in".
        # We pick the useful pieces of --bare individually below.
        argv = [
            self._claude, "-p",
            "--model", self._model,
            "--system-prompt", system_text,
            "--json-schema", schema_json,
            "--tools", "",
            "--no-session-persistence",
            "--setting-sources", "",
            "--permission-mode", "bypassPermissions",
            "--output-format", "json",
            "--max-turns", str(max_turns),
        ]
        if self._fallback_model:
            argv += ["--fallback-model", self._fallback_model]
        if mcp_active:
            argv += [
                "--mcp-config", str(self._ensure_mcp_config()),
                "--strict-mcp-config",
            ]

        return _Invocation(
            argv=argv,
            stdin=prompt.encode("utf-8"),
            extra_log={"mcp": mcp_active, "max_turns": max_turns},
        )

    # ---- envelope --------------------------------------------------------

    def _parse_envelope(
        self, *, stdout: bytes, stderr: bytes, returncode: int
    ) -> dict[str, Any]:
        # `claude -p` frequently writes the real failure into the stdout
        # envelope (is_error=true, result=<message>) and exits non-zero
        # with empty stderr — e.g. when not logged in. So we parse stdout
        # first regardless of exit code, and only decorate with the
        # returncode if the parse itself fails.
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            if returncode != 0:
                raise ClaudeCLIError(
                    f"claude -p exited {returncode}: "
                    f"{_tail(stderr.decode('utf-8', errors='replace')) or '<no stderr>'}"
                )
            raise ClaudeCLIError(
                f"claude -p produced empty stdout; stderr="
                f"{_tail(stderr.decode('utf-8', errors='replace'))}"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            if returncode != 0:
                raise ClaudeCLIError(
                    f"claude -p exited {returncode}: "
                    f"{_tail(stderr.decode('utf-8', errors='replace')) or '<no stderr>'}"
                ) from e
            raise ClaudeCLIError(
                f"claude -p envelope not JSON: {e}; stdout[:200]={text[:200]!r}"
            ) from e

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
        if envelope.get("is_error"):
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

    def _validation_exhausted_error(
        self, last_error: str | None, attempts: list[str]
    ) -> Exception:
        # Unreachable — claude doesn't retry — but the abstract method
        # demands an implementation.
        return ClaudeCLIError(
            f"claude -p validation retries exhausted: {last_error!r}"
        )


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
    "ClaudeCLIError",
    "ClaudeCLIModel",
    "ClaudeCLINotFound",
    "GeminiCLIError",
    "GeminiCLIModel",
    "GeminiCLINotFound",
    "SubprocessModel",
]
