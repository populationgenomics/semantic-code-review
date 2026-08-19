# Slices — One visibility model

Three mechanisms currently decide whether a line renders, with three
different identities, three persistence tiers and no shared rule:

| | identity | survives re-render | survives reload |
|---|---|---|---|
| `> ` marker collapse | span of *rendered row indices* | since #9 | no |
| file / hunk header collapse | stable id (`H0_1`) | yes | yes, URL hash |
| unchanged context outside a hunk | none — pure DOM | no | no |

The first is keyed on something the user can move: revealing context
changes the rendered row array, `_computeFoldRegions` derives different
spans, and the record holding the collapse state is orphaned (#10). The
third has no record at all, so any `render()` — a top-bar click, an SSE
hunk patch, a filter change — silently reverts it.

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

## Slice 2 — Spans become absolute

`CodeFold` detection expressed in side-tagged absolute file lines and
derived from file content, not from the rendered row array. Nothing
visible changes; the record simply stops moving when context is
revealed.

Inherits `_HEAD_LINES_CAP` (5,000 lines) and head-side-only content
until Slice 6 — so it fixes #10 for most files and not for the largest,
which must be stated in the code rather than discovered.

**Done when:** detecting twice under different reveal states yields
identical spans; #10's repro (reveal, fold, fold file, unfold file)
preserves the collapse.

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

- **PR #9** stores collapse state on the region record keyed by span —
  the model Slice 2 replaces. It is a strict improvement on today and
  green, so it can land as a stepping stone, but its tests encode the
  superseded semantics. Decide before Slice 3, not during.
- **This plan has no ADR yet.** The decision record for the visibility
  model is owed; per `docs/adr/README.md` the ADR holds the *why* and
  this file holds the order.
