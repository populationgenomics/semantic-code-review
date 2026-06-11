"""Deterministic structural layer — tree-sitter, no LLM (see ADR 0001).

Where the code is and what it literally declares: every definition, its
name, qualified name, exact range, and declared signature, as a nested
`Symbol` tree. This is the *skeleton*; meaning (what a reference resolves
to, why a change was made) stays the LLM's job.

`Symbol` is the single internal currency every consumer reads (the
`outline` tool, the overview seed, the sidebar Symbols axis). Parsing is
a runtime service: `outline_symbols(source, lang)` parses one file;
`language_for_path(path)` maps an extension to a supported language, or
`None`. Unsupported language or parse failure degrades to an empty
result — never a raise.
"""

from __future__ import annotations

from .diff import ChangedSymbol, SymbolDelta, diff_file, merge
from .parse import (
    enclosing_symbol,
    language_for_path,
    outline_symbols,
    symbol_to_json,
    symbols_to_json,
)
from .symbols import Symbol, SymbolRange

__all__ = [
    "ChangedSymbol",
    "Symbol",
    "SymbolDelta",
    "SymbolRange",
    "diff_file",
    "enclosing_symbol",
    "language_for_path",
    "merge",
    "outline_symbols",
    "symbol_to_json",
    "symbols_to_json",
]
