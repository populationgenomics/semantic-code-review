"""Verify the pydantic-ai → legacy-trace JSON adapter.

Builds a synthetic AgentRunResult-shaped object whose `all_messages()`
returns hand-crafted ModelRequest/ModelResponse pairs, runs the
adapter, and asserts the on-disk trace matches the shape `run_agentic`
emits today.
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
    assert tool_blocks == [
        {"type": "tool_use", "id": "c1", "name": "grep", "input": {"pattern": "x"}}
    ]
    assert it0["tool_results"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "a.py:1: x = 1"}
    ]
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
