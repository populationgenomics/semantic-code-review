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
silent fallback. Slice 6 removed it along with the cap and the
head-side-only limit: content comes from `/file-text`, and a file with
none offers no new fold at all.

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

## Slice 5 — The manifest *(done, 3716bbe)*

Every hide is headed by the notes it covers: a two-column list (old /
new) of line number plus the note's first line, colour-sidebar'd by kind,
in `viewer/assets/manifest.ts`. Five places render one — a collapsed
file, a collapsed hunk, a hunk folded to its `seg-list`, a collapsed
context chip, and a collapsed `CodeFold` (as an annotation under the
header row the chevron sits on). Entries are inert and the list is
unbounded, both as decided.

In: unresolved reviewer comments, one entry per thread at its root,
local and ingested alike — a comment promoted from an LLM annotation is
still a comment. Out: resolved threads (read from the root, so a local
reply cannot re-open one), the annotation a comment replaced, and
generated fold summaries.

**LLM line notes are in**, under their own colour. They were the part
the plan left optional, and they cost nothing: they already hang off a
hunk by absolute line, which is the manifest's address. Hunk- and
segment-level *smells* are not in — they are not line-anchored, and the
chrome that stands in for a hidden hunk already carries them.

Four things the plan and the ADR did not settle:

- **A `segment` span cannot answer what the seg-list hides.** Slice 3
  made it a binary switch on the hunk body rather than a hide of its own
  lines, and it carries no base side at all, so while every segment is
  collapsed the hunk's context rows and its whole base side are covered
  by no span while nothing renders them. The seg-list therefore gets one
  manifest for the hunk's whole extent, by line range rather than by
  span — `Manifest.inRange` alongside `Manifest.under`. Giving segment
  spans real ranges would not have fixed it: the rows between segments
  belong to none of them.
- **A collapsed `CodeFold` keeps one line on screen**, so a note on that
  line is listed *and* rendered. The fold's manifest drops the header
  line for that reason.
- **The manifest needs a repaint when the comment store lands.** It is
  built during `render()`, and `/comments` resolves after the first
  paint — a hide already in place at boot (the default level, or restored
  view state) would head an empty list forever. `Comments`' `onChange`
  now re-renders as well as refreshing the sidebar counts.
- **The viewer test harness's footer had diverged from `index.html`**,
  omitting `#status-counts`. `_updateStatus` fell back to writing
  `textContent` on the footer itself, which wipes the console bar
  mounted there. Harmless until a render happened after `Console.init`,
  which the comment-store repaint made routine.

**The gutter claim was already true, for a reason the plan did not
give.** Every collapse kind stands something in the skipped lines' place
— a file or hunk header, a segment summary row, a gap chip, and for a
`CodeFold` the header line the chevron hangs off. Between two hunks it is
the next hunk's own `@@` header that accounts for the step, which is why
a file with no `head_lines` (nothing to build a chip's rows from) does
not read as a jump either. `tests/js/viewer.test.ts` pins all of it with
a walker that reads one side's gutter in DOM order and reports a step
nothing accounts for; the walker is itself tested against a
hand-built discontinuity, or it would pass vacuously.

**One case is still false, and it is Slice 6's.** A file with no
`head_lines` under an active sidebar filter: expanding the region that
swallowed the demoted hunks splices their rows together with the
unchanged context between them missing and no row to say so. There is no
`HiddenSpan` there either, so the notes on those lines have nowhere to
appear. Both go away when `/file-text` supplies the context. In the
unfiltered case — the common one, and the one that carries ingested
comments on unchanged lines of an over-cap file — an inert
`.gap-absent` band now names the missing lines and hosts their notes.

**Testing.** `tests/js/manifest.test.ts` settles what belongs in a
manifest without a document: the resolved/reply/promotion rules, the
side-tagging, that a hide not in the store lists nothing, and that the
list is unbounded. The renderer's claims sit in `viewer.test.ts` — seven
manifest tests and nine gutter tests. All seven manifest tests fail
against 414f835; of the gutter tests only the two about a file with no
shipped content do, because the rest were already true.

## Slice 6 — `/file-text` as the content source *(done, 8b5d4ed, e7c13a6, 024aa87, 9e74bf4)*

`file_text.ts` owns the lazy per-file fetch and cache; both renderers
read it. `head_lines` is gone from the payload, and with it the
5,000-line cap, the head-side-only limit and the row-derived detection
fallback. Measured on this branch's own 19-file diff (pre-augment, so
the ratio is a ceiling): `data.json` 1,104,642 → 547,143 bytes.

Five things the plan and the ADR got wrong or left open.

- **The cap moved; it did not go.** `/file-text` has its own —
  `_FILE_TEXT_CAP_BYTES`, 2 MB *per side* — and a side over it comes
  back null. Null is load-bearing and is not an empty file: an empty
  file detects every fold out of existence and makes every gap zero
  lines long. `hasContent` is false for such a file and the renderer
  stands the `.gap-absent` band in its lines' place, the same as it does
  for a binary file and for the paint before the answer arrives.

