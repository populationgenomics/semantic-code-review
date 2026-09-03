# ADR 0008 — Hide by the diff, fold by the structure, label by meaning

- Status: Accepted — slices 1–3 in progress; 4–6 wait on the open questions
- Date: 2026-09-03

## Context

The viewer has four independent ways of showing less:

| mechanism | scope | source |
|---|---|---|
| fold level (`files \| hunks \| segments \| off`) | global | keys 1–4, URL hash |
| fold regions (chevrons inside a hunk) | per hunk | indent / symbol detector, LLM summary on demand |
| collapsible regions (expand-N chips) | between hunks | also absorbs hunks a filter demotes |
| rendered mode's ladder (`sections \| runs \| open`) | per `.md` file | its own state |

plus per-item overrides and an ephemeral focus reveal. CONTEXT.md needs three
glossary entries — Fold level, Fold region, Collapsible region — whose main job
is to say which of the others each one is not.

They are answering two different questions. Collapsible regions **hide**:
unchanged context is off screen by default because relevance comes from the
diff, and the reviewer reveals it. Fold regions and the ladder **fold**:
content that is on screen is shown at less detail, and the reviewer unfolds
it. A changed function can be folded to its signature; a revealed unchanged
one can be too. Hiding follows the diff, folding follows the structure, and
the two compose — hide, reveal, fold. Rendered mode already has both in one
ladder: `runs` hides unchanged runs behind a chip, `sections` folds the
heading tree.

Two of these mechanisms are defined by a semantic concept. The `segments` rung
shows one summary row per [[segment]], where a segment is the per-hunk pass's
grouping of changed lines by intent, addressed by `new_start`/`new_count` the
model emits. The rung therefore needs every hunk to carry a flat, ordered,
covering list of segments — a segment-less hunk gets a synthetic whole-hunk one
so the level is uniform. That obligation is what forbids segments from nesting.

Measured against the cached corpus (122 runs, 1074 hunks):

- The model's segments are dropped 7.4% of the time (44 of 553), on 23% of the
  hunks it tries to segment. 19 are an inclusive/exclusive end mix-up at the
  hunk boundary; 18 of 444 multi-segment hunks nest — a region, then a callout
  inside it — which a flat partition cannot express; the rest are pre-image
  coordinates or ranges leaving the hunk. #23 recovers 27 by stating the
  range and clamping. The nesting cannot be prompted away: the model is
  describing hierarchy, and the schema is a partition.
- 53 pure-insertion runs start mid-body with the new definition's opener
  appearing later in the block — the differ anchored on a shared trailing
  stanza. They concentrate in large insertions (89, 56, 35, 33 lines). Git's
  indent heuristic slides hunk boundaries, not runs inside a mixed hunk, so
  the algorithm choice does not reach them (myers and histogram agree on every
  file tested).
- The fold-region detector is implemented twice, in Python
  (`hunk_layout.compute_fold_regions`) and TypeScript (`folds._computeFoldRegions`),
  as line-for-line translations held in step by a JSON fixture — while the
  server already ships `fold_regions` on the wire.

#22 established the pattern this ADR generalises: the overview pass spent 89%
of its output (16k tokens, ~56 s of a 237 s run) retyping the deterministic
symbol delta verbatim, and the delta itself flagged 251 symbols as modified of
which 6 had changed declaration. Moving both from "ask the model" to "parse it"
gave exact results and most of the latency back. Line coordinates are the same
kind of job: the thing the model is worst at and tree-sitter is exact at.

## Decision

**Three axes, three sources. The diff decides what is shown by default. The
structure decides at what detail. The model decides what it means. None of
them defines another's levels, and none of them bounds what the reviewer can
reach.**

### Hiding follows the diff — as a default, never a bound

What is on screen by default is what changed. Unchanged context sits behind
an expand chip, and a hunk a sidebar filter demotes joins it there. This is
the collapsible-region model as it stands; the decision is that it is the
*only* hiding mechanism, and that it is not a fold. Revealing hidden content
puts it on screen at the current fold depth — it does not open it.

**Everything in the file is disclosable.** The hidden set is the whole file
on both sides minus what is shown, not the diff's context lines minus what
is shown. The unified diff carries hunks and three lines around each; the
rest of the file lives in the run's `base/` and `head/` worktrees, and a
reveal that needs it fetches it. Disclosure is therefore lazy and uncapped:
nothing about a file's size, its side, or whether it survives to head may
make a line unreachable.

### Folding follows the structure

Fold regions come from the AST — function, class and block boundaries
(parked item 14) — with the indent detector as the fallback for languages
without a grammar. Computed once, in Python, and shipped on the wire; the
TypeScript consumes `fold_regions` and stops re-deriving them. The
cross-language fixture becomes an ordinary Python test.

