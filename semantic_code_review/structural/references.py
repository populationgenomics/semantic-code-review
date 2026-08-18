"""Where a name is *used*, as opposed to where it is defined.

The definition side of this package answers "what changed"; this answers
"what still refers to it", which is what a reviewer — and the model —
asks far more often. Measured over one sweep, 73% of the model's tool
calls were reference questions ("is this still used", "confirm it is
gone"), against 12% asking what changed.

Two backends, because the right instrument differs by language:

- **Python** uses `ast`. The upstream tree-sitter tags query captures
  only `reference.call`, and `np.array(x)` captures as `array` — the
  module root is lost, so the commonest question of all ("is this
  import still used") is unanswerable from it. `ast` walks an
  `Attribute` chain back to its root `Name` and reads `Import` bindings
  directly, so it answers exactly that.
- **Everything else** uses the tags query's `@reference.*` captures,
  which we already compute while collecting definitions and discard.

Both are name-based, not scope-resolved: two distinct `helper`s in one
file are indistinguishable. That is still far better than a text search
— comments, strings, and substring hits are all excluded — but it is
not a resolver, and callers should not present it as one.
"""

from __future__ import annotations

import ast
import dataclasses
import re

from .parse import _load, language_for_path

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclasses.dataclass(frozen=True)
class Reference:
    """One use site. `kind` is the reference flavour where the backend
    reports one (`call`, `class`, `type`, `attribute`, `name`).
    """

    line: int
    kind: str
    text: str


class ParseFailed(Exception):
    """The source did not parse. Both sides of a diff come from git, so
    this should not happen; when it does it is a signal worth surfacing
    rather than swallowing — the caller falls back to a text search and
    says so.
    """


def references(source: str, path: str, name: str) -> list[Reference]:
    """Use sites of `name` in `source`. Raises `ParseFailed` if it can't parse."""
    lang = language_for_path(path)
    if lang == "python":
        return _python_references(source, name)
    if lang is None:
        raise ParseFailed(f"no grammar for {path}")
    return _tags_references(source, lang, name)


def python_bindings(source: str) -> dict[str, str]:
    """Imported names in `source`, mapped to what they refer to.

    `import numpy as np` -> `{"np": "numpy"}`. Lets a caller answer
    "is this import still used" as a lookup rather than a search.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ParseFailed(str(e)) from e
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out[a.asname or a.name] = f"{node.module or ''}.{a.name}".lstrip(".")
    return out


def _python_references(source: str, name: str) -> list[Reference]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ParseFailed(str(e)) from e
    lines = source.splitlines()
    out: list[Reference] = []
    seen: set[tuple[int, int]] = set()

    def add(node: ast.AST, kind: str) -> None:
        line = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)
        if (line, col) in seen:
            return
        seen.add((line, col))
        out.append(Reference(line=line, kind=kind, text=lines[line - 1].strip() if 0 < line <= len(lines) else ""))

    for node in ast.walk(tree):
        # A load of the bare name: `helper(x)`, `CONST + 1`, `C()`.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name:
            add(node, "name")
        # `np.array(x)` — credit the root, which is what "is numpy still
        # used" turns on and what the tags query loses.
        elif isinstance(node, ast.Attribute):
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id == name:
                add(root, "attribute")
    return sorted(out, key=lambda r: r.line)


def _tags_references(source: str, lang_name: str, name: str) -> list[Reference]:
    from tree_sitter import Parser, QueryCursor

    language, query = _load(lang_name)
    data = source.encode("utf-8")
    try:
        tree = Parser(language).parse(data)
    except Exception as e:
        raise ParseFailed(str(e)) from e
    caps = QueryCursor(query).captures(tree.root_node)
    out: list[Reference] = []
    for capture, nodes in caps.items():
        if not capture.startswith("reference"):
            continue
        kind = capture.split(".", 1)[-1]
        for node in nodes:
            text = data[node.start_byte : node.end_byte].decode("utf-8", "replace")
            # The capture spans the whole expression — `new Thing()`,
            # `foo.bar(x)` — so match on the identifiers inside its
            # head rather than the raw text. That covers the callee, a
            # dotted receiver, and a `new`-prefixed construction alike.
            head = text.split("(", 1)[0]
            if name in _IDENTIFIER.findall(head):
                out.append(Reference(line=node.start_point[0] + 1, kind=kind, text=text.splitlines()[0][:120]))
    return sorted(out, key=lambda r: r.line)


__all__ = ["ParseFailed", "Reference", "python_bindings", "references"]
