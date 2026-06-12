"""Tree-sitter parsing service (ADR 0001).

Runs a grammar's `tags.scm` tag query over source and folds the
`@definition.*` captures into a nested `Symbol` tree, nested by source
containment (a definition whose span sits inside another's is its
child). `@reference.*` captures are ignored — definitions only.

Adding a language is a deliberate act: register a loader returning its
`(Language, tags_query)` plus the extensions it owns. Everything past
the registry is language-agnostic; per-language signature extraction is
the one seam (`_SIGNATURE_EXTRACTORS`).

The whole surface degrades to an empty list rather than raising:
unsupported language, parse failure, or a malformed query all yield
`[]`, so callers never need a guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from tree_sitter import Language, Node, Parser, Query, QueryCursor

from .symbols import Symbol, SymbolRange


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LangSupport:
    extensions: tuple[str, ...]
    # Deferred so importing this module doesn't import every grammar wheel.
    loader: Callable[[], tuple[Language, str]]


def _python_support() -> tuple[Language, str]:
    import tree_sitter_python as tsp

    return Language(tsp.language()), tsp.TAGS_QUERY


# The upstream tree-sitter-typescript `tags.scm` is *additive* over the
# JavaScript one — it captures only the TS-specific constructs (interfaces,
# ambient signatures, abstract classes, namespaces) and inherits classes,
# functions, methods and arrow consts from the shared JS grammar. So the TS
# query is `js + ts`. Type aliases and enums are tagged by neither wheel;
# vendor those two captures here (ADR 0001: "vendor where absent").
_TS_EXTRA_TAGS = """
(type_alias_declaration name: (type_identifier) @name) @definition.type
(enum_declaration name: (identifier) @name) @definition.enum
"""


def _javascript_support() -> tuple[Language, str]:
    import tree_sitter_javascript as tsj

    return Language(tsj.language()), tsj.TAGS_QUERY


def _typescript_tags() -> str:
    import tree_sitter_javascript as tsj
    import tree_sitter_typescript as tst

    return "\n".join([tsj.TAGS_QUERY, tst.TAGS_QUERY, _TS_EXTRA_TAGS])


def _typescript_support() -> tuple[Language, str]:
    import tree_sitter_typescript as tst

    return Language(tst.language_typescript()), _typescript_tags()


def _tsx_support() -> tuple[Language, str]:
    import tree_sitter_typescript as tst

    return Language(tst.language_tsx()), _typescript_tags()


_REGISTRY: dict[str, _LangSupport] = {
    "python": _LangSupport(extensions=(".py",), loader=_python_support),
    "javascript": _LangSupport(
        extensions=(".js", ".jsx", ".mjs", ".cjs"), loader=_javascript_support
    ),
    # `.ts` and `.tsx` need distinct grammars: the `<T>` cast / JSX ambiguity
    # means the tsx grammar mis-parses plain TS and vice versa.
    "typescript": _LangSupport(
        extensions=(".ts", ".mts", ".cts"), loader=_typescript_support
    ),
    "tsx": _LangSupport(extensions=(".tsx",), loader=_tsx_support),
}

_EXT_TO_LANG: dict[str, str] = {
    ext: name for name, spec in _REGISTRY.items() for ext in spec.extensions
}


def language_for_path(path: str) -> str | None:
    """Supported language name for `path`'s extension, or `None`."""
    return _EXT_TO_LANG.get(Path(path).suffix)


@lru_cache(maxsize=None)
def _load(lang_name: str) -> tuple[Language, Query]:
    """Compile and cache the `(Language, Query)` for a language."""
    language, tags_query = _REGISTRY[lang_name].loader()
    return language, Query(language, tags_query)


# ---------------------------------------------------------------------------
# Parse → Symbol tree
# ---------------------------------------------------------------------------


@dataclass
class _RawDef:
    kind: str
    name: str
    node: Node


def outline_symbols(source: bytes | str, lang_name: str) -> list[Symbol]:
    """Nested top-level `Symbol` forest for `source` parsed as `lang_name`.

    Returns `[]` for an unsupported language or any parse/query failure —
    never raises (ADR 0001 graceful degradation).
    """
    if lang_name not in _REGISTRY:
        return []
    if isinstance(source, str):
        source = source.encode("utf-8")
    try:
        language, query = _load(lang_name)
        tree = Parser(language).parse(source)
        defs = _collect_definitions(tree.root_node, query, source)
        return _nest(defs, source, lang_name)
    except Exception:
        return []


def _collect_definitions(root: Node, query: Query, source: bytes) -> list[_RawDef]:
    """Pull `(kind, name, node)` for each `@definition.*` match."""
    out: list[_RawDef] = []
    for _, caps in QueryCursor(query).matches(root):
        def_node: Node | None = None
        kind: str | None = None
        for cap_name, nodes in caps.items():
            if cap_name.startswith("definition."):
                def_node = nodes[0]
                kind = cap_name[len("definition.") :]
        if def_node is None or kind is None:
            continue  # a reference.* (or otherwise nameless) match
        name_nodes = caps.get("name")
        if not name_nodes:
            continue
        out.append(_RawDef(kind=kind, name=_text(source, name_nodes[0]), node=def_node))
    return out