- **What the head-side-only limit actually cost is not left-addressed
  folds.** A definition deleted in head has `del` rows *inside* a hunk,
  which already mapped into the base spans, so those worked. What it
  cost is a file whose **head side the route cannot serve** — over the
  cap, or absent: it had no content stream at all. The base side answers
  the same question, because unchanged context is by definition the same
  text on both sides, so base now substitutes for head and such a file
  keeps its folds and its expandable gaps.

- **Slice 5's `.gap-absent` band does not go away.** `/file-text` does
  not delete the condition, it narrows it: from "generated, binary,
  deleted, or over 5,000 lines" to "the route served neither side, or
  has not answered yet". Deleting the band would put the notes on those
  lines nowhere again.

- **Slice 5's filtered splice needed more than content.** With content
  the region fills; without it, the run now *breaks* at the unfillable
  gap — each side of it is its own region and a band names the lines
  between — rather than splicing the demoted hunks together. The gutter
  is honest in both cases, and the notes on the missing lines have a
  home in both.

- **The ordering claim held, with one visible cost.** `_restoreViewState`
  runs before first paint and needs no rows; `planRows` hides a restored
  `codefold` span with nothing detected, and nothing re-seeds a
  `codefold` (only the level and the gaps seed, and the marks ledger
  stops a re-seed undoing a reveal). What the plan does not mention is
  that the *first paint* has no gap chips either — a file's unchanged
  runs render as bands for one frame, then repaint into chips.

**The JS/Python divergence converged by deleting the second detector,
not by giving Python the same content.** Slice 2 left Python detecting
per-hunk over rows and called converging "slice 6's job"; the ADR
frames that as one content source. One *detector* is the better answer:
the viewer is the side that knows which content it has, and slice 2
already caught the two implementations silently disagreeing. So the
server no longer detects. `FileBlock.fold_regions` is the file's
persisted `FoldDescription`s as addressed records — the summaries the
run has — and the viewer matches its own detected regions to them by
address. A summary now re-seeds for an indentation region in an
unsupported language and for a definition no hunk touches, both of which
per-hunk detection silently dropped. `/fold-summary` resolves the
definition for its prompt from `fold_symbols`, the spans the client's
address came from. `hunk_layout.compute_fold_regions` survives as the
reference implementation the viewer's detector is pinned against on the
shared fixture, and says so.

Two things fell out on the way:

- Fold records used to live on `hunks[0]`, so an SSE `hunk` event
  replacing hunk 0 destroyed every record — including a summary fetched
  this session. They are the file's now.
- A zero-count hunk side is written by git as the line *before* the hunk
  (`@@ -10,3 +9,0 @@`), so `start + count` resumed the head side one line
  early and paired every later context row against the wrong base line.
  Pre-existing — the same arithmetic ran against `head_lines` — and now
  in one place, `FileText.hunkBounds` (9e74bf4).

**Testing.** `tests/js/file_text.test.ts` has no home: the module is a
cache around one route, and what is worth pinning about it is what the
renderer does with it, so the claims sit in `viewer.test.ts` and
`folds.test.ts`. Each was checked by mutation rather than against the
pre-slice bundle (the harness changed shape): dropping the base-side
fallback fails the two base-side tests; splicing instead of breaking the
run fails the three band tests; reading the hunk header literally fails
the pure-deletion pairing test. The `/file-text` stub answers from a
per-test registry rather than the response queue, and can be held open,
which is how a test looks at the viewer before its content exists.

## What the model does not cover

With all six slices in, "one primitive, one rule" holds for every hide
the reviewer can move. Three things sit outside it, all deliberately:

- **The sidebar filter** (slice 3). Demoting a non-live hunk into a
  region is not a `HiddenSpan`: its rows render inside an expanded
  region regardless of level, because the region — not the absent hunk
  header — is what stands in for them. Folding it into the union would
  leave the reviewer expanding a chip onto context with no affordance
  for the changes.
- **The seg-list** (slice 5). A `segment` span is a binary switch on the
  hunk body rather than a hide of its own lines, and carries no base
  side, so while every segment is collapsed the hunk's context rows and
  its whole base side are covered by no span. Its manifest is built from
  a line range instead (`Manifest.inRange`).
- **The sidebar's active pill** (slice 4), which persists to
  `localStorage` keyed on `head_sha` — already-broken cross-run
  persistence by ADR 0006's own port-0 reasoning, left alone because the
  pill is a filter.

And one bound remains: `/file-text`'s 2 MB per side. A file over it on
both sides has no content in the viewer — no `CodeFold`s, no expandable
gaps, bands where its unchanged runs would be. That is the honest
failure: the alternative is a detector reading the rendered rows, which
is #10 with better manners.

## Deferred

- **Single-file view for large files**, bounding DOM size. `/file-text`
  is the enabling piece and is in place; the view is not built.
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
