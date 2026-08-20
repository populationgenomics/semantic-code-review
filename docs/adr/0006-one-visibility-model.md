# ADR 0006 — One visibility model

- Status: Accepted (slices 1–2 implemented; 3–6 pending)
- Date: 2026-08-20

## Context

Three mechanisms decide whether a line renders in the viewer, and they
were built at different times with nothing in common:

| | identity | survives re-render | survives reload |
|---|---|---|---|
| `> ` marker collapse | span of *rendered row indices* | no | no |
| file / hunk header collapse | stable id (`H0_1`) | yes | yes, URL hash |
| unchanged context outside a hunk | none — pure DOM | no | no |

Three identities, three persistence tiers, three sets of rules. A line's
visibility can be governed by all three at once, and the first nests.

Issue #10 is the symptom that forced this. The `> ` marker collapse was
keyed on something the user can move: revealing context inserts rendered
rows, detection derives different spans from the new row array, and the
record holding the collapse state is orphaned. The third mechanism has
no record at all, so any `render()` — a top-bar click, an SSE hunk
patch, a filter change — silently reverts a reveal.

Both are instances of the same defect. State set by a user action is
being stored in a place that another user action redefines.

The governing principle, stated by the author while working the bug:
**state a user set should not be lost by toggling any of the three
mechanisms, by reloading the page, or by using the top bar.** Every
decision below follows from that plus the observation that the three
mechanisms differ only in how they present a hidden range, not in what
hiding means.

## Decision

**One primitive.** A `HiddenSpan` is named, has an owner, and covers
both header collapse and default-hidden context. A line's visibility is
the complement of the union of hidden spans. Reveal is the removal of a
span, which makes it reversible — it is a record, not an absence.

**Presentation stays polymorphic.** A `CodeFold` shows a summary row, a
hunk shows its `@@` header, a context gap shows a chip. Nothing else
about them differs.

**Two vocabulary terms, fixed first**, because the code called all of it
"fold" and any discussion collapsed into ambiguity within two sentences:

- **`CodeFold`** — a `> def foo(): …` collapse of a structural region.
- **`HiddenSpan`** — the union primitive above.

Neither `FoldRegion`, `_folded`, `_state.fold` nor the `fold=` hash key
survives; each names a `HiddenSpan` today and reads as its opposite.

### Identity is absolute file lines

A `CodeFold` is addressed by side-tagged absolute 1-indexed file lines,
derived from file content — the definition's own `fold_symbols` span, or
indentation over the whole file — never from the rows on screen. Row
indices remain, demoted to per-render presentation, recomputed each time
the viewer places a region onto the rows it just rendered.

This is what fixes #10: the address no longer moves when a reveal
changes the row array, so the persisted record is re-matched rather than
orphaned.

Absolute lines are viable only because **the scope is one user on
localhost**. Shareable URLs would have forced portable structural ids;
they are not a goal, so the simpler address wins.

### Nested state survives its container

Collapsing something that contains a collapse, then expanding the
container, restores the inner collapse. State is nested, not flattened —
a container's collapse does not destroy what is inside it.

**Bulk actions apply the same logic.** The top bar is a bulk action, not
a reset. It may fold or unfold something inside an existing collapsed
state, and that outer state survives.

The driving case: a reviewer folds `uv.lock` away because they do not
intend to review it, then clicks "off" to expand everything else. The
manual fold-away must not blow back open into a huge diff. Today
`_setCollapseLevel` clears every override, which fails this outright.

### View state is per-tab; comments stay server-serialised

Fold and hide state persists in `sessionStorage`, keyed by run. Comments
continue to round-trip through the review server into the run directory.

Browser storage cannot do better. `localStorage`, IndexedDB and OPFS are
all origin-scoped, and the review server binds `--port 0` — a
kernel-assigned port — so the origin changes on every run. Nothing
stored under one run's origin is reachable from the next. Within a
single run the port is fixed, so `sessionStorage` covers exactly what is
achievable: a tab's lifetime, including reload.

Two tabs disagreeing about folds is accepted. It is rare, and the
alternative costs a synchronisation channel for view state that no one
has asked to share.

### Collapsed content becomes a manifest, not an absence

Hiding a range currently erases what is inside it, including the
reviewer's own notes. Instead, a hide is headed by a two-column list
(old / new — the side is load-bearing) of line number plus first line,
coloured by kind, floated to the top of the hide.

What appears there:

- Generated summaries **drop out** — a fold summary makes no sense
  under an outer fold.
