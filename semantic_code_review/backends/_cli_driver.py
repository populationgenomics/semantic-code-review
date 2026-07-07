"""Shared base for the CLI drivers in this package.

A **CLI driver** is a concrete `pydantic_ai.Model` subclass we author to
wrap a third-party LLM CLI (`claude -p`). Each `request()`
spawns the CLI, parses its envelope, and returns a synthetic
`ModelResponse`; the multi-turn tool-call loop runs inside the
subprocess via MCP, not in pydantic-ai.

`SubprocessModel` owns the request loop, message flattening, retry
control, subprocess spawning, and `ModelResponse` construction.
Subclasses (the per-backend drivers, e.g. `ClaudeCLIModel` in
`claude_cli.py`) provide the differences: argv shape, prompt format,
envelope parsing and error mapping, and usage normalisation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
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
                            text = getattr(item, "text", None) or (item if isinstance(item, str) else None)
                            if text:
                                user_chunks.append(f"# user\n{text}")
                elif isinstance(part, ToolReturnPart):
                    user_chunks.append(f"# tool_result {part.tool_call_id}\n{_stringify(part.content)}")
                elif isinstance(part, RetryPromptPart):
                    user_chunks.append(f"# retry {part.tool_call_id or ''}\n{_stringify(part.content)}")
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
            "CLI driver requires an output_type-driven Agent — model_request_parameters.output_tools is empty."
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
    for ip in mrp.instruction_parts or []:
        text = getattr(ip, "content", None) or getattr(ip, "text", None) or ""
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def _tail(text: str, n: int = 400) -> str:
    t = text.strip()
    if len(t) <= n:
        return t
    return "..." + t[-n:]


def _head(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…(+{len(text) - n} chars)"


def _redact_argv(argv: list[str]) -> list[str]:
    """Copy argv with the `--system-prompt` value truncated.

    The system prompt is long, static, and would dominate every debug
    record; the rest of the argv (flags, model, session id) is the part
    worth seeing. Value-bearing flags aside, argv carries no secrets — the
    CLI reads credentials from the environment, not the command line.
    """
    out: list[str] = []
    redact_next = False
    for tok in argv:
        if redact_next:
            out.append(_head(tok, 120))
            redact_next = False
            continue
        out.append(tok)
        if tok == "--system-prompt":
            redact_next = True
    return out


def _usage_from_envelope(envelope: dict[str, Any]) -> RequestUsage:
    usage_src = envelope.get("usage") or {}
    return RequestUsage(
        input_tokens=int(usage_src.get("input_tokens", 0) or 0),
        output_tokens=int(usage_src.get("output_tokens", 0) or 0),
        cache_write_tokens=int(usage_src.get("cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(usage_src.get("cache_read_input_tokens", 0) or 0),
    )


def _structured_to_response(
    structured: dict[str, Any],
    *,
    tool_name: str,
    envelope: dict[str, Any],
    model_name: str,
    provider_name: str,
) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name=tool_name,
                args=structured,
                tool_call_id=envelope.get("session_id", "") or "",
            ),
        ],
        usage=_usage_from_envelope(envelope),
        model_name=model_name,
        provider_name=provider_name,
        finish_reason="tool_call",
    )


def _text_to_response(
    text: str,
    *,
    envelope: dict[str, Any],
    model_name: str,
    provider_name: str,
) -> ModelResponse:
    """Wrap a free-form text answer as a `ModelResponse`.

    The free-form (console) counterpart to `_structured_to_response`:
    the CLI ran its own tool loop internally (via MCP) and returned prose,
    so the response carries a single `TextPart` rather than a submit-tool
    `ToolCallPart`. pydantic-ai surfaces this as the agent's `str` output.
    """
    return ModelResponse(
        parts=[TextPart(content=text)],
        usage=_usage_from_envelope(envelope),
        model_name=model_name,
        provider_name=provider_name,
        finish_reason="stop",
    )


# ---------------------------------------------------------------------------
# Shared validation-retry sentinels
# ---------------------------------------------------------------------------


class _SchemaValidationError(ValueError):
    pass


class _ValidationFailure(Exception):
    """Internal: a parse / schema-validation error that the request loop
    should retry rather than surface. Subclasses raise this from
    `_envelope_to_structured` for retry-eligible failures.
    """

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
    """Base for the CLI drivers — `claude -p` style.

    Owns: message flattening, retry loop, subprocess spawn, response
    assembly. Subclasses (the per-backend CLI drivers) provide the
    differences via the hooks below. Not itself a CLI driver — concrete
    driver classes live in their per-backend files.
    """

    is_subprocess_backend = True
    _provider = None  # type: ignore[assignment]
    _provider_name: str = ""  # subclass overrides; used as `system`.

    def __init__(self, *, model: str, max_validation_retries: int = 0) -> None:
        super().__init__()
        self._model = model
        self._max_validation_retries = max_validation_retries
        # The run's hosted HTTP MCP server entry (`{type:"http", url, headers}`),
        # set via `set_mcp_endpoint` (ADR 0003 Slice 3). None ⇒ single-shot,
        # no tools.
        self._mcp_endpoint: dict[str, Any] | None = None
        # Debug observability (opt-in). When a sink is bound, each subprocess
        # spawn emits a structured record the review server fans out to the
        # debug drawer. None (the default) is a hard gate: no sink → the
        # record is never built, so there's zero overhead off the debug path.
        self._debug_sink: Callable[[dict[str, Any]], None] | None = None

    def set_debug_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Route per-spawn debug records to `sink` (None disables).

        Bound by the review server in `--debug` mode so the raw `claude -p`
        envelope of every turn is visible in the viewer's debug drawer.
        """
        self._debug_sink = sink

    def _emit_debug(
        self,
        *,
        inv: _Invocation,
        stdout: bytes,
        stderr: bytes,
        returncode: int,
        duration_ms: int,
        envelope: dict[str, Any] | None,
        free_form: bool,
    ) -> None:
        """Build and hand one per-spawn record to the debug sink (if bound).

        No sink → returns immediately, before any record is built. The
        record is bounded (argv system-prompt redacted, stdin + result
        previewed) so it stays cheap to buffer and ship over SSE.
        """
        sink = self._debug_sink
        if sink is None:
            return
        env = envelope or {}
        result = env.get("result")
        record: dict[str, Any] = {
            "provider": self._provider_name,
            "model": self._model,
            "free_form": free_form,
            "returncode": returncode,
            "duration_ms": duration_ms,
            "argv": _redact_argv(inv.argv),
            "stdin_preview": _head(inv.stdin.decode("utf-8", errors="replace"), 4000) if inv.stdin else "",
            "stderr_tail": _tail(stderr.decode("utf-8", errors="replace")),
            "envelope": {
                "subtype": env.get("subtype"),
                "is_error": env.get("is_error"),
                "stop_reason": env.get("stop_reason"),
                "num_turns": env.get("num_turns"),
                "session_id": env.get("session_id"),
                "usage": env.get("usage"),
                "result_preview": _head(result, 8000) if isinstance(result, str) else result,
            },
        }
        try:
            sink(record)
        except Exception:  # noqa: BLE001 — debug telemetry must never break a turn
            log.warning("debug sink raised; dropping record", exc_info=True)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def system(self) -> str:
        return self._provider_name

    def set_mcp_endpoint(self, config: dict[str, Any] | None) -> None:
        """Point the CLI at the run's hosted HTTP MCP server (ADR 0003 Slice 3).

        `config` is the `--mcp-config` server entry the run's host exposes
        (`McpHttpHost.mcp_config()`), or None to clear it (single-shot, no
        tools). Every spawn connects to that one warm server.
        """
        self._mcp_endpoint = config
        self._invalidate_mcp_artifacts()

    async def aclose(self) -> None:
        # Pipeline calls this in a try/finally so any temp config file
        # is removed deterministically at end-of-pass. Idempotent.
        self._invalidate_mcp_artifacts()

    # ---- subclass hooks --------------------------------------------------

    def _invalidate_mcp_artifacts(self) -> None:
        """Drop any temp files materialised for the current MCP endpoint."""

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
    def _parse_envelope(self, *, stdout: bytes, stderr: bytes, returncode: int) -> dict[str, Any]:
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
    def _validation_exhausted_error(self, last_error: str | None, attempts: list[str]) -> Exception: ...

    # ---- free-form (console) hooks ---------------------------------------
    #
    # The structured hooks above drive the augment passes' forced
    # `output_type=ToolOutput(...)` path. The console (ADR 0002) runs a
    # *free-form* agent with no output_type: the request loop dispatches
    # to these instead when `output_tools` is empty. The default raises —
    # a driver opts into console support by overriding both.

    def _build_text_invocation(self, *, system_text: str, user_text: str) -> _Invocation:
        """Spawn the CLI in plain-text mode (no schema-constrained output)."""
        raise NotImplementedError(f"{self._provider_name} does not support free-form console turns")

    def _envelope_to_text(self, *, envelope: dict[str, Any]) -> str:
        """Envelope → the model's prose answer. Hard failures raise."""
        raise NotImplementedError(f"{self._provider_name} does not support free-form console turns")

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

        # Free-form branch (ADR 0002 — console). A no-`output_type` Agent
        # leaves `output_tools` empty: skip the submit-tool path entirely,
        # spawn the CLI in plain-text mode, and return a `TextPart`. There
        # is no schema to validate against, so no retry loop — the CLI ran
        # its own tool loop internally and either answered or errored.
        if not model_request_parameters.output_tools:
            return await self._request_text(
                system_text=system_text,
                user_text=user_text,
            )

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
                self._provider_name,
                self._model,
                attempt + 1,
                tool_def.name,
                "".join(f" {k}={v}" for k, v in inv.extra_log.items()),
            )
            t0 = time.monotonic()
            stdout, stderr, returncode = await self._spawn(inv)
            duration_ms = int((time.monotonic() - t0) * 1000)

            envelope: dict[str, Any] | None = None
            try:
                envelope = self._parse_envelope(
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                )
            finally:
                self._emit_debug(
                    inv=inv,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    duration_ms=duration_ms,
                    envelope=envelope,
                    free_form=False,
                )
            # `_parse_envelope` returns a dict or raises; a None here would
            # mean the finally ran on the raise path, which propagates.
            assert envelope is not None
            try:
                structured = self._envelope_to_structured(
                    envelope=envelope,
                    schema=schema,
                    submit_tool_name=tool_def.name,
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

    async def _request_text(self, *, system_text: str, user_text: str) -> ModelResponse:
        inv = self._build_text_invocation(
            system_text=system_text,
            user_text=user_text,
        )
        log.info(
            "%s invoking (free-form): model=%s%s",
            self._provider_name,
            self._model,
            "".join(f" {k}={v}" for k, v in inv.extra_log.items()),
        )
        t0 = time.monotonic()
        stdout, stderr, returncode = await self._spawn(inv)
        duration_ms = int((time.monotonic() - t0) * 1000)
        envelope: dict[str, Any] | None = None
        try:
            envelope = self._parse_envelope(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
            )
        finally:
            self._emit_debug(
                inv=inv,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                duration_ms=duration_ms,
                envelope=envelope,
                free_form=True,
            )
        assert envelope is not None  # parse returns a dict or raises
        text = self._envelope_to_text(envelope=envelope)
        return _text_to_response(
            text,
            envelope=self._envelope_to_usage(envelope),
            model_name=self._model,
            provider_name=self._provider_name,
        )

    async def _spawn(self, inv: _Invocation) -> tuple[bytes, bytes, int]:
        stdin_arg = asyncio.subprocess.PIPE if inv.stdin is not None else asyncio.subprocess.DEVNULL
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
            self._provider_name,
            rc,
            len(stdout),
            len(stderr),
        )
        return stdout, stderr, rc


# ---------------------------------------------------------------------------
# JSON helpers (used by drivers that validate/extract client-side)
# ---------------------------------------------------------------------------


def _validate_against_schema(value: Any, schema: dict[str, Any]) -> None:
    """Minimal JSON-Schema check: required-keys + top-level type.

    Shallow on purpose — pydantic-ai will validate the model's output
    against the full Pydantic schema once we hand it back. This layer
    just distinguishes "wrong shape, retry" from "fine JSON, downstream
    contract bug" so the retry path is bounded.
    """
    if schema.get("type") == "object" and not isinstance(value, dict):
        raise _SchemaValidationError(f"top-level type must be object, got {type(value).__name__}")
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


__all__ = [
    "SubprocessModel",
    "_Invocation",
    "_SchemaValidationError",
    "_ValidationFailure",
    "_extract_json_object",
    "_flatten_messages",
    "_instructions_to_system",
    "_output_tool",
    "_stringify",
    "_structured_to_response",
    "_tail",
    "_text_to_response",
    "_usage_from_envelope",
    "_validate_against_schema",
]
