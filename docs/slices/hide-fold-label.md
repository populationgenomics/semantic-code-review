# Slices — Hide by the diff, fold by the structure, label by meaning

Pairs with [ADR 0008](../adr/0008-hide-by-the-diff-fold-by-the-structure-label-by-meaning.md).
Slices 1–3 follow from what the ADR decides and touch none of its open
questions. Slices 4–6 wait on those questions; each names the one it needs.

## Dependency order

```
1 disclosure ─┐
              ├─▶ 3 AST fold regions ─▶ 4 annotation spans ─▶ 5 depth ladder ─▶ 6 reveal vs unfold
2 positioning ┘   (independent of 2)
```

1 and 2 are independent and may land in either order or together.

## Slice 1 — Disclosure is lazy and uncapped

Region expansion (before, between and after hunks; demoted hunks under a
filter) fetches its rows from `/file-text` — the route rendered mode already
uses, which serves both sides with no cap — instead of the `head_lines` bundle
in `/data.json`.

- `head_lines`, `_load_head_lines` and `_HEAD_LINES_CAP` go from
  `build_json.py` and `types.d.ts`.
- A file over 5,000 lines becomes expandable past the diff's context. A
  deleted file becomes expandable at all: its content is on the base side.
- `/data.json` loses the full text of every file it carried.
- The fetched text is cached per file and shared with rendered mode's cache;
  one round trip per file per session, whichever mode asks first.

**Gate:** the invariant is testable — every line of both sides of every file
in a fixture is reachable through a chip, including a deleted file and a file
over the old cap.

## Slice 2 — Positioning follows the diff, scored by the structure

A pure insertion run in `hunk_layout.py` is placed at the best position in
its content-neutral slide range.

- A run of `n` inserted lines can slide up `k` lines exactly when its last
  `k` equal the `k` lines above it, and down symmetrically. Every position in
  the range renders the identical file; this is a rendering choice.
- Score candidates: a definition opener beats a blank line beats anything
  else. Use `structural.parse` where a grammar exists, indentation where not.
- Line numbers on every row are unchanged; only the row order within the
  hunk moves. Comment anchors are `(file, side, line)` and must be unaffected.
- The corpus has 53 known instances (runs starting mid-body with the
  definition opener later in the block); they are the regression fixture.

**Gate:** the 53 render with the opener as the first inserted line; every
other hunk in the corpus renders byte-identically to before.

## Slice 3 — Fold regions from the AST, once, on the wire

`compute_fold_regions` takes its boundaries from tree-sitter (function, class,
block) with the indent detector as fallback for languages without a grammar,
computes them over the whole file (Slice 1 makes the whole file the unit),
and ships them. `folds.ts` consumes `fold_regions` and deletes
`_computeFoldRegions`, `_symbolRawRegions`, `_indentRawRegions` and
`_upsertFoldRegion`. `tests/fixtures/fold_regions_cases.json` becomes an
ordinary Python test.

Resolves candidate 5 of the architecture review.

**Gate:** the TS has no fold-region detector; the Python one is the only
implementation; regions exist for lines the diff never carried.

## Slice 4 — Annotation spans *(blocked: labelling mode)*

Segments and line notes become one `AnnotationSpan` — a post-image range with
intent, smells, refs and a stable id — that nests and may cross hunks. The
per-hunk output loses `new_start`/`new_count`. **Needs the ADR's open
question answered:** does the model label existing structural spans, select
from candidate ranges the structural layer proposes, or both?

## Slice 5 — The ladder becomes structural depth *(blocked: depth semantics)*

`segments` stops being a fold rung; the level between hunk and code is a
depth over Slice 3's regions. **Needs:** what "depth" means when AST depth is
uneven — absolute, or relative to the enclosing definition.

## Slice 6 — Reveal vs unfold

`focusReveal` is today's unnamed exception to "reveal puts content on screen
at the current depth". Decide whether a symbol-pill click is a reveal, an
unfold, or both, and name it.
