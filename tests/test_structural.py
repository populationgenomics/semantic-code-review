"""Tests for the tree-sitter structural layer (ADR 0001, Slices 1-2)."""

from __future__ import annotations

import json

from semantic_code_review.structural import (
    Symbol,
    diff_file,
    enclosing_symbol,
    language_for_path,
    merge,
    outline_symbols,
    symbol_to_json,
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


# --- enclosing_symbol ------------------------------------------------------


def test_enclosing_symbol_descends_to_innermost() -> None:
    syms = outline_symbols(SAMPLE, "python")
    # Line 15 is `pass`, the body of `inner`, nested under Bar.method.
    sym = enclosing_symbol(syms, 15)
    assert sym is not None and sym.qualified_name == "Bar.method.inner"


def test_enclosing_symbol_stops_at_class_body() -> None:
    syms = outline_symbols(SAMPLE, "python")
    # Line 11 is `attr = 5` — inside Bar but not in any method.
    sym = enclosing_symbol(syms, 11)
    assert sym is not None and sym.qualified_name == "Bar"


def test_enclosing_symbol_none_outside_any_definition() -> None:
    syms = outline_symbols(SAMPLE, "python")
    assert enclosing_symbol(syms, 1) is None  # the import line


# --- symbol_to_json --------------------------------------------------------


def test_symbol_to_json_none_is_null() -> None:
    assert symbol_to_json(None) == "null"


def test_symbol_to_json_serializes_one_symbol() -> None:
    foo = next(s for s in outline_symbols(SAMPLE, "python") if s.name == "foo")
    assert json.loads(symbol_to_json(foo))["qualified_name"] == "foo"


# --- diff_file / merge -----------------------------------------------------

_BASE = '''X = 1

def keep():
    return 1

def gone():
    return 2

class C:
    def m(self):
        return 1
'''

_HEAD = '''X = 1

def keep():
    return 1

def added():
    return 3

class C:
    def m(self):
        # one more line shifts the range
        return 1
'''


def test_diff_added_removed_by_qualified_name() -> None:
    delta = diff_file("m.py", outline_symbols(_BASE, "python"), outline_symbols(_HEAD, "python"))
    assert [c.qualified_name for c in delta.added] == ["added"]
    assert [c.qualified_name for c in delta.removed] == ["gone"]


def test_diff_modified_is_differing_range() -> None:
    delta = diff_file("m.py", outline_symbols(_BASE, "python"), outline_symbols(_HEAD, "python"))
    # C.m gained a comment line → its range differs → modified. C's range
    # also shifts. `keep` and `X` are byte-identical on both sides.
    qns = {c.qualified_name for c in delta.modified}
    assert "C.m" in qns
    assert "keep" not in qns and "X" not in qns


def test_diff_carries_path_and_live_side_range() -> None:
    delta = diff_file("m.py", outline_symbols(_BASE, "python"), outline_symbols(_HEAD, "python"))
    added = delta.added[0]
    assert added.path == "m.py"
    assert added.kind == "function" and added.signature == "def added()"


def test_diff_added_file_is_all_added() -> None:
    delta = diff_file("new.py", [], outline_symbols(_HEAD, "python"))
    assert not delta.removed and not delta.modified
    assert {c.qualified_name for c in delta.added} >= {"X", "keep", "added", "C", "C.m"}


def test_diff_deleted_file_is_all_removed() -> None:
    delta = diff_file("old.py", outline_symbols(_BASE, "python"), [])
    assert not delta.added and not delta.modified
    assert "gone" in {c.qualified_name for c in delta.removed}


def test_merge_concatenates_per_file_deltas() -> None:
    d1 = diff_file("a.py", [], outline_symbols("def a():\n    pass\n", "python"))
    d2 = diff_file("b.py", [], outline_symbols("def b():\n    pass\n", "python"))
    merged = merge([d1, d2])
    assert {(c.path, c.qualified_name) for c in merged.added} == {("a.py", "a"), ("b.py", "b")}


# --- TypeScript / TSX / JavaScript (Slice 6) -------------------------------

_TS_SAMPLE = '''interface Foo {
  a: number;
}

type Bar = string | number;

enum Color { Red, Green }

abstract class Base {
  abstract render(): string;
}

class Widget extends Base {
  render(x: number): string {
    return "x";
  }
}

function freestanding(a: number): void {}

const arrow = (n: number): number => n + 1;
'''


def test_ts_extension_detection() -> None:
    assert language_for_path("src/app.ts") == "typescript"
    assert language_for_path("src/app.mts") == "typescript"
    assert language_for_path("ui/comp.tsx") == "tsx"
    assert language_for_path("lib/util.js") == "javascript"
    assert language_for_path("lib/util.jsx") == "javascript"
    assert language_for_path("lib/util.mjs") == "javascript"


def test_ts_top_level_kinds() -> None:
    top = _by_name(outline_symbols(_TS_SAMPLE, "typescript"))
    assert top["Foo"].kind == "interface"
    assert top["Bar"].kind == "type"
    assert top["Color"].kind == "enum"
    assert top["Base"].kind == "class"
    assert top["Widget"].kind == "class"
    assert top["freestanding"].kind == "function"
    assert top["arrow"].kind == "function"


def test_ts_signatures() -> None:
    top = _by_name(outline_symbols(_TS_SAMPLE, "typescript"))
    assert top["Foo"].signature == "interface Foo"
    assert top["Bar"].signature == "type Bar = string | number"
    assert top["Widget"].signature == "class Widget extends Base"
    assert top["freestanding"].signature == "function freestanding(a: number): void"
    # The arrow const keeps its declaration keyword; the `=>` body is dropped.
    assert top["arrow"].signature == "const arrow = (n: number): number"


def test_ts_method_nests_under_class() -> None:
    top = _by_name(outline_symbols(_TS_SAMPLE, "typescript"))
    method = _by_name(top["Widget"].children)["render"]
    assert method.kind == "method"
    assert method.qualified_name == "Widget.render"
    assert method.signature == "render(x: number): string"


def test_tsx_parses_jsx_returning_component() -> None:
    src = (
        "export function Button(props: {label: string}): JSX.Element {\n"
        "  return <button>{props.label}</button>;\n"
        "}\n"
    )
    top = _by_name(outline_symbols(src, "tsx"))
    assert top["Button"].kind == "function"
    assert top["Button"].signature == "function Button(props: {label: string}): JSX.Element"


def test_js_outline_has_no_signature() -> None:
    """Untyped JS carries no declared signature (Slice 6)."""
    src = (
        "function greet(name) {\n"
        "  return name;\n"
        "}\n"
        "\n"
        "class Box {\n"
        "  open() { return 1; }\n"
        "}\n"
    )
    top = _by_name(outline_symbols(src, "javascript"))
    assert set(top) >= {"greet", "Box"}
    assert top["greet"].kind == "function" and top["greet"].signature is None
    box_method = _by_name(top["Box"].children)["open"]
    assert box_method.qualified_name == "Box.open" and box_method.signature is None


def test_ts_changed_symbols_diff() -> None:
    base = outline_symbols(
        "function keep(): void {}\nfunction gone(): void {}\n", "typescript"
    )
    head = outline_symbols(
        "function keep(): void {}\nfunction added(): void {}\n", "typescript"
    )
    delta = diff_file("m.ts", base, head)
    assert [c.qualified_name for c in delta.added] == ["added"]
    assert [c.qualified_name for c in delta.removed] == ["gone"]


def test_ts_enclosing_symbol() -> None:
    syms = outline_symbols(_TS_SAMPLE, "typescript")
    # `return "x";` sits inside Widget.render.
    line = _TS_SAMPLE.splitlines().index('    return "x";') + 1
    sym = enclosing_symbol(syms, line)
    assert sym is not None and sym.qualified_name == "Widget.render"
