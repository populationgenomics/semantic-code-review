# Slices — One visibility model

The *why* is [ADR 0006](../adr/0006-one-visibility-model.md);
this file holds the order.

Three mechanisms decided whether a line renders, with three different
identities, three persistence tiers and no shared rule:

| | identity | survives re-render | survives reload |
|---|---|---|---|
| `> ` marker collapse | span of absolute file lines (slice 2; was *rendered row indices*) | since slice 3 | since slice 4 |
| file / hunk header collapse | stable id (`H0_1`) | yes | since slice 4 (was: URL hash) |
| unchanged context outside a hunk | span of absolute file lines (slice 3; was *none — pure DOM*) | since slice 3 | since slice 4 |

The first was keyed on something the user can move: revealing context
changed the rendered row array, `_computeFoldRegions` derived different
spans, and the record holding the collapse state was orphaned (#10).
Slice 2 re-derived the span from file content, so the record stays put.
The third had no record at all, so any `render()` — a top-bar click, an
SSE hunk patch, a filter change — silently reverted it.

The target, reached in slice 3: **one primitive, one rule.** A hidden
span is named, has an owner, and visibility is the complement of the
union. Presentation stays polymorphic — a `CodeFold` shows a summary row,
a hunk shows its `@@` header, a context gap shows a chip — but nothing
else differs.

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

## Slice 3 — One visibility function *(done, 260fc26, c4c78ad)*

All three sources are `HiddenSpan`s in `viewer/assets/visibility.ts`;
visibility is the complement of their union, from state, with no DOM
read. The chip↔expansion swap and the per-row `display` writes are gone
— a region renders as a chip or as its rows depending on whether its
span is present, and the renderer simply does not emit the rows a
collapsed `CodeFold` covers.

**The bulk-action rule, which the ADR left to this slice.** A span
carries an *owner*, and a bulk action retracts only what it asserted.
Picking a level drops every `level`-owned span and re-seeds at the new
depth; `user` spans (a click) and `gap` spans (a region's context) are
untouched. So `uv.lock` folded away by hand survives "off", while a hunk
the reviewer expanded is folded back by the next level pick — the level
stays authoritative over its own hides without being a reset. Reset is
the one control that drops everything.

The mechanism the ADR floated — *bulk actions scoped to visible items
only* — was not needed. Scoping by owner is the same guarantee with no
dependence on what happens to be on screen, and it is what makes the
level idempotent: pressing "2" twice must re-fold, which a
visible-items-only rule cannot express.

Two ledgers per file make that work: `spans` (hidden now) and `marks`
(every id ever asserted, and by whom). Seeding a marked id is a no-op,
which is how a reveal survives the renderer re-seeding the same gap on
every render *and* survives a bulk action. It also expresses "the
reviewer opened this" without a negative record — the absence the ADR
says a reveal must be.

Four things the plan and the ADR did not settle:

- **The filter is a fourth hiding mechanism, and it stays outside the
  model.** Demoting a non-live hunk into a region is not a `HiddenSpan`:
  its rows render inside an expanded region regardless of level, because
  the region — not the absent hunk header — is what stands in for them.
  Folding it into the union would have left the reviewer expanding a
  chip and seeing only context, with no affordance for the changes.
- **A `CodeFold`'s presentation eats one of its own lines.** The span is
  the definition's whole extent; the renderer keeps the run's first
  *rendered* line as the header the chevron and summary hang off. That
  reproduces today's behaviour where a hunk starts mid-definition, and
  it is the only place a hidden line is still drawn.
- **The `fold=` hash lost its per-item entries.** A span set does not fit
  `id=f|o` pairs, and an expansion is now the absence of a record.
  Per-item collapse therefore stops surviving a reload until slice 4
  restores it properly; the level still round-trips.
- **The wire's detection row indices are gone** (`header_idx` /
  `body_start_idx` / `body_end_idx`), as slice 2 said they would be.
  They stay inside `_FoldRegion` and the cross-language fixture.

Detection now reads `FileRows.sourceRows` — the rows a container covers,
folded ones included. Without it a collapsed fold on a file with no
`head_lines` detected itself out of existence and could not be reopened.

**Testing.** `tests/js/visibility.test.ts` drives the span algebra with
no rendered document, which is the slice's done-condition: the union and
its complement, reveal surviving a re-seed, nesting, every bulk-action
case, and `planRows`. Three things still need a document, because they
are claims about the renderer rather than the state — a revealed gap
surviving a repaint, a `CodeFold` surviving its file's collapse/reopen
cycle, and the `uv.lock` case end to end; those sit in `viewer.test.ts`
and each fails against the pre-slice viewer.

PR #9 is now fully superseded: its `_folded` lived on the region record,
which no longer holds collapse state.

## Slice 4 — Per-tab persistence *(done, 182c747, 3abb19a)*

One `sessionStorage` record per run — `scr-view-state:<run_id>` — holding
the collapse level and both span ledgers of every file. `#fold=` is gone
from the URL along with the `hashchange` listener, so there is one source
of truth. `view_state.ts` owns the record; `visibility.ts` gained
`snapshot` / `restore` / `revision` and stayed storage-free.

**There was no run id to key on.** `pr.head_sha` was the obvious
candidate (`sidebar.ts` uses it) and is wrong: it does not move when the
*base* does, so `scr review HEAD~1` and `scr review HEAD~3` on a clean
tree share it while showing different diffs. The plan's worry — a dirty
tree holding the head SHA fixed — is not real here, because
`_synthesise_head_sha` folds the working-tree diff into the SHA. The run
directory's name is used instead: it is the run's identity everywhere
else (comments, sidecar, cache), and the server stamps it into
`/data.json` at serve time next to the `debug` flag, both being
properties of the run rather than of the diff.

**What the run key is for.** `sessionStorage` is already per-tab, so the
key does not guard tab collision. It guards the one way a record outlives
its run: the kernel hands a later run the same ephemeral port, the
reviewer points the same tab at it, the origin matches, and spans
addressing absolute file lines would hide arbitrary lines of a different
diff.

**Where the write happens.** At the end of `render()`, which every span
mutation already funnels through, gated on a revision counter so a render
that moved nothing does not re-serialise a span set the size of the diff
(one per SSE frame otherwise). Restoring skips the boot-time
`setLevel`: a restored store already holds the level's *marks*, and
re-seeding over them would re-hide every hunk the reviewer had expanded.

**Fail soft on write, loud on corruption.** A denied or full store
degrades to in-memory with a warning — nothing is wrong with the state
itself. A record that parses but does not describe view state raises
`ViewStateError`, uncaught. A record at another schema `version` is
neither: it is a known stale artefact of an older scr in the same tab,
discarded with a warning. Without that third case the first version bump
would hard-fail every reused tab.

**Testing.** `tests/js/view_state.test.ts` drives the record without a
document: the round trip of both ledgers, per-tab isolation (a second
`sessionStorage` instance, which is what a tab is), a denied store, a
refused write, and each malformed shape. The three done-conditions need
the renderer, so they sit in `viewer.test.ts` — a reload restoring a
hide, a reveal and the level; two tabs staying independent; an empty
`location.hash` after a level pick and a fold. All six fail against
9774606.

**Left alone: `sidebar.ts`'s `localStorage`.** The active pill persists to
`localStorage` keyed on `head_sha`, and by ADR 0006's own reasoning that
cannot survive a run — 352ba4c introduced it as "persists to localStorage
keyed by head sha", i.e. as cross-run persistence, before anyone had
reasoned about the port-0 origin. So it is already-broken cross-run
persistence rather than a written-down exception. Not changed here: the
pill is a *filter*, which slice 3 deliberately kept outside the span
model, and switching it to `sessionStorage` would trade a real if small
behaviour (reopen the URL in a fresh tab within one run and the filter
comes back) for consistency. The author's call, not this slice's.

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

- **PR #9** — *residual; close unmerged.* It stores collapse state on the
  region record keyed by span. Slice 3 landed the collapse as a
  `HiddenSpan` instead, so the record it hangs `_folded` off no longer
  holds any collapse state; there is nothing left of it to salvage.
