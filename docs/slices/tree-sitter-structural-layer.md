# Slices — Tree-sitter structural layer

Implements [ADR 0001](../adr/0001-tree-sitter-structural-layer.md).
Vertical slices, ordered. Each ends in something that ships and is
exercisable on its own; later slices widen coverage or add consumers
but never block earlier ones from landing.

The shared currency is the normalized `Symbol{kind, name,
qualified_name, range, signature?, children[]}` tree (see ADR). It is
introduced in Slice 1 and never re-shaped afterwards.

---

## Slice 1 — Tracer bullet: parse Python, expose `outline`

Thinnest end-to-end path. Proves the whole stack — pinned native
grammar, parsing service, `Symbol` schema, `@_tool` export to both the
agent and the MCP server — against this repo's own primary language.

- Add `tree-sitter` + `tree-sitter-python`, hash-pinned in
  `requirements.lock` and `== `-pinned in `pyproject.toml`.
- `Symbol` Pydantic model + a parsing module that runs a Python
  `tags.scm` query and builds the nested `Symbol` tree from `head/`.
- `RepoTools.outline(path, sha=None)` as an `@_tool` method (reuses the
  `read_file_at` git-show path for `sha`).
- Graceful degradation: non-Python / parse failure ⇒ empty result, no
  raise.

**Done when:** the review agent (and MCP `tools/call`) can request
`outline("foo.py")` and get back a correct nested symbol tree with
signatures. No UI, no base side yet.

## Slice 2 — Base/head set-diff + `changed_symbols` + `symbol_at`

Adds the second source side and the deterministic delta.

- Parse `base/` as well as `head/` for changed files.
- `qualified_name` set-diff → added / removed / modified.
- `RepoTools.symbol_at(path, line, sha=None)` and
  `RepoTools.changed_symbols()` `@_tool` methods.

**Done when:** `changed_symbols()` returns the correct add/remove/modify
set for a Python diff, and `symbol_at` resolves a line to its enclosing
symbol on either side.

## Slice 3 — Seed the overview prompt

Turns the delta from pull-only into pull + seed.

- Compute `changed_symbols()` before the overview pass; inject into the
  prompt for supported languages.
- Unsupported language ⇒ no seed, today's behaviour unchanged.

**Done when:** on a Python diff the overview pass's `symbols_*` match
the deterministic delta (no hallucinated/missing symbols), and an
all-unsupported-language diff is byte-identical to pre-slice output.

## Slice 4 — Sidebar Symbols axis (filtering)

First viewer consumer. Ship the axis working *flat* before nesting, so
the data path and filter integration land independently of the tree
render.

- Serve the per-file `Symbol` tree to the viewer (extend
  `build_json.py` or a lazy route, per the `fold-summary` precedent).
- Map each changed symbol → overlapping `hunk_ids`.
- Add a third `SidebarAxis` (`symbols`) rendered as flat pills; reuse
  `applyFilter`, localStorage, counts.

**Done when:** clicking a symbol pill filters the diff to that symbol's
hunks, persists across reload, and coexists with the Themes/Files axes.

## Slice 5 — Nested symbol tree render

The render-layer enhancement ADR 0001 commits to.

- `GroupBlock` gains optional `children[]`; the symbols axis grows a
  tree-walk render path (class ▸ method), expanded-by-default,
  collapsible.
- Ancestors shown for context even when unchanged; parent `hunk_ids` =
  subtree union; count = distinct subtree hunks.

**Done when:** a changed method renders under its (possibly unchanged)
class; clicking the class filters to all its changed methods, clicking
the method to just its hunks.

## Slice 6 — TypeScript / TSX / JavaScript grammars

Language expansion; rides every prior slice unchanged.

- Add the three grammar wheels (hash-pinned) + their `tags.scm`
  queries (vendor where absent).
- `signature` populated from TS annotations / interface / alias text;
  `None` for untyped JS.

**Done when:** outline, changed_symbols, seed, and the sidebar axis all
work on a TS/TSX/JS diff with no Python-specific assumptions.

---

## Not in these slices

Backlog items (i)–(iv) from ADR 0001 (symbol-aware folds; symbol-
boundary hunk segmentation; symbol-relative comment anchoring, which
sequences *after* segmentation; callgraph/signature injection). Each is
its own effort, weighed once the foundation is felt in use.
