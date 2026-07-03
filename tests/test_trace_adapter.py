"""Verify the pydantic-ai → legacy-trace JSON adapter.

Builds a synthetic AgentRunResult-shaped object whose `all_messages()`
returns hand-crafted ModelRequest/ModelResponse pairs, runs the
adapter, and asserts the on-disk trace matches the shape that
existed before the pydantic-ai migration.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from semantic_code_review.augment.trace_adapter import (
    submit_args_from_result,
    write_partial_trace,
    write_pydantic_ai_trace,
)


class _FakeOutput:
    def __init__(self, dump: dict) -> None:
        self._dump = dump

    def model_dump(self, by_alias: bool = False) -> dict:
        return dict(self._dump)


class _FakeResult:
    def __init__(self, messages: list, output: _FakeOutput) -> None:
        self._messages = messages
        self.output = output

    def all_messages(self) -> list:
        return self._messages


def _ts() -> datetime:
    return datetime(2026, 5, 5, 0, 0, 0)


def test_adapter_emits_legacy_shape(tmp_path: Path) -> None:
    """One iteration: user prompt → text + tool call → tool result → final."""
    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="sys", timestamp=_ts()),
                UserPromptPart(content="hello", timestamp=_ts()),
            ],
            timestamp=_ts(),
        ),
        ModelResponse(
            parts=[
                TextPart(content="thinking..."),
                ToolCallPart(tool_name="grep", args={"pattern": "x"}, tool_call_id="c1"),
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=4, cache_read_tokens=2),
            model_name="test-model",
            finish_reason="tool_calls",
            timestamp=_ts(),
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="grep",
                    content="a.py:1: x = 1",
                    tool_call_id="c1",
                    timestamp=_ts(),
                ),
            ],
            timestamp=_ts(),
        ),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="submit_annotations",
                    args={"intent": "stub"},
                    tool_call_id="c2",
                ),
            ],
            usage=RequestUsage(input_tokens=12, output_tokens=3),
            model_name="test-model",
            finish_reason="tool_calls",
            timestamp=_ts(),
        ),
    ]
    result = _FakeResult(messages=messages, output=_FakeOutput({"intent": "stub"}))

    trace_path = tmp_path / "trace.json"
    write_pydantic_ai_trace(
        result,
        trace_path=trace_path,
        model="test-model",
        system="sys",
        tool_names=["grep"],
        submit_tool="submit_annotations",
    )

    trace = json.loads(trace_path.read_text())
    assert trace["model"] == "test-model"
    assert trace["system"] == "sys"
    assert trace["submit_tool"] == "submit_annotations"
    assert trace["tools"] == ["grep"]
    assert len(trace["iterations"]) == 2

    it0 = trace["iterations"][0]
    assert it0["messages_sent"][0] == {"role": "user", "content": "hello"}
    # The tool call shows up in the response content.
    tool_blocks = [b for b in it0["response"]["content"] if b["type"] == "tool_use"]
    assert tool_blocks == [{"type": "tool_use", "id": "c1", "name": "grep", "input": {"pattern": "x"}}]
    assert it0["tool_results"] == [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py:1: x = 1"}]
    # Token aggregation across responses.
    assert trace["result"]["input_tokens"] == 22
    assert trace["result"]["output_tokens"] == 7
    assert trace["result"]["cache_read_tokens"] == 2
    assert trace["result"]["submit_args"] == {"intent": "stub"}

    # tool_calls list flattens across responses; result_len carries from
    # the matching ToolReturnPart.
    grep_call = next(c for c in trace["result"]["tool_calls"] if c["name"] == "grep")
    assert grep_call["input"] == {"pattern": "x"}
    assert grep_call["result_len"] == len("a.py:1: x = 1")


def test_submit_args_from_result_uses_by_alias() -> None:
    """OverviewEdge dumps with from/to aliases — by_alias must propagate."""

    class WithAlias:
        def model_dump(self, by_alias: bool = False) -> dict:
            return {"from": "x", "to": "y"} if by_alias else {"src": "x", "dst": "y"}

    class R:
        output = WithAlias()

    out = submit_args_from_result(R())
    assert out == {"from": "x", "to": "y"}


def test_adapter_handles_no_messages(tmp_path: Path) -> None:
    """Defensive: an empty message list still produces a parseable trace."""
    result = _FakeResult(messages=[], output=_FakeOutput({"summary": ""}))
    trace_path = tmp_path / "trace.json"
    write_pydantic_ai_trace(
        result,
        trace_path=trace_path,
        model="m",
        system="s",
        tool_names=[],
        submit_tool="submit_overview",
    )
    trace = json.loads(trace_path.read_text())
    assert trace["iterations"] == []
    assert trace["result"]["submit_args"] == {"summary": ""}


def test_partial_trace_captures_tool_calls_and_error(tmp_path: Path) -> None:
    """When the agent loop blows the request cap mid-run, the partial
    trace must still record the prompt, the tool calls that did fire,
    and the failure metadata so we can see what the model was doing
    when it ran out of budget."""
    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="sys", timestamp=_ts()),
                UserPromptPart(content="annotate this hunk", timestamp=_ts()),
            ],
            timestamp=_ts(),
        ),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="grep", args={"pattern": "foo"}, tool_call_id="c1"),
            ],
            usage=RequestUsage(input_tokens=20, output_tokens=5),
            model_name="m",
            finish_reason="tool_calls",
            timestamp=_ts(),
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="grep",
                    content="a.py:1: foo",
                    tool_call_id="c1",
                    timestamp=_ts(),
                ),
            ],
            timestamp=_ts(),
        ),
        # No final ModelResponse — the run died here.
    ]

    trace_path = tmp_path / "partial.json"
    write_partial_trace(
        messages,
        trace_path=trace_path,
        model="m",
        system="sys",
        tool_names=["grep"],
        submit_tool="submit_annotations",
        error=RuntimeError("request_limit of 50 exceeded"),
    )

    trace = json.loads(trace_path.read_text())
    assert trace["error"] == {
        "type": "RuntimeError",
        "message": "request_limit of 50 exceeded",
    }
    # The user prompt is preserved.
    assert trace["iterations"][0]["messages_sent"][0] == {
        "role": "user",
        "content": "annotate this hunk",
    }
    # The grep call is captured even though no submit happened.
    grep_calls = [c for c in trace["result"]["tool_calls"] if c["name"] == "grep"]
    assert grep_calls and grep_calls[0]["input"] == {"pattern": "foo"}
    # No submit args populated on the failure path.
    assert trace["result"]["submit_args"] == {}


def test_response_falls_back_for_unknown_part_types(tmp_path: Path) -> None:
    """Parts the adapter doesn't render specifically (ThinkingPart and
    similar) still leave a class-name + repr trace so a misbehaving
    model run's output is inspectable."""
    from pydantic_ai.messages import ThinkingPart

    messages = [
        ModelRequest(
            parts=[UserPromptPart(content="hi", timestamp=_ts())],
            timestamp=_ts(),
        ),
        ModelResponse(
            parts=[ThinkingPart(content="let me think about this carefully")],
            usage=RequestUsage(input_tokens=5, output_tokens=2),
            model_name="m",
            finish_reason="tool_calls",
            timestamp=_ts(),
        ),
    ]
    trace_path = tmp_path / "thinking.json"
    write_partial_trace(
        messages,
        trace_path=trace_path,
        model="m",
        system="s",
        tool_names=[],
        submit_tool="submit",
        error=RuntimeError("validation failed"),
    )
    trace = json.loads(trace_path.read_text())
    blocks = trace["iterations"][0]["response"]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "ThinkingPart"
    assert "let me think" in blocks[0]["repr"]


