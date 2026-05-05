"""GeminiSDKClient: SDK call mocked out, translation contract verified."""

from __future__ import annotations

from typing import Any

import pytest

from semantic_code_review.augment.gemini_sdk_client import (
    GeminiSDKClient,
    _clean_schema,
    _flatten_system,
    _translate_messages,
    _translate_response,
    _translate_tools,
)
from semantic_code_review.augment.runner import run_agentic


SUBMIT_TOOL = {
    "name": "submit_annotations",
    "description": "Emit hunk annotations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "smells": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["intent"],
        "$schema": "http://json-schema.org/draft-07/schema#",
    },
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_flatten_system_concatenates_text_blocks() -> None:
    out = _flatten_system([
        {"type": "text", "text": "you are reviewing"},
        {"type": "text", "text": "follow the schema"},
    ])
    assert out == "you are reviewing\n\nfollow the schema"


def test_flatten_system_drops_cache_control_markers() -> None:
    out = _flatten_system([
        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}},
    ])
    assert out == "x"


def test_clean_schema_strips_top_level_dialect_keys() -> None:
    cleaned = _clean_schema({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$defs": {"Foo": {"type": "string"}},
        "type": "object",
        "properties": {"a": {"type": "string"}},
    })
    assert "$schema" not in cleaned
    assert "$defs" not in cleaned
    assert cleaned["type"] == "object"
    assert cleaned["properties"]["a"]["type"] == "string"


def test_clean_schema_recurses_into_properties_and_items() -> None:
    cleaned = _clean_schema({
        "type": "object",
        "properties": {
            "tags": {
                "$schema": "x",  # nested junk that should be dropped
                "type": "array",
                "items": {"$schema": "y", "type": "string"},
            },
        },
    })
    assert "$schema" not in cleaned["properties"]["tags"]
    assert "$schema" not in cleaned["properties"]["tags"]["items"]


def test_translate_tools_packs_into_one_tool_object() -> None:
    out = _translate_tools([SUBMIT_TOOL, {
        "name": "read_file", "description": "", "input_schema": {"type": "object"},
    }])
    assert out is not None and len(out) == 1
    decls = out[0].function_declarations
    assert [d.name for d in decls] == ["submit_annotations", "read_file"]


def test_translate_tools_returns_none_for_empty_list() -> None:
    assert _translate_tools([]) is None


def test_translate_messages_maps_roles_and_blocks() -> None:
    contents = _translate_messages([
        {"role": "user", "content": [{"type": "text", "text": "USER"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents here"},
        ]},
    ])
    # 3 input messages → 3 Gemini Contents, with roles user/model/user.
    assert [c.role for c in contents] == ["user", "model", "user"]

    # Assistant message: text + function_call parts.
    assistant_parts = contents[1].parts
    assert assistant_parts[0].text == "thinking..."
    assert assistant_parts[1].function_call.name == "read_file"
    assert assistant_parts[1].function_call.args == {"path": "a.py"}

    # Tool result: function_response with the name resolved by tool_use_id
    # lookup, response wrapped as {"output": <str>} for string content.
    tool_part = contents[2].parts[0]
    assert tool_part.function_response.name == "read_file"
    assert tool_part.function_response.response == {"output": "file contents here"}


def test_translate_messages_passes_dict_tool_result_through() -> None:
    contents = _translate_messages([
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": {"matches": ["a.py:10", "b.py:20"]}},
        ]},
    ])
    resp = contents[1].parts[0].function_response.response
    assert resp == {"matches": ["a.py:10", "b.py:20"]}


# ---------------------------------------------------------------------------
# Response translation (Gemini → Anthropic)
# ---------------------------------------------------------------------------

class _FakePart:
    def __init__(self, *, text: str | None = None, function_call: Any | None = None) -> None:
        self.text = text
        self.function_call = function_call


class _FakeFunctionCall:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args


class _FakeFinishReason:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContent:
    def __init__(self, parts: list[_FakePart]) -> None:
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts: list[_FakePart], finish_reason: _FakeFinishReason | None = None) -> None:
        self.content = _FakeContent(parts)
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, prompt: int, candidates: int, cached: int = 0) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.cached_content_token_count = cached


class _FakeResponse:
    def __init__(self, *, candidates: list[_FakeCandidate], usage: _FakeUsage,
                 response_id: str = "resp-1") -> None:
        self.candidates = candidates
        self.usage_metadata = usage
        self.response_id = response_id


def test_translate_response_text_only_maps_to_end_turn() -> None:
    resp = _FakeResponse(
        candidates=[_FakeCandidate(
            parts=[_FakePart(text="hello world")],
            finish_reason=_FakeFinishReason("STOP"),
        )],
        usage=_FakeUsage(prompt=10, candidates=5),
    )
    out = _translate_response(resp, model="gemini-2.5-pro")
    assert out["stop_reason"] == "end_turn"
    assert out["content"] == [{"type": "text", "text": "hello world"}]
    assert out["usage"]["input_tokens"] == 10
    assert out["usage"]["output_tokens"] == 5


