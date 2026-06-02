"""Translate a pydantic-ai `AgentRunResult` to the per-iteration trace
JSON shape `run_agentic` (now retired) used to write.

Every consumer of `trace/*.json` reads this shape (the viewer's
trace tab, ad-hoc tooling, support diagnostics). Keeping it stable
across the SDK / CLI migration means downstream code doesn't notice
which loop driver produced the run.

The shape:

    {
        "model": str,
        "system": str,
        "tools": [tool_name, ...],
        "submit_tool": str,         # name of the structured-output sink
        "iterations": [
            {
                "messages_sent": [{"role", "content"}, ...],
                "response": {"model", "role", "stop_reason", "usage", "content"},
                "tool_results": [{"type": "tool_result", "tool_use_id", "content"}, ...]
            },
            ...
        ],
        "result": {
            "submit_args": dict,    # the validated output_type instance, dumped
            "tool_calls": [{"name", "input", "result_len"}, ...],
            "input_tokens": int,
            "output_tokens": int,
            "cache_read_tokens": int,
        },
        "error": {                  # present iff the run failed before submit
            "type": str,            # exception class name
            "message": str,         # str(exc)
        },
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)


#: Cap for the fallback `repr` on unrecognised parts. Long thinking
#: blocks can be tens of KB; trimming keeps traces inspectable.
_FALLBACK_REPR_CAP = 5000


def _request_to_sent(req: ModelRequest) -> list[dict[str, Any]]:
    """Translate a ModelRequest's parts into the legacy `messages_sent` shape.

    System prompts are intentionally dropped — the trace records the
    system prompt once at the top level. Anything else we don't have
    explicit handling for (FilePart, InstructionPart, …) falls through
    to a generic dump so the trace doesn't silently lose information.
    """
    out: list[dict[str, Any]] = []
    for part in req.parts:
        if isinstance(part, SystemPromptPart):
            continue
        if isinstance(part, UserPromptPart):
            out.append({"role": "user", "content": part.content})
        elif isinstance(part, ToolReturnPart):
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": part.tool_call_id,
                            "content": _stringify(part.content),
                        }
                    ],
                }
            )
        elif isinstance(part, RetryPromptPart):
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": part.tool_call_id or "",
                            "content": _stringify(part.content),
                            "is_error": True,
                        }
                    ],
                }
            )
        else:
            out.append({"role": "unknown", "content": _fallback_part(part)})
    return out


def _response_content(resp: ModelResponse) -> list[dict[str, Any]]:
    """Flatten ModelResponse parts into the legacy assistant `content` blocks.

    Parts the legacy shape doesn't have a slot for (ThinkingPart,
    BuiltinToolCallPart, …) fall through to a generic dump so a
    misbehaving model run's trace still shows what the model emitted.
    """
    out: list[dict[str, Any]] = []
    for part in resp.parts:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.content})
        elif isinstance(part, ToolCallPart):
            try:
                input_ = part.args_as_dict()
            except Exception as e:  # noqa: BLE001 — args may be unparseable JSON
                # The malformed args themselves are exactly what we
                # want to see when a tool-output validation fails.
                input_ = {
                    "_raw": getattr(part, "args", None),
                    "_parse_error": f"{type(e).__name__}: {e}",
                }
            out.append(
                {
                    "type": "tool_use",
                    "id": part.tool_call_id or "",
                    "name": part.tool_name,
                    "input": input_,
                }
            )
        else:
            out.append(_fallback_part(part))
    return out


def _fallback_part(part: Any) -> dict[str, Any]:
    """Generic dump for message parts we don't render specifically.

    Carries the class name and a truncated `repr` so trace readers can
    see at minimum what kind of part the model emitted, and (for short
    parts) its content. Common targets: ThinkingPart from extended-
    thinking models, BuiltinToolCallPart / BuiltinToolReturnPart from
    server-side tool surfaces, FilePart / InstructionPart from
    multimodal prompts.
    """
    text = repr(part)
    if len(text) > _FALLBACK_REPR_CAP:
        text = text[:_FALLBACK_REPR_CAP] + "…(truncated)"
    return {"type": type(part).__name__, "repr": text}


def _response_to_dict(resp: ModelResponse) -> dict[str, Any]:
    usage = resp.usage
    return {
        "id": resp.provider_response_id or "",
        "model": resp.model_name or "",
        "role": "assistant",
        "stop_reason": resp.finish_reason or "",
        "usage": {
            "input_tokens": (usage.input_tokens or 0) if usage else 0,
            "output_tokens": (usage.output_tokens or 0) if usage else 0,
            "cache_creation_input_tokens": (usage.cache_write_tokens or 0) if usage else 0,
            "cache_read_input_tokens": (usage.cache_read_tokens or 0) if usage else 0,
        },
        "content": _response_content(resp),
    }


def _tool_returns_in(req: ModelRequest) -> list[dict[str, Any]]:
    """Tool results that follow a response (carried by the next request)."""
    out: list[dict[str, Any]] = []
    for part in req.parts:
        if isinstance(part, ToolReturnPart):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": part.tool_call_id,
                    "content": _stringify(part.content),
                }
            )
        elif isinstance(part, RetryPromptPart):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": part.tool_call_id or "",
                    "content": _stringify(part.content),
                    "is_error": True,
                }
            )
    return out


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def write_pydantic_ai_trace(
    result: Any,
    *,
    trace_path: Path,
    model: str,
    system: str,
    tool_names: list[str],
    submit_tool: str,
) -> None:
    """Render an `AgentRunResult` into the legacy trace shape and write it."""
    submit_args = _submit_args_from_output(getattr(result, "output", None))
    write_partial_trace(
        list(result.all_messages()),
        trace_path=trace_path,
        model=model,
        system=system,
        tool_names=tool_names,
        submit_tool=submit_tool,
        submit_args=submit_args,
    )


def write_partial_trace(
    messages: list[Any],
    *,
    trace_path: Path,
    model: str,
    system: str,
    tool_names: list[str],
    submit_tool: str,
    submit_args: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Write a trace for a (possibly partial) message history.

    Distinct entry point from ``write_pydantic_ai_trace`` because the
    caller drives ``agent.iter()`` rather than ``agent.run()`` — on a
    `UsageLimitExceeded` or similar mid-run failure there is no
    `AgentRunResult` to extract `output`/`all_messages()` from, but
    the partial message history is still on the `AgentRun`.
    """
    requests = [m for m in messages if isinstance(m, ModelRequest)]
    responses = [m for m in messages if isinstance(m, ModelResponse)]

    iterations: list[dict[str, Any]] = []
    cumulative: list[dict[str, Any]] = []
    for idx, resp in enumerate(responses):
        if idx < len(requests):
            cumulative.extend(_request_to_sent(requests[idx]))
        sent_snapshot = [dict(m) for m in cumulative]
        response_dict = _response_to_dict(resp)
        cumulative.append({"role": "assistant", "content": response_dict["content"]})
        if idx + 1 < len(requests):
            tool_results = _tool_returns_in(requests[idx + 1])
        else:
            tool_results = []
        iterations.append(
            {
                "messages_sent": sent_snapshot,
                "response": response_dict,
                "tool_results": tool_results,
            }
        )

    # Trailing request with no matching response — happens on the
    # failure path when the agent loop raised before the model
    # replied (e.g. usage-limit fired on the first call, or the
    # provider returned an error). Surface the prompt anyway so the
    # diagnostic trace shows what was sent.
    if len(requests) > len(responses):
        cumulative.extend(_request_to_sent(requests[len(responses)]))
        iterations.append(
            {
                "messages_sent": [dict(m) for m in cumulative],
                "response": None,
                "tool_results": [],
            }
        )

    input_t = output_t = cache_t = 0
    for resp in responses:
        u = resp.usage
        if u is None:
            continue
        input_t += u.input_tokens or 0
        output_t += u.output_tokens or 0
        cache_t += u.cache_read_tokens or 0

    if submit_args is None:
        submit_args = {}

    tool_calls: list[dict[str, Any]] = []
    # Map tool_call_id -> result content length (taken from subsequent
    # ToolReturnParts) so the legacy `result_len` field stays populated.
    result_lens: dict[str, int] = {}
    for req in requests:
        for part in req.parts:
            if isinstance(part, ToolReturnPart):
                result_lens[part.tool_call_id] = len(_stringify(part.content))
    for resp in responses:
        for part in resp.parts:
            if isinstance(part, ToolCallPart):
                tool_calls.append(
                    {
                        "name": part.tool_name,
                        "input": part.args_as_dict(),
                        "result_len": result_lens.get(part.tool_call_id or "", 0),
                    }
                )

    trace: dict[str, Any] = {
        "model": model,
        "system": system,
        "tools": tool_names,
        "submit_tool": submit_tool,
        "iterations": iterations,
        "result": {
            "submit_args": submit_args,
            "tool_calls": tool_calls,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_read_tokens": cache_t,
        },
    }
    if error is not None:
        trace["error"] = {"type": type(error).__name__, "message": str(error)}
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")


def _submit_args_from_output(output: Any) -> dict[str, Any]:
    if output is None:
        return {}
    if hasattr(output, "model_dump"):
        return output.model_dump(by_alias=True)
    return {}


def submit_args_from_result(result: Any) -> dict[str, Any]:
    """Extract the validated output as a dict that `apply_*_to_diff` can consume."""
    output = result.output
    return output.model_dump(by_alias=True)
