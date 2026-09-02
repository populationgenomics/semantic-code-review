"""Tests for the tree-sitter structural layer (ADR 0001, Slices 1-2)."""

from __future__ import annotations

import json

from semantic_code_review.structural import (
    ChangeReason,
    Symbol,
    SymbolDelta,
    diff_file,
    enclosing_symbol,
    language_for_path,
    merge,
    outline_symbols,
    symbol_to_json,
    symbols_to_json,
)

SAMPLE = """import os

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
"""


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

_BASE = """X = 1

def keep():
    return 1

def gone():
    return 2

class C:
    def m(self):
        return 1
"""

_HEAD = """X = 1

def keep():
    return 1

def added():
    return 3

class C:
    def m(self):
        # one more line shifts the range
        return 1
"""


def _delta(base: str = _BASE, head: str = _HEAD, path: str = "m.py") -> SymbolDelta:
    return diff_file(
        path,
        outline_symbols(base, "python"),
        outline_symbols(head, "python"),
        base_src=base,
        head_src=head,
    )


def test_diff_added_removed_by_qualified_name() -> None:
    delta = _delta()
    assert [c.qualified_name for c in delta.added] == ["added"]
    assert [c.qualified_name for c in delta.removed] == ["gone"]


def test_diff_modified_is_a_real_code_change() -> None:
    delta = _delta()
    # C.m gained a comment line → its text differs → modified. C's text
    # differs too (its child grew). `keep` and `X` are byte-identical.
    assert {c.qualified_name for c in delta.modified} == {"C", "C.m"}


def test_diff_carries_path_and_live_side_range() -> None:
    added = _delta().added[0]
    assert added.path == "m.py"
    assert added.kind == "function" and added.signature == "def added()"


def test_modified_reason_is_signature_when_the_declaration_moves() -> None:
    base = "def f(a):\n    return a\n"
    head = "def f(a, *, b=1):\n    return a\n"
    assert [(c.qualified_name, c.reason) for c in _delta(base, head).modified] == [("f", ChangeReason.SIGNATURE)]


def test_modified_reason_is_body_when_only_the_implementation_moves() -> None:
    base = "def f(a):\n    return a\n"
    head = "def f(a):\n    return a\n    # trailing\n"
    assert [(c.qualified_name, c.reason) for c in _delta(base, head).modified] == [("f", ChangeReason.BODY)]


def test_a_pure_line_shift_is_moved_not_modified() -> None:
    base = "def f():\n    return 1\n"
    head = "X = 0\n\n\ndef f():\n    return 1\n"
    delta = _delta(base, head)
    assert not delta.modified
    assert [(c.qualified_name, c.from_path) for c in delta.moved] == [("f", None)]


def test_a_line_count_neutral_body_edit_under_a_shift_is_body_not_moved() -> None:
    """Span *length* cannot tell these apart — the text can.

    `f` shifts down two lines and edits one line in place, so
    `end - start` is identical on both sides. Measured on a real diff, a
    length comparison misfiled one of six genuine API changes this way.
    """
    base = "def f():\n    return 1\n"
    head = "X = 0\n\ndef f():\n    return 2\n"
    delta = _delta(base, head)
    assert not delta.moved
    assert [(c.qualified_name, c.reason) for c in delta.modified] == [("f", ChangeReason.BODY)]


def test_diff_added_file_is_all_added() -> None:
    delta = diff_file("new.py", [], outline_symbols(_HEAD, "python"), base_src=None, head_src=_HEAD)
    assert not delta.removed and not delta.modified and not delta.moved
    assert {c.qualified_name for c in delta.added} >= {"X", "keep", "added", "C", "C.m"}


def test_diff_deleted_file_is_all_removed() -> None:
    delta = diff_file("old.py", outline_symbols(_BASE, "python"), [], base_src=_BASE, head_src=None)
    assert not delta.added and not delta.modified and not delta.moved
    assert "gone" in {c.qualified_name for c in delta.removed}


