"""Backend registry: lookup, unknown-name reporting, auto walk."""

from __future__ import annotations

import pytest
import typer

from semantic_code_review import backends
from semantic_code_review.backends.base import Backend
from semantic_code_review.config import BackendDef, BackendType, ScrConfig


def _cfg(backends_map: dict[str, BackendDef]) -> ScrConfig:
    return ScrConfig(backends=dict(backends_map))


def test_unknown_backend_lists_known_choices() -> None:
    cfg = _cfg(
        {
            "groq": BackendDef(
                type=BackendType.OPENAI_COMPAT,
                base_url="https://example.com",
                api_key_env="FAKE",
            ),
        }
    )
    with pytest.raises(typer.BadParameter, match="auto, groq"):
        backends.get("does-not-exist", config=cfg)


def test_auto_picks_first_supporting_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """`resolve_auto` walks adapters in `auto_priority` order.

    Stub adapters keep the test free of subprocess and network deps.
    """

    class _Yes(Backend):
        auto_priority = 5

        def resolve(self, *, model: str):
            raise NotImplementedError

        def supports_auto(self) -> bool:
            return True

    class _No(Backend):
        auto_priority = 0

        def resolve(self, *, model: str):
            raise NotImplementedError

        def supports_auto(self) -> bool:
            return False

    monkeypatch.setitem(backends._HANDLERS, BackendType.ANTHROPIC_SDK, _No)
    monkeypatch.setitem(backends._HANDLERS, BackendType.CLAUDE_CLI, _Yes)

    cfg = _cfg(
        {
            "claude-api": BackendDef(type=BackendType.ANTHROPIC_SDK),
            "claude-cli": BackendDef(type=BackendType.CLAUDE_CLI),
        }
    )
    assert backends.resolve_auto(config=cfg) == "claude-cli"


def test_auto_prefers_lower_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Always(Backend):
        auto_priority = 0

        def resolve(self, *, model: str):
            raise NotImplementedError

        def supports_auto(self) -> bool:
            return True

    class _AlsoAlways(Backend):
        auto_priority = 1

        def resolve(self, *, model: str):
            raise NotImplementedError

        def supports_auto(self) -> bool:
            return True

    monkeypatch.setitem(backends._HANDLERS, BackendType.ANTHROPIC_SDK, _Always)
    monkeypatch.setitem(backends._HANDLERS, BackendType.CLAUDE_CLI, _AlsoAlways)
    cfg = _cfg(
        {
            "claude-cli": BackendDef(type=BackendType.CLAUDE_CLI),
            "claude-api": BackendDef(type=BackendType.ANTHROPIC_SDK),
        }
    )
    assert backends.resolve_auto(config=cfg) == "claude-api"


def test_auto_raises_when_no_adapter_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Never(Backend):
        auto_priority = 0

        def resolve(self, *, model: str):
            raise NotImplementedError

        def supports_auto(self) -> bool:
            return False

    monkeypatch.setitem(backends._HANDLERS, BackendType.ANTHROPIC_SDK, _Never)
    monkeypatch.setitem(backends._HANDLERS, BackendType.CLAUDE_CLI, _Never)
    cfg = _cfg(
        {
            "claude-api": BackendDef(type=BackendType.ANTHROPIC_SDK),
            "claude-cli": BackendDef(type=BackendType.CLAUDE_CLI),
        }
    )
    with pytest.raises(typer.BadParameter, match="No Anthropic credentials"):
        backends.resolve_auto(config=cfg)


def test_auto_skips_adapters_with_priority_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-compat backends and Gemini variants don't participate in auto."""

    class _OptIn(Backend):
        auto_priority = 0

        def resolve(self, *, model: str):
            raise NotImplementedError

        def supports_auto(self) -> bool:
            return True

    class _OptOut(Backend):
        # Default auto_priority = None — never considered.
        def resolve(self, *, model: str):
            raise NotImplementedError

    monkeypatch.setitem(backends._HANDLERS, BackendType.OPENAI_COMPAT, _OptOut)
    monkeypatch.setitem(backends._HANDLERS, BackendType.ANTHROPIC_SDK, _OptIn)
    cfg = _cfg(
        {
            "groq": BackendDef(
                type=BackendType.OPENAI_COMPAT,
                base_url="https://example.com",
            ),
            "claude-api": BackendDef(type=BackendType.ANTHROPIC_SDK),
        }
    )
    assert backends.resolve_auto(config=cfg) == "claude-api"