- Resolved comments **drop out**.
- Unresolved reviewer comments **stay**, truncated to one line. Whether
  a comment was derived from an LLM annotation does not matter; whether
  a human owns it does.

Comments and fold summaries diverge here on purpose. A summary is a
description of hidden content and is redundant once the content is
hidden again; a comment is a thing the reviewer needs to know exists,
wherever it sits.

The manifest is **not navigable** — entries do not scroll or reveal on
click — until testing shows people expect it. Its size is **not
bounded**; a file rarely carries an unmanageable number of notes.

## Consequences

The `> ` marker collapse's identity changed on both sides of the wire.
Only the viewer was expected to need it: both detectors were reading a
snapped region's ranges off the rows rather than off the
`FoldSymbolSpan` they had just snapped to, so changing one alone would
have desynced the viewer's addresses from the server's `fold_regions`
and from every persisted `FoldDescription` keyed on the same tuple —
silently dropping re-seeded fold summaries on the next run. The
cross-language fixture had not caught it because it only exercised
whole-definition hunks, where the two agreed by accident.

Three behaviour changes fell out of the new address, all corrections
rather than regressions, none of them invisible:

- a block the hunk truncates folds to its real end, not the last
  visible row;
- a definition present on one side only is addressed on that side
  alone, so `context` can go `both` → `right`;
- `/fold-summary` receives the whole definition's line range, so its
  prompts carry more content. `extract_fold_body` has no cap: a
  500-line class ships 500 lines. Summaries are generated on demand, so
  the cost is per-interaction rather than per-review.

Content-derived detection needs file content, which today means
`head_lines` — head-side only, capped at 5,000 lines, absent for
generated, binary and deleted files. Those files fall back to the
row-derived path and keep #10. The fallback is explicit and commented
rather than silent, because a quiet degradation here reads as a fix that
works everywhere. Slice 6 removes the limit by moving to the lazy
`/file-text` route.

Detection became O(rows × definition spans) over the whole file rather
than over the rendered rows, so it is memoised per `FileBlock` and
invalidated on `HunkBlock` identity change.

Python-side indentation regions stay per-hunk and row-derived —
`build_hunk_viewer_block` has no whole-file content. The existing
JS/Python divergence for indentation regions therefore widens: on a file
with no `fold_symbols` (an unsupported language), a previously generated
fold summary may fail to re-seed. `_fold_symbol_from_viewer_json`
already tolerates unmatched client-side regions, so nothing fails
loudly. Converging both sides on one content source is Slice 6's job.

Moving detection to `/file-text` makes it asynchronous. That is safe
because a persisted `HiddenSpan` is self-describing: restoring one needs
no detection at all, only *offering new folds* does. State returns at
first paint; fold affordances arrive with the content.

## Alternatives considered

**Keep the row-index identity and re-key the record on every reveal.**
Smaller change, and it is roughly what PR #9 does. Rejected: it makes
every code path that inserts a row responsible for maintaining fold
state, which is how the bug arose. #9 is left as residual and expected
to close unmerged — its `_folded` would now land on a key that holds,
but the record it hangs off is replaced anyway.

**Portable structural ids** (`qualified_name` + ordinal) rather than
absolute lines. Survives editing the file underneath the viewer and
would make URLs shareable. Rejected because neither matters here: the
diff endpoints are pinned for a run's lifetime, and the scope is one
user on localhost.

**`localStorage`, IndexedDB, or OPFS** for persistence. All fail for the
same reason — origin-scoped storage against a per-run kernel-assigned
port. OPFS additionally buys a filesystem API for state that is a few
hundred bytes.

**A fixed port**, to make browser storage persist across runs. Rejected:
it trades a collision with anything else on that port, and concurrent
runs of scr against each other, for persistence of view state that is
cheap to rebuild.

**Reveal stays irreversible**, as GitHub's does. Consistent with a
familiar UI and less state to carry. Rejected because irreversible is
the property that makes the current behaviour surprising — an accidental
reveal cannot be undone, and a re-render undoes it for you.

**Bulk actions scoped to visible items only.** Considered as the
mechanism for making the top bar non-destructive, with the `uv.lock`
case as its test. The decision recorded here is the requirement — user
state survives a bulk action, nesting and all — not this particular
implementation of it; Slice 3 settles the mechanism against the one
visibility function rather than against the current override map.

**Collapse the manifest to a count** ("12 hidden comments"). Cheaper and
trivially bounded. Rejected: the reviewer's own notes are the thing they
most need to find again, and a count does not let them find one.