def _nest(defs: list[_RawDef], source: bytes, lang_name: str) -> list[Symbol]:
    """Fold flat definitions into a tree by source-span containment."""
    # Outermost first; ties broken so the wider span precedes the narrower.
    defs.sort(key=lambda d: (d.node.start_byte, -d.node.end_byte))
    roots: list[Symbol] = []
    stack: list[tuple[_RawDef, Symbol]] = []
    for d in defs:
        sym = Symbol(
            kind=d.kind,
            name=d.name,
            qualified_name=d.name,
            range=_range(d.node),
            signature=_signature(d.node, source, lang_name),
        )
        while stack and not _contains(stack[-1][0].node, d.node):
            stack.pop()
        if stack:
            parent_sym = stack[-1][1]
            sym.qualified_name = f"{parent_sym.qualified_name}.{d.name}"
            parent_sym.children.append(sym)
        else:
            roots.append(sym)
        stack.append((d, sym))
    return roots


def enclosing_symbol(symbols: list[Symbol], line: int) -> Symbol | None:
    """Innermost symbol whose 1-indexed line range encloses `line`.

    Descends into `children` for the most specific match (the method,
    not its enclosing class); `None` if no symbol covers the line.
    Siblings don't overlap, so the first cover at each level is the only
    one.
    """
    for s in symbols:
        if s.range.start_line <= line <= s.range.end_line:
            return enclosing_symbol(s.children, line) or s
    return None


def _contains(outer: Node, inner: Node) -> bool:
    return (
        outer.start_byte <= inner.start_byte
        and inner.end_byte <= outer.end_byte
        and outer.id != inner.id
    )


def _range(node: Node) -> SymbolRange:
    sp, ep = node.start_point, node.end_point
    return SymbolRange(
        start_line=sp.row + 1,
        end_line=ep.row + 1,
        start_col=sp.column,
        end_col=ep.column,
    )


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Signature extraction (per-language seam)
# ---------------------------------------------------------------------------


def _signature(node: Node, source: bytes, lang_name: str) -> str | None:
    extractor = _SIGNATURE_EXTRACTORS.get(lang_name)
    return extractor(node, source) if extractor else None


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _python_signature(node: Node, source: bytes) -> str | None:
    """Declared signature: header text up to (not including) the body.

    `def foo(a: int) -> str`, `class Bar(Base)`, `X: int` for an
    annotated assignment; `None` for a bare assignment (no declared type).
    """
    if node.type in ("function_definition", "class_definition"):
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        header = source[node.start_byte : end].decode("utf-8", errors="replace").rstrip()
        if header.endswith(":"):
            header = header[:-1].rstrip()
        return _collapse_ws(header)
    if node.type == "assignment":
        type_node = node.child_by_field_name("type")
        left = node.child_by_field_name("left")
        if type_node is not None and left is not None:
            return f"{_text(source, left)}: {_text(source, type_node)}"
        return None
    return None


def _ts_header_range(node: Node) -> tuple[int, int]:
    """Byte span of a TS/TSX definition's header — declaration up to its body.

    `start` reaches back to the parent `const`/`let`/`var` keyword for an
    arrow / function-expression const (whose definition node is the bare
    `variable_declarator`); `end` is the body's start, or the node end for a
    bodyless declaration (an ambient signature, interface member, …).
    """
    start = node.start_byte
    if node.type == "variable_declarator":
        parent = node.parent
        if parent is not None and parent.type in (
            "lexical_declaration",
            "variable_declaration",
        ):
            start = parent.start_byte
    body = node.child_by_field_name("body")
    if body is None:
        # arrow / function-expression const: the body lives under `value`.
        value = node.child_by_field_name("value")
        if value is not None:
            body = value.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return start, end


def _ts_signature(node: Node, source: bytes) -> str | None:
    """Declared signature for a TS/TSX definition: header text up to the body.

    `class Widget extends Base`, `function f(a: number): void`,
    `render(x: number): string`, `interface Foo`, `enum Color`; the full
    declaration for a type alias (`type Bar = string | number`). For an
    arrow / function-expression const the parent `const`/`let`/`var`
    keyword is kept (`const arrow = (n: number): number`).
    """
    if node.type == "type_alias_declaration":
        return _collapse_ws(_text(source, node).rstrip().rstrip(";"))
    start, end = _ts_header_range(node)
    header = source[start:end].decode("utf-8", errors="replace").rstrip()
    for suffix in ("=>", "=", "{", ":", ";"):
        if header.endswith(suffix):
            header = header[: -len(suffix)].rstrip()
            break
    return _collapse_ws(header) or None


# JavaScript is deliberately omitted: untyped JS carries no declared
# signature (ADR 0001 Slice 6), so `_signature` returns `None` for it.
_SIGNATURE_EXTRACTORS: dict[str, Callable[[Node, bytes], str | None]] = {
    "python": _python_signature,
    "typescript": _ts_signature,
    "tsx": _ts_signature,
}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

_SYMBOL_LIST_ADAPTER = None


def symbols_to_json(symbols: list[Symbol]) -> str:
    """Serialize a `Symbol` forest to a JSON array string."""
    global _SYMBOL_LIST_ADAPTER
    if _SYMBOL_LIST_ADAPTER is None:
        from pydantic import TypeAdapter

        _SYMBOL_LIST_ADAPTER = TypeAdapter(list[Symbol])
    return _SYMBOL_LIST_ADAPTER.dump_json(symbols).decode("utf-8")


def symbol_to_json(symbol: Symbol | None) -> str:
    """Serialize one optional `Symbol` to JSON; `"null"` for `None`."""
    return "null" if symbol is None else symbol.model_dump_json()