def test_get_returns_adapter_with_bound_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get` returns an adapter that knows its own registered name."""

    captured: dict = {}

    class _Spy(Backend):
        def resolve(self, *, model: str):
            captured["name"] = self.name
            captured["bdef"] = self.bdef
            captured["model"] = model
            return "sentinel"  # type: ignore[return-value]

    monkeypatch.setitem(backends._HANDLERS, BackendType.OPENAI_COMPAT, _Spy)
    bdef = BackendDef(type=BackendType.OPENAI_COMPAT, base_url="https://x")
    cfg = _cfg({"my-llm": bdef})
    adapter = backends.get("my-llm", config=cfg)
    assert adapter.name == "my-llm"
    assert adapter.bdef is bdef
    assert adapter.resolve(model="some-model") == "sentinel"
    assert captured == {"name": "my-llm", "bdef": bdef, "model": "some-model"}


# --- agentic-loop bound -----------------------------------------------------


def test_sdk_backends_carry_a_request_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without one, a pass that can't answer its question runs to
    pydantic-ai's ceiling and returns no annotation at all — 51 requests
    and 1.5M input tokens for a hunk that produced nothing."""
    from semantic_code_review.backends.anthropic_sdk import AnthropicSdkBackend
    from semantic_code_review.config import BackendDef, BackendType

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bdef = BackendDef(type=BackendType.ANTHROPIC_SDK, default_model="m", api_key_env="ANTHROPIC_API_KEY")
    client = AnthropicSdkBackend("claude-api", bdef).resolve(model="m")
    assert client.request_limit == AnthropicSdkBackend.DEFAULT_REQUEST_LIMIT


def test_max_turns_bounds_the_sdk_loop_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """One knob for both transports: the CLI spends it on `--max-turns`,
    the SDK on pydantic-ai's request limit."""
    from semantic_code_review.backends.anthropic_sdk import AnthropicSdkBackend
    from semantic_code_review.config import BackendDef, BackendType

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bdef = BackendDef(type=BackendType.ANTHROPIC_SDK, default_model="m", api_key_env="ANTHROPIC_API_KEY", max_turns=7)
    client = AnthropicSdkBackend("claude-api", bdef).resolve(model="m")
    assert client.request_limit == 7


def test_default_matches_the_cli_driver_default() -> None:
    """The two transports should bound the loop the same way by default."""
    from semantic_code_review.backends.base import Backend

    assert Backend.DEFAULT_REQUEST_LIMIT == 20


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        # pydantic-ai carries a hard-coded list of Anthropic models with
        # native structured output; one outside it must stay on ToolOutput.
        ("claude-opus-4-8", True),
        ("claude-sonnet-4-7", False),
    ],
)
def test_anthropic_native_output_follows_the_model(
    monkeypatch: pytest.MonkeyPatch, model_name: str, expected: bool
) -> None:
    """Native output is a model capability, not a backend one. Forcing it
    on makes every hunk raise before the first request, which the pipeline
    catches per hunk — a run that exits 0 with no annotations."""
    from semantic_code_review.backends.anthropic_sdk import AnthropicSdkBackend
    from semantic_code_review.config import BackendDef, BackendType

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bdef = BackendDef(type=BackendType.ANTHROPIC_SDK, default_model=model_name, api_key_env="ANTHROPIC_API_KEY")
    client = AnthropicSdkBackend("claude-api", bdef).resolve(model=model_name)
    assert client.native_output is expected


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        # Google raises on NativeOutput + function tools below Gemini 3,
        # and the hunk agent always registers tools.
        ("gemini-2.5-pro", False),
        ("gemini-3-pro", True),
    ],
)
def test_google_native_output_follows_the_model(
    monkeypatch: pytest.MonkeyPatch, model_name: str, expected: bool
) -> None:
    from semantic_code_review.backends.google_sdk import GoogleSdkBackend
    from semantic_code_review.config import BackendDef, BackendType

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    bdef = BackendDef(type=BackendType.GOOGLE_SDK, default_model=model_name, api_key_env="GEMINI_API_KEY")
    client = GoogleSdkBackend("gemini-api", bdef).resolve(model=model_name)
    assert client.native_output is expected


def test_every_sdk_backend_bounds_its_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Gemini API-key branch shipped without one — unbounded is
    pydantic-ai's own 50-request ceiling, which is the burn this guards."""
    from semantic_code_review.backends.google_sdk import GoogleSdkBackend
    from semantic_code_review.backends.openai_compat import OpenAICompatBackend
    from semantic_code_review.config import BackendDef, BackendType

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    gem = GoogleSdkBackend(
        "gemini-api",
        BackendDef(type=BackendType.GOOGLE_SDK, default_model="gemini-3-pro", api_key_env="GEMINI_API_KEY"),
    ).resolve(model="gemini-3-pro")
    assert gem.request_limit == GoogleSdkBackend.DEFAULT_REQUEST_LIMIT

    monkeypatch.setenv("OAI_KEY", "k")
    oai = OpenAICompatBackend(
        "openai",
        BackendDef(
            type=BackendType.OPENAI_COMPAT,
            default_model="gpt-4o",
            api_key_env="OAI_KEY",
            base_url="https://example/v1",
        ),
    ).resolve(model="gpt-4o")
    assert oai.request_limit == OpenAICompatBackend.DEFAULT_REQUEST_LIMIT


def test_thinking_is_only_asked_for_alongside_native_output() -> None:
    """Anthropic rejects thinking with the forced tool choice `ToolOutput`
    produces, so a ToolOutput model asked to think raises on every call."""
    from semantic_code_review.augment.agents import Client
    from semantic_code_review.augment.hunks import _hunk_model_settings

    native = _hunk_model_settings(Client(model="m", native_output=True))  # type: ignore[arg-type]
    tools = _hunk_model_settings(Client(model="m", native_output=False))  # type: ignore[arg-type]
    assert "anthropic_thinking" in native
    assert "anthropic_thinking" not in tools
