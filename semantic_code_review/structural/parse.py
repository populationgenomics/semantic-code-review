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


_REGISTRY: dict[str, _LangSupport] = {
    "python": _LangSupport(extensions=(".py",), loader=_python_support),
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


_SIGNATURE_EXTRACTORS: dict[str, Callable[[Node, bytes], str | None]] = {
    "python": _python_signature,
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
