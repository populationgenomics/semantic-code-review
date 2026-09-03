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
- A file over 5,000 lines, and a `generated` or `binary` file, becomes
  expandable past the diff's context. `/file-text`'s own 2 MB per-side cap
  goes with the bundle's. (A deleted file needs no disclosure — its diff is
  every base line as a deletion.)
- `/data.json` loses the full text of every file it carried.
- The fetched text is cached per file and shared with rendered mode's cache;
  one round trip per file per session, whichever mode asks first.

**Gate:** the invariant is testable — every line of both sides of every file
in a fixture is reachable, through a chip or through the hunk itself,
including a deleted file, a generated file, and a file over the old cap.

## Slice 2 — Positioning follows the diff, scored by the structure

A pure insertion run in `hunk_layout.py` is placed at the best position in
its content-neutral slide range.

- A run of `n` inserted lines can slide up `k` lines exactly when its last
  `k` equal the `k` lines above it, and down symmetrically. Every position in
  the range renders the identical file; this is a rendering choice.
- Score both seams — above the run's first line and below its last — and
  sum: a definition opener beats a blank line beats anything else. Scoring
  the first line alone flips git's `entry, blank` into `blank, entry` across
  every lockfile and import block, which is the same seam with the blank on
  the other side. An opener's edge includes the decorator and comment lines
  above it; tree-sitter's definition node does not. Use `structural.parse`
  where a grammar exists, indentation where not.
- Line numbers on every row are unchanged; only the row order within the
  hunk moves. Comment anchors are `(file, side, line)` and must be unaffected.
- The corpus fixture holds hunks that must move and hunks that must not.

**Gate:** every slidable run lands on its best seam; every hunk with no
slidable run renders byte-identically to before; no hunk loses an
opener-first rendering.

## Slice 3 — Fold regions from the AST, once, on the wire ✅ done

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

Landed as: `viewer/fold_regions.py` (the one implementation), regions
on `FileBlock.fold_regions` in `/data.json` (`HunkBlock.fold_regions`
and `FileBlock.fold_symbols` gone), `FoldDescription` lifted to
`FileAnnotations`, `folds.ts` chrome only with its chrome registry
keyed by row container rather than file id.

## Slice 4 — Annotation spans

Segments and line notes become one `AnnotationSpan` — a post-image range with
intent, smells, refs and a stable id — that nests. The per-hunk output loses
`new_start`/`new_count`: the prompt carries a numbered list of *boundary
lines* (hunk edges and every AST node edge inside it; indent changes and
blanks without a grammar), and a span is a pair of boundary ids plus its
label. Spans are bounded by the hunk their pass saw until the batch pass is
on.

**Gate:** no integer coordinate in the model's output schema; every drop
bucket from #21 is unrepresentable; nested spans render.

## Slice 5 — The ladder becomes `definitions`

`segments` stops being a fold rung. The ladder is `files | hunks |
definitions | off`: the middle rung folds every definition-level node a hunk
touches to its opener and label, and a hunk touching no definition folds as
one region. `SegmentBlock`, the `seg-list` renderer and the synthetic
whole-hunk segment go.

**Gate:** key `3` and the URL hash select `definitions`; nothing is
synthesised; every hunk renders at that level.

## Slice 6 — Focus is the one reveal that unfolds

A reveal puts content on screen at the current depth. A *focus* — the
symbol-pill click — reveals and unfolds its span to `off`, ephemerally.
`focusReveal` becomes a property of the gesture, not a flag on `RenderState`,
and no other reveal path inherits it.

**Gate:** expanding a chip does not change fold depth; clicking a pill shows
code; touching the slider clears the focus.