The global ladder becomes depth over that structure, applied to whatever is
shown. `segments` stops being a rung: the level between "hunk" and "raw code"
is a structural depth, not a semantic partition, so every hunk has one by
construction and nothing is synthesised. Folding a region shows its label if
one exists — the fold-summary pass, or an annotation span that covers it —
and its signature otherwise.

### Positioning follows the diff, scored by the structure

A pure insertion run can slide up *k* lines exactly when its last *k* lines
equal the *k* lines above it; every position in that range renders the
identical file. The viewer places the run at the position that starts on a
definition opener or a blank line, scored from the AST where one exists and
from indentation otherwise. This is a rendering choice over the same content
and changes nothing the differ recorded. It applies to runs, which is the
granularity git's heuristic does not reach.

### Labelling is semantic, and anchored to structure

Segments and line notes become one thing: an **annotation span** — a range on
the post-image with an intent, smells, refs and a stable id. Spans nest, may
cross hunks, and are under no obligation to cover or partition anything. A
segment is a span over several lines; a line note is a span over one.

The model does not emit coordinates. It labels spans that already exist — the
structural spans from the AST, or a choice among candidate ranges the
structural layer proposes — so a label cannot be out of range, overlap
incoherently, or land in pre-image coordinates. Every drop bucket measured
above disappears by construction rather than by instruction.

### What this keeps from ADR 0001

Stance C — the semantic and structural layers do not reconcile — holds for
*meaning*. The model still decides what a change is about, which lines belong
together, and why. What moves to the structural side is only the *anchor*:
where a label sits. CONTEXT.md's line that segments "need not line up with one
symbol" stays true; a span may cover half a function or three. What changes is
that its edges are chosen from positions that exist rather than typed.

## Consequences

- Two mechanisms, orthogonal, where there were four tangled: one hides by the
  diff, one folds by the structure. Rendered mode is the existing proof —
  `runs` is its hide, `sections` its fold, with the heading tree as the
  structure — and unifies rather than staying separate.
- The per-hunk output schema loses `segments[].new_start`/`new_count` in favour
  of references to structural spans or candidate ranges. The prompt no longer
  explains inclusive ends, because there are none to explain.
- `SegmentBlock`, the `seg-list` renderer and the synthetic whole-hunk segment
  go. The `segments` fold rung goes; the depth ladder replaces it.
- Comment anchors are `(file, side, line)` and are unaffected by any of this;
  positioning reorders rows within a hunk but not the lines they carry.
- Disclosure today is an eager `head_lines` bundle in `/data.json`, capped at
  5,000 lines and head-side only: a larger file cannot be expanded past the
  diff's context, and a deleted file cannot be expanded at all. Both violate
  the invariant. The lazy `/file-text` route rendered mode already uses serves
  both sides with no cap; region expansion moves onto it, the bundle and its
  cap go, and `/data.json` shrinks by the full text of every file it carried.
- Spans that cross hunks need a pass that sees more than one hunk. The
  per-file batch pass exists and is disabled (`batch_size`, parked); this ADR
  does not decide which pass owns segmentation, only that the anchors are
  structural. Until that is decided, spans are bounded by the hunk their pass
  saw.
- Candidate 5 of the architecture review (the duplicated fold-region
  algorithm) is resolved as a side effect rather than on its own.

## Alternatives considered

**Keep segments, fix the prompt.** #23 does this and recovers 27 of 44 drops.
It cannot reach the 18 nested cases, because they are not a coordinate error
— the model is describing hierarchy the schema forbids — and it leaves the
model computing line numbers, which is the root of every bucket.

**Replace segments with free-nesting spans and nothing else.** Fixes nesting
and cross-hunk labels but removes the ladder's middle rung with no structural
depth to put in its place; the viewer would fold from hunk straight to raw
code. The hiding side has to be solved for the labelling side to be freed.

**One segment per AST node (parked item 15).** Makes segments deterministic
outright, but collapses the semantic layer into the structural one: a
refactor spanning three methods becomes three segments with no way to say it
is one change. Rejected by stance C for the same reason it was rejected in
ADR 0001. Anchoring to structure is not the same as being structure.

**Slide hunks with `--indent-heuristic` / `--histogram`.** Already the
default; agrees with myers on every real file tested; operates on hunk
boundaries and does not reach a run inside a mixed hunk.

## Open questions

- Depth semantics for the ladder: AST depth is uneven across languages and
  files. The rung may want to be "one level below the enclosing definition"
  rather than an absolute depth.
- Whether the model labels existing structural spans, or selects from
  proposed candidate ranges, or both. The first is simpler and
  cannot fail; the second preserves more of the current freedom.
- Which pass owns cross-hunk spans, and whether that is the batch pass
  turned on or a new file-level pass.
- Whether a reveal should ever open a fold. The decision says no — reveal
  puts content on screen at the current depth — but a reviewer clicking a
  symbol pill expects to see its code, which is a reveal *and* an unfold.
  `focusReveal` is that case today.