def test_response_captures_malformed_tool_call_args(tmp_path: Path) -> None:
    """When a model emits a ToolCallPart whose args are invalid JSON
    (the usual cause of UnexpectedModelBehavior in output validation),
    the trace must carry the raw args + parse error rather than
    silently dropping the part."""
    messages = [
        ModelRequest(
            parts=[UserPromptPart(content="hi", timestamp=_ts())],
            timestamp=_ts(),
        ),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="submit_annotations",
                    # Pass an invalid JSON string — args_as_dict will
                    # raise on this and the adapter should capture it.
                    args='{"intent": "ok", "smells": [',
                    tool_call_id="c1",
                ),
            ],
            usage=RequestUsage(input_tokens=5, output_tokens=10),
            model_name="m",
            finish_reason="tool_calls",
            timestamp=_ts(),
        ),
    ]
    trace_path = tmp_path / "bad.json"
    write_partial_trace(
        messages,
        trace_path=trace_path,
        model="m",
        system="s",
        tool_names=["submit_annotations"],
        submit_tool="submit_annotations",
        error=RuntimeError("validation failed"),
    )
    trace = json.loads(trace_path.read_text())
    blocks = trace["iterations"][0]["response"]["content"]
    tool_use = next(b for b in blocks if b["type"] == "tool_use")
    # pydantic-ai exposes the raw text under `INVALID_JSON` rather than
    # raising — either way the raw string the model emitted is what we
    # want preserved in the trace.
    raw = tool_use["input"]
    raw_text = raw.get("INVALID_JSON") or raw.get("_raw") or ""
    assert "smells" in raw_text


def test_partial_trace_no_error_field_when_no_error(tmp_path: Path) -> None:
    """`write_partial_trace` doubles as the engine for the success
    path; callers omit `error=`, and the trace must not carry an
    `error` key in that case."""
    trace_path = tmp_path / "ok.json"
    write_partial_trace(
        [],
        trace_path=trace_path,
        model="m",
        system="s",
        tool_names=[],
        submit_tool="submit",
        submit_args={"intent": "ok"},
    )
    trace = json.loads(trace_path.read_text())
    assert "error" not in trace
    assert trace["result"]["submit_args"] == {"intent": "ok"}
