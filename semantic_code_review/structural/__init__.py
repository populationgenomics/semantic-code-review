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

from .parse import language_for_path, outline_symbols, symbols_to_json
from .symbols import Symbol, SymbolRange

__all__ = [
    "Symbol",
    "SymbolRange",
    "language_for_path",
    "outline_symbols",
    "symbols_to_json",
]
