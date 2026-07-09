# Tree-sitter: design notes

> **Status:** shipped. tree-sitter is a pinned runtime dependency
> (`pyproject.toml`, `requirements.lock`); the deterministic structural
> layer lives in `semantic_code_review/structural/` (`parse.py`,
> `symbols.py`, `diff.py`). This doc is the design history — why the dep
> is carried and what remains unbuilt. The live vocabulary (Symbol,
> SymbolDelta, Symbols axis, overview seed) is defined in
> [`CONTEXT.md`](CONTEXT.md).

## Why the dep is carried

tree-sitter was skipped while the viewer was a static, self-contained
HTML artifact: every structural pre-computation had to be paid up front
for files the reviewer might never open, and it added a native-binding
Python dep against the supply-chain pinning regime we'd just tightened.

The calculus changed once `scr review` became a long-running localhost
HTTP server with a back-channel:

- **Lazy parsing.** A file's AST is built only when the reviewer expands
  it — up-front parsing of every touched file is no longer the only option.
- **Cached state across calls.** The server holds parsed ASTs in memory
  across every per-hunk call, fold-toggle, and ref-click. Parse once,
  query many.
- **Interactive features that need ground truth.** Click-to-jump, symbol
  cross-referencing, and "where else is this used" become affordable when
  the parse is already resident.

## Shipped

- **Symbol grouping (the Symbols axis).** The sidebar carries a
  deterministic structural axis alongside LLM-curated themes: top-level
  symbols enumerated from each touched file's AST, each linking to the
  hunks that touch it (`viewer/build_json.py` `_symbol_blocks`,
  `structural/symbols.py`). Themes answer "what is this PR for?"; symbols
  answer "show me everything about X".
- **AST fold regions.** Fold ranges come from real function / class /
  block boundaries, not indentation (`viewer/build_json.py` `_fold_spans`).
  The indent heuristic remains only as the fallback for languages without
  a shipped grammar.
- **Deterministic overview seed.** The overview pass is seeded with the
  base→head `SymbolDelta` (`augment/overview.py` `_format_symbol_seed`),
  so `symbols_added` / `symbols_modified` come from the parse rather than
  the LLM guessing.

Grammars ship as three individual packages — `tree-sitter-python`,
`tree-sitter-javascript`, `tree-sitter-typescript`. Other languages
degrade to the indent-based fold fallback: the floor is unchanged, the
ceiling rises only for what we ship.

## Not yet built

**Semantic hunk splitting.** Hunks are still whatever `git diff`
coalesced; the per-hunk pass does some semantic splitting after the fact
via the optional `segments[]` field. Moving boundary detection upstream —
re-segmenting the diff so each segment maps to one changed AST node —
would make it deterministic and retire the prompt's "split into segments"
guidance. It reshapes the per-hunk pipeline, so it is the most ambitious
remaining step.

Two stretch goals fall out once AST diffs drive segmentation:

- **Cross-file move detection.** Same body-hash AST node deleted from
  file A and added to file B → one "moved" operation instead of paired
  delete+add. The single biggest mental-model improvement for
  refactor-heavy PRs, and impossible without structural parsing.
- **Sub-symbol rename detection.** Distinguish "function `foo` renamed to
  `bar`, body unchanged" from "`foo` had one line changed".

## Out of scope

- Replacing the LLM as the source of `intent` / `context` / `smells`.
  The structural parse augments the LLM; it doesn't supplant it.
- **Sub-line folding.** Per-line is the floor for diff review (cursor,
  selection, search, and `git diff` all expect the line as the unit). The
  lever is smarter line-level boundaries, which AST folds already deliver;
  collapse-to-glyph and parameter-list elision don't generalise and break
  too many tooling expectations.
- Live ref-link verification ("does the symbol named in `reason` actually
  live at the cited line?"). Plausible follow-on now that symbol indexing
  exists, but unbuilt.
- IDE-style real-time editing. The viewer is a review surface, not an editor.