def test_translate_response_function_call_forces_tool_use_stop_reason() -> None:
    resp = _FakeResponse(
        candidates=[_FakeCandidate(
            parts=[
                _FakePart(text="let me check"),
                _FakePart(function_call=_FakeFunctionCall("read_file", {"path": "x.py"})),
            ],
            # Even though Gemini returns STOP here, presence of a function
            # call must drive run_agentic, so we override to tool_use.
            finish_reason=_FakeFinishReason("STOP"),
        )],
        usage=_FakeUsage(prompt=20, candidates=3),
    )
    out = _translate_response(resp, model="gemini-2.5-pro")
    assert out["stop_reason"] == "tool_use"
    assert out["content"][0] == {"type": "text", "text": "let me check"}
    assert out["content"][1]["type"] == "tool_use"
    assert out["content"][1]["name"] == "read_file"
    assert out["content"][1]["input"] == {"path": "x.py"}
    # Synthesised id must be present and non-empty (Gemini doesn't issue one).
    assert out["content"][1]["id"].startswith("gem-")


def test_translate_response_max_tokens_finish_reason_maps_correctly() -> None:
    resp = _FakeResponse(
        candidates=[_FakeCandidate(
            parts=[_FakePart(text="truncated")],
            finish_reason=_FakeFinishReason("MAX_TOKENS"),
        )],
        usage=_FakeUsage(prompt=1, candidates=1),
    )
    out = _translate_response(resp, model="gemini-2.5-pro")
    assert out["stop_reason"] == "max_tokens"


def test_translate_response_surfaces_cached_tokens_as_cache_read() -> None:
    resp = _FakeResponse(
        candidates=[_FakeCandidate(parts=[_FakePart(text="x")], finish_reason=_FakeFinishReason("STOP"))],
        usage=_FakeUsage(prompt=100, candidates=10, cached=80),
    )
    out = _translate_response(resp, model="gemini-2.5-pro")
    assert out["usage"]["cache_read_input_tokens"] == 80
    # We don't track explicit cache writes here.
    assert out["usage"]["cache_creation_input_tokens"] == 0


def test_translate_response_handles_empty_candidates() -> None:
    """Some safety-blocked responses come back with no candidates at all."""
    class _Empty:
        candidates = []
        usage_metadata = _FakeUsage(prompt=5, candidates=0)
        response_id = "blocked"
    out = _translate_response(_Empty(), model="gemini-2.5-pro")
    assert out["content"] == []
    assert out["stop_reason"] == "end_turn"


# ---------------------------------------------------------------------------
# create_message — runner integration
# ---------------------------------------------------------------------------

class _FakeAioModels:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_call: dict[str, Any] | None = None

    async def generate_content(self, *, model: str, contents: list[Any], config: Any) -> _FakeResponse:
        self.last_call = {"model": model, "contents": contents, "config": config}
        return self._response


class _FakeAio:
    def __init__(self, models: _FakeAioModels) -> None:
        self.models = models


class _FakeGenaiClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.aio = _FakeAio(_FakeAioModels(response))


@pytest.fixture
def submit_response() -> _FakeResponse:
    return _FakeResponse(
        candidates=[_FakeCandidate(
            parts=[_FakePart(function_call=_FakeFunctionCall(
                "submit_annotations", {"intent": "explain the refactor"},
            ))],
            finish_reason=_FakeFinishReason("STOP"),
        )],
        usage=_FakeUsage(prompt=42, candidates=17, cached=20),
    )


def test_create_message_translates_round_trip(submit_response: _FakeResponse) -> None:
    """Synchronous wrapper around the async create_message — exercises
    the whole translation path, with the SDK call faked out."""
    import asyncio

    fake_client = _FakeGenaiClient(submit_response)
    c = GeminiSDKClient(client=fake_client)

    response = asyncio.get_event_loop().run_until_complete(c.create_message(
        model="gemini-2.5-pro",
        max_tokens=4096,
        system=[{"type": "text", "text": "SYS"}],
        tools=[SUBMIT_TOOL],
        messages=[{"role": "user", "content": [{"type": "text", "text": "USER"}]}],
    ))

    # SDK was called with the right shape.
    call = fake_client.aio.models.last_call
    assert call is not None
    assert call["model"] == "gemini-2.5-pro"
    assert call["config"].system_instruction == "SYS"
    assert call["config"].max_output_tokens == 4096
    assert call["config"].tools is not None  # at least one Tool

    # Response was Anthropic-shaped.
    assert response["role"] == "assistant"
    assert response["stop_reason"] == "tool_use"
    assert response["content"][0]["name"] == "submit_annotations"
    assert response["content"][0]["input"] == {"intent": "explain the refactor"}
    assert response["usage"]["input_tokens"] == 42
    assert response["usage"]["output_tokens"] == 17
    assert response["usage"]["cache_read_input_tokens"] == 20


async def test_create_message_drives_run_agentic(submit_response: _FakeResponse) -> None:
    """End-to-end: run_agentic accepts the synthesized response and terminates."""
    fake_client = _FakeGenaiClient(submit_response)
    c = GeminiSDKClient(client=fake_client)

    result = await run_agentic(
        c,
        model="gemini-2.5-pro",
        system="SYS",
        user_content=[{"type": "text", "text": "USER"}],
        tools=[SUBMIT_TOOL],
        submit_tool_name="submit_annotations",
    )
    assert result.submit_args == {"intent": "explain the refactor"}
    assert result.input_tokens == 42
    assert result.cache_read_tokens == 20