def test_merge_concatenates_per_file_deltas() -> None:
    d1 = _delta("", "def a():\n    pass\n", path="a.py")
    d2 = _delta("", "def b():\n    pass\n", path="b.py")
    merged = merge([d1, d2])
    assert {(c.path, c.qualified_name) for c in merged.added} == {("a.py", "a"), ("b.py", "b")}


def test_body_sha_stays_out_of_the_wire_format() -> None:
    """It is an internal comparison key, not part of the `Symbol` currency."""
    dumped = json.loads(_delta().model_dump_json())
    assert all("body_sha" not in c for bucket in dumped.values() for c in bucket)


# --- cross-file moves (resolved diff-wide, in `merge`) ---------------------

_FN = "def helper(x: int) -> int:\n    return x + 1\n"


def test_merge_collapses_a_cross_file_move() -> None:
    merged = merge([_delta(_FN, "", path="old.py"), _delta("", _FN, path="new.py")])
    assert not merged.added and not merged.removed
    assert [(c.path, c.from_path, c.qualified_name) for c in merged.moved] == [("new.py", "old.py", "helper")]


def test_merge_leaves_a_moved_and_edited_symbol_as_two_events() -> None:
    """Only byte-identical code is a move by construction. Pairing an
    edited definition with a same-named removal would be an inference,
    which this layer does not make (ADR 0001).
    """
    merged = merge([_delta(_FN, "", path="old.py"), _delta("", _FN.replace("x + 1", "x + 2"), path="new.py")])
    assert not merged.moved
    assert [c.path for c in merged.added] == ["new.py"]
    assert [c.path for c in merged.removed] == ["old.py"]


def test_merge_does_not_link_same_named_symbols_with_different_bodies() -> None:
    gone = _delta("def helper():\n    return 1\n", "", path="a.py")
    arrived = _delta("", "def helper():\n    return 2\n", path="b.py")
    merged = merge([gone, arrived])
    assert not merged.moved and len(merged.added) == 1 and len(merged.removed) == 1


# --- TypeScript / TSX / JavaScript (Slice 6) -------------------------------

_TS_SAMPLE = """interface Foo {
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
"""


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
    src = "export function Button(props: {label: string}): JSX.Element {\n  return <button>{props.label}</button>;\n}\n"
    top = _by_name(outline_symbols(src, "tsx"))
    assert top["Button"].kind == "function"
    assert top["Button"].signature == "function Button(props: {label: string}): JSX.Element"


def test_js_outline_has_no_signature() -> None:
    """Untyped JS carries no declared signature (Slice 6)."""
    src = "function greet(name) {\n  return name;\n}\n\nclass Box {\n  open() { return 1; }\n}\n"
    top = _by_name(outline_symbols(src, "javascript"))
    assert set(top) >= {"greet", "Box"}
    assert top["greet"].kind == "function" and top["greet"].signature is None
    box_method = _by_name(top["Box"].children)["open"]
    assert box_method.qualified_name == "Box.open" and box_method.signature is None


_TS_BASE = "function keep(): void {}\nfunction gone(): void {}\n"
_TS_HEAD = "function keep(): void {}\nfunction added(): void {}\n"


def test_ts_changed_symbols_diff() -> None:
    base = outline_symbols(_TS_BASE, "typescript")
    head = outline_symbols(_TS_HEAD, "typescript")
    delta = diff_file("m.ts", base, head, base_src=_TS_BASE, head_src=_TS_HEAD)
    assert [c.qualified_name for c in delta.added] == ["added"]
    assert [c.qualified_name for c in delta.removed] == ["gone"]


def test_ts_enclosing_symbol() -> None:
    syms = outline_symbols(_TS_SAMPLE, "typescript")
    # `return "x";` sits inside Widget.render.
    line = _TS_SAMPLE.splitlines().index('    return "x";') + 1
    sym = enclosing_symbol(syms, line)
    assert sym is not None and sym.qualified_name == "Widget.render"
