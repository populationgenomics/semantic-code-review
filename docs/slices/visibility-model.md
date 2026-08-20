# Slices — One visibility model

Three mechanisms currently decide whether a line renders, with three
different identities, three persistence tiers and no shared rule:

| | identity | survives re-render | survives reload |
|---|---|---|---|
| `> ` marker collapse | span of absolute file lines (slice 2; was *rendered row indices*) | since #9 | no |
| file / hunk header collapse | stable id (`H0_1`) | yes | yes, URL hash |
| unchanged context outside a hunk | none — pure DOM | no | no |

The first was keyed on something the user can move: revealing context
changed the rendered row array, `_computeFoldRegions` derived different
spans, and the record holding the collapse state was orphaned (#10).
Slice 2 re-derived the span from file content, so the record stays put.
The third still has no record at all, so any `render()` — a top-bar
click, an SSE hunk patch, a filter change — silently reverts it.

The target: **one primitive, one rule.** A hidden span is named, has an
owner, and visibility is the complement of the union. Presentation stays
polymorphic — a `CodeFold` shows a summary row, a hunk shows its `@@`
header, a context gap shows a chip — but nothing else differs.

## Vocabulary

Fixed first, because today's code calls all of it "fold":

- **`CodeFold`** — a `> def foo(): …` collapse of a structural region.
- **`HiddenSpan`** — the union primitive: file/hunk collapse *and*
  default-hidden context outside a hunk. Reveal is the removal of one.

Neither `FoldRegion`, `_folded`, `_state.fold` nor the `fold=` hash key
survives; each names a `HiddenSpan` today and would read as its opposite.

## Decided

- **Scope is one user on localhost.** URLs are not shareable, so spans
  can be absolute file lines rather than portable structural ids.
- **Fold/hide state is per-tab** (`sessionStorage`); comments stay
  shared in the run dir with the server as serialiser. Two tabs
  disagreeing about folds is acceptable and rare.
- **Collapsed content becomes a manifest**, not an absence: a two-column
  list (old / new) of line number plus first line, coloured by kind,
  floated to the top of the hide. Generated summaries and resolved
  comments drop out; unresolved user comments stay.
- **Not navigable** until testing shows people expect to click through.
- **Bounding the manifest is deferred** — a file rarely carries an
  unmanageable number of notes. Revisit if one does.

---

## Slice 1 — Rename *(done, 6879d59)*

Narrower than first scoped. Of ~135 "fold" identifiers, 113 already mean
a `>`-marker region — `FoldRegion`, `fold_regions`, `FoldDescription`,
`/fold-summary`, `submit_fold_summary` — and are correct under the new
vocabulary. Renaming those would churn the sidecar format, the SSE event
names and an LLM-facing tool name for nothing.

What was misnamed is the global collapse *level*: `FoldMode` ->
`CollapseLevel`, `_state.fold` -> `_state.collapseLevel`, `_isFolded` ->
`_isHidden`, `_toggleFold` -> `_toggleHidden`, `_setGlobalFold` ->
`_setCollapseLevel`, `_defaultHunkFolded` -> `_defaultHunkHidden`.

The `fold=` hash key is left alone: Slice 4 deletes it.

`HiddenSpan` does not appear yet — it is introduced by Slice 3, which is
where the primitive actually exists. Renaming to a type that has no
definition would have been theatre.

## Slice 2 — Spans become absolute *(done, 39bdbb3, e103074)*

Two halves, and the plan mis-weighted them.

**Symbol-snapped regions** were the easy half as scoped, but they turned
out to need a *Python* change, not just a viewer one. Both detectors read
a snapped region's ranges off the rows rather than off the
`FoldSymbolSpan` they had already snapped to, so a definition addressed
itself differently depending on how much of it the rows covered. Fixing
only the viewer would have desynced it from the server's wire
`fold_regions` and from the persisted `FoldDescription`s keyed on the
same tuple, silently dropping every re-seeded summary. `compute_fold_regions`
and `_computeFoldRegions` now both take the declared span. The
cross-language fixture needed no edits — it only ever exercised
whole-definition hunks, where the two agreed by accident.

**Indentation regions** were the hard half, as expected. Detection runs
over a whole-file row stream synthesised from `head_lines` plus the
hunks' own rows, so the algorithm is unchanged and only its input is
reveal-independent. Rendered rows are used for placement alone.

Presentation moved off the record entirely: `header_idx` /
`body_start_idx` / `body_end_idx` are no longer read or written by the
viewer, which re-places each region against the current render.
They stay on the wire (the server still emits them) — slice 3 retires
them.

Two things the plan got wrong:

- **"Nothing visible changes" is not quite true.** A block the hunk
  truncates now folds to its real end rather than the last visible row,
  and a definition present on only one side is addressed on that side
  alone instead of picking up a row-derived range on the other. Both
  are the intended correction, not regressions, but they are visible.
  `/fold-summary` also now sends the whole definition's line range, so
  its prompts carry more content than before.
- **#10's repro cannot be demonstrated end-to-end on this branch.**
  Collapse state is still DOM-only here; it is PR #9 that puts `_folded`
  on the record, and any `render()` — including collapsing a file —
  discards a DOM-only collapse regardless of span identity. What slice 2
  fixes is the mechanism underneath: the record is re-matched rather
  than orphaned across a reveal, which is what makes #9's `_folded` (and
  today's `summary`) survive. The tests assert exactly that.

Files with no `head_lines` — generated, binary, deleted, or over
`_HEAD_LINES_CAP` (5,000 lines) — still detect over the rendered rows,
#10 included. That is an explicit, commented branch in `folds.ts`, not a
silent fallback. Slice 6 removes it along with the cap and the
head-side-only limit.

Detection is O(rows x definition spans) over the whole file, so it is
memoised per `FileBlock`, invalidated when an SSE `hunk` event swaps a
`HunkBlock`.

## Slice 3 — One visibility function

All three sources become `HiddenSpan`s; visibility is the complement of
their union, computed from state with no DOM read. Retires the
chip↔expansion DOM swap and the per-row `display` writes.

**Done when:** every hide/reveal/fold interaction is unit-testable
without a rendered document, and `render()` no longer loses reveal state.

## Slice 4 — Per-tab persistence

`sessionStorage`, keyed by run; `fold=` and the per-hunk overrides leave
the URL hash so there is one source of truth.

**Done when:** reload restores collapse state, two tabs are independent,
and no view state rides in the URL.

## Slice 5 — The manifest

Two-column list at the head of a hide. Colour sidebar by kind. Resolved
comments and generated summaries excluded.

**Done when:** collapsing a file shows its unresolved comments as
one-line entries with line numbers, and no collapsed state anywhere
produces a gutter discontinuity.

## Slice 6 — `/file-text` as the content source

Replace eager `head_lines` in `data.json` with the lazy route rendered
markdown already uses. Removes the 5,000-line cap and the head-side-only
limit, and shrinks the payload by the full text of every file.

Detection becomes asynchronous. That is safe because a persisted
`HiddenSpan` is self-describing — restoring it needs no detection, only
*offering new folds* does — so state returns at first paint and fold
affordances arrive with the content.

**Done when:** `CodeFold`s and gap expansion work on files over the cap
and on the base side; `head_lines` is gone from the viewer payload.

## Deferred

- **Single-file view for large files**, bounding DOM size. `/file-text`
  is the enabling piece; Slice 6 heads that way without committing to it.
- **Manifest navigation** — clicking an entry reveals to that line.
- **Skipped files** (`PARKED_IDEAS.md` item 25) — a lockfile should
  collapse to one row rather than render hunks reading "(no intent — may
  need re-run)". Adjacent, and the right home for "I never want to review
  this", which view state should not be asked to carry.

## Open

- **PR #9** — *residual; expected to close unmerged once Slice 3 lands.*
  It stores collapse state on the region record keyed by span. Slice 2
  fixed the span rather than replacing the record, so its `_folded`
  would now land on a key that holds — but Slice 3 replaces the record
  with a `HiddenSpan`, so rewriting its row-derived tests to land it
  first buys one slice of life.
- **This plan has no ADR yet.** The decision record for the visibility
  model is owed; per `docs/adr/README.md` the ADR holds the *why* and
  this file holds the order.
