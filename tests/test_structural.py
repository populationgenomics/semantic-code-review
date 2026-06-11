"""Tests for the tree-sitter structural layer (ADR 0001, Slice 1)."""

from __future__ import annotations

import json

from semantic_code_review.structural import (
    Symbol,
    language_for_path,
    outline_symbols,
    symbols_to_json,
)

SAMPLE = '''import os

X = 1
Y: int = 2

def foo(a: int,
        b) -> str:
    return "x"

class Bar(Base):
    attr = 5

    def method(self, q):
        def inner():
            pass
        return q
'''


def _by_name(symbols: list[Symbol]) -> dict[str, Symbol]:
    return {s.name: s for s in symbols}


# --- language detection ----------------------------------------------------


def test_language_for_path_python() -> None:
    assert language_for_path("pkg/mod.py") == "python"


def test_language_for_path_unsupported_is_none() -> None:
    assert language_for_path("main.rs") is None
    assert language_for_path("README") is None


# --- outline_symbols -------------------------------------------------------


def test_top_level_definitions_enumerated() -> None:
    top = _by_name(outline_symbols(SAMPLE, "python"))
    assert set(top) == {"X", "Y", "foo", "Bar"}
    assert top["X"].kind == "constant"
    assert top["foo"].kind == "function"
    assert top["Bar"].kind == "class"


def test_nesting_and_qualified_names() -> None:
    top = _by_name(outline_symbols(SAMPLE, "python"))
    method = _by_name(top["Bar"].children)["method"]
    assert method.qualified_name == "Bar.method"
    inner = _by_name(method.children)["inner"]
    assert inner.qualified_name == "Bar.method.inner"


def test_class_body_assignment_is_not_a_constant() -> None:
    """tags.scm captures only module-level constants — `attr` stays out."""
    top = _by_name(outline_symbols(SAMPLE, "python"))
    assert [c.name for c in top["Bar"].children] == ["method"]


def test_signatures() -> None:
    top = _by_name(outline_symbols(SAMPLE, "python"))
    # Multi-line params collapse to one line; trailing colon dropped.
    assert top["foo"].signature == "def foo(a: int, b) -> str"
    assert top["Bar"].signature == "class Bar(Base)"
    # Annotated assignment carries the declared type; bare one does not.
    assert top["Y"].signature == "Y: int"
    assert top["X"].signature is None


def test_ranges_are_one_indexed() -> None:
    top = _by_name(outline_symbols(SAMPLE, "python"))
    assert top["X"].range.start_line == 3
    assert top["foo"].range.start_line == 6
    assert top["foo"].range.end_line == 8


# --- graceful degradation --------------------------------------------------


def test_unsupported_language_returns_empty() -> None:
    assert outline_symbols("fn main() {}", "rust") == []


def test_syntax_error_does_not_raise() -> None:
    # tree-sitter is error-tolerant: the well-formed def still surfaces.
    out = outline_symbols("def ok():\n    pass\n\ndef broken(:\n", "python")
    assert "ok" in {s.name for s in out}


def test_empty_source_returns_empty() -> None:
    assert outline_symbols("", "python") == []


def test_accepts_bytes() -> None:
    out = outline_symbols(b"def f():\n    pass\n", "python")
    assert [s.name for s in out] == ["f"]


# --- serialization ---------------------------------------------------------


def test_symbols_to_json_round_trips() -> None:
    syms = outline_symbols(SAMPLE, "python")
    parsed = json.loads(symbols_to_json(syms))
    assert isinstance(parsed, list)
    bar = next(s for s in parsed if s["name"] == "Bar")
    assert bar["children"][0]["qualified_name"] == "Bar.method"


def test_empty_forest_serializes_to_empty_array() -> None:
    assert symbols_to_json([]) == "[]"
