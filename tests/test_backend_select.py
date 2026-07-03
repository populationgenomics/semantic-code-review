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
