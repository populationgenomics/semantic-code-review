# ADR 0004 — Rendered markdown diff

- Status: Accepted
- Date: 2026-07-14

## Context

For prose docs (`.md`), the line-grid diff is the wrong instrument: it
tells you *which lines changed* but not *whether the rendered result
reads well* — do headings nest, does the table render, is the link
syntax right. GitHub is preferred here because it renders markdown. The
question this ADR answers: what does "review markdown the way GitHub
lets you" actually require, and how much of scr's viewer does it touch.

The premise "side-by-side rendered markdown diff" conflates three
features. GitHub's rich-diff is **single-pane**: the head doc rendered,
with added/changed/removed blocks ribboned inline. It is *not* base-left
/ head-right — nobody renders true two-pane markdown because prose
reflows, so "the same line on both sides" stops meaning anything off the
monospace grid, and aligning two independently-rendered HTML trees is a
similarity-matching problem with no clean answer. The design below is
the two-pane version anyway, made tractable by refusing the matching
question rather than solving it.

Governing constraint: scr's existing viewer is a monospace two-column
**line-grid** keyed on row objects (`_renderDiffRows`, `hunk_layout.py`,
the row model in `types.d.ts`). A rendered aligned two-pane view shares
none of that machinery — it is a **second renderer**, not a feature
bolt-on. This ADR's job is to hold the second renderer to the minimum
that is still faithful.

## Decision

### Shape — a per-file toggle, rendered mode is a second view

A `.md` file gets a file-level toggle flipping its body between the
existing text diff and a rendered mode. The text diff stays
**authoritative**: it answers "what changed" precisely (hunks,
segments, line anchors) and owns commenting. Rendered mode answers only
the question the text diff answers badly — "does the finished prose read
well" — and shows the delta so you are not hunting.

Rejected — **replace the text diff for markdown.** The semantic layer
(hunks/segments/intent) and comment anchoring ride the text diff; losing
them to gain rendering is a bad trade. Two views, one keypress apart.

### Two-pane, not single-pane rich diff

Base rendered left, head rendered right. Rejected — **GitHub's
single-pane rich diff.** Single-pane forces the decision two-pane
refuses: is a reworded paragraph one *changed* block or a
*removed+added* pair? That is a similarity threshold with no right
answer (a threshold tuned for "fixed a typo" is wrong for "rewrote the
intro"). It also cannot show a changed **diagram** at all. Two-pane
renders each side independently and never has to match blocks across
sides — see below. Mermaid vindicates this: old-left / new-right is the
only sensible way to review a changed diagram.

### Delta by projecting the line diff — no similarity matching

Colour each block by its *own* line-diff status: a base block touching
removed lines is red, a head block touching added lines is green, a
rewritten paragraph is all-red left / all-green right. No cross-side
matching is computed. This is sound because **markdown blocks always
break on line boundaries**, so projecting line status onto blocks is
exact. Vertical alignment is the same anchor-and-pad logic a side-by-side
*line* diff uses, lifted to block granularity — also read straight off
the existing line diff.

Amended at slice-4 build: alignment projects the diff's *own row-level
pairing*, not a positional zip. A block that keeps ≥1 line the diff
aligns to the other side (a `ctx`/`pair` row) is `matched` and pairs 1:1
in order with the next matched block opposite; a block whose lines are
all one-sided (`del` only / `ins` only) drains against a blank cell.
This still reads straight off the line diff — no cross-side content
matching — but a *replaced* item now lands beside its replacement and a
*deleted* item on its own row, where the earlier zip-the-changed-runs
approach mis-paired them (e.g. delete-then-modify zipped the deleted
block against the modification).

Rejected — **AST/token similarity alignment.** Reintroduces the
threshold monster two-pane exists to avoid.

Known failure mode, accepted: git occasionally matches a common line (a
blank line, a `| --- |` table rule, a bare `>`) across unrelated
regions, giving a spurious anchor that mis-aligns the surrounding
blocks. Visible as an odd stitch once in a while; not worth engineering
around.

### Block-level granularity

Highlight is per-block ("this paragraph changed"). Rejected for v1 —
**intra-block word-level diff** (diff the inline token stream, re-render
with `<ins>`/`<del>`). It is the fiddliest single piece and block-level
is enough to review prose. Backlogged.

Amended at slice-4 build: shipped anyway, and *not* fiddly — the text
diff's own sub-diff already solves it. For a replaced pair (matched,
changed on both sides), run the text renderer's `blockDiff` (token LCS)
over each block's **rendered `textContent`** — markdown syntax already
stripped, so a `**word**` change marks `word`, not the asterisks — and
paint the ranges with the same `wrapRanges` the code cells use (it splits
text nodes and crosses inline elements like `<strong>`/`<a>`, so no
`<ins>`/`<del>` re-render and no invalid nesting). Deleted chars mark red
on the base pane, added chars green on the head — the `char-chg`
treatment lifted from the text diff. The feared HTML-token-diff was
avoided by diffing rendered text and reusing the existing range painter.

### Rendering in the browser; the Python side is empty

The browser renders (consistency), given the full base and head text
plus the line diff it already has in `ViewerData`. It parses both sides
with markdown-it — which it must, to render — getting block boundaries
with source-line maps; overlaying the line diff onto those maps yields
classification and alignment client-side. **Python computes nothing.**

The one new Python footprint: a server endpoint handing the browser the
**full base+head text of one file**, fetched lazily when a `.md` file is
flipped to rendered mode (the `base/`/`head/` worktrees are already
there; folds fetch-on-demand set the precedent). Most docs are never
toggled, so `ViewerData` stays lean — no eager full-text payload.

Rejected — **precompute block classification in `build_json.py`.** Worth
it only if a server-side LLM pass later annotates per-heading-section;
until then it is dead Python. Revisit then, not now.

### Fold model — collapse runs of unchanged blocks

The fold primitive is **collapse a contiguous run of unchanged (context)
blocks** into an expand chip. Identical source renders to identical
height, so a matched unchanged run is height-symmetric across panes by
construction — collapse it to an equal-height chip on both sides and
everything below stays aligned, with no extra alignment math. This is
scr's existing collapsible-region concept lifted to rendered blocks.

Unchanged **headings stay visible as landmarks** — fold runs break at
them. Collapsing across a heading saves pixels and costs the map that
tells you where the next change lives. So a heading fold is only a
*coarse convenience* ("collapse this whole section") and the source of
the sidebar outline labels — it is not the fold mechanism.

Rejected — **heading-exact-text-match as the fold criterion.** Duplicate
headings (`### Args`, `### Returns` recurring) make text-equality
ambiguous about which occurrence pairs with which; the diff's own
context classification resolves position for free. Rejected —
**swallowing unchanged headings into runs** for maximum collapse; loses
the landmark.

Consequence worth noting: paragraph-run folding **repairs the
renamed-heading case** for free. `## Setup` → `## Installation` over an
identical body: the heading block shows changed (correct), and the body
is a run of context blocks that folds anyway — no body-similarity
matching required.

### Controls — a parallel structural model in rendered mode

Rendered mode has no hunks or segments, so it swaps in the document
outline:

- **Fold ladder** `sections → runs → open` (collapse whole unchanged
  sections / collapse unchanged block-runs / everything open).
- **Outline** of the heading tree, each entry badged changed/unchanged.

Amended at slice-3 build: these are **per-file, in the file body**, not
the global PR-bar slider and sidebar. Rendered mode is a per-file toggle
(see Shape), and a review commonly mixes text-mode and rendered-mode
files; a single global slider can only reflect one mode's ladder, so
either it lies about the other files or one mode's controls sit inert.
Per-file in-body controls dodge that — each rendered `.md` owns its own
ladder + outline and touches no other file's fold state. Cost: the fold
level is per-file, not one keystroke for the whole review. Superseded
here — **one shared slider reused across modes** (the original sketch:
`sections/runs/open` on the same slider + 1–3 keys); it cannot serve a
mixed text/rendered file set coherently.

### Expand affordance — three altitudes, not three buttons

The reveal granularities are block / section / document — the complete
set — attached where muscle memory expects them, not stacked on one
chip:

- **Chip:** a chevron at each end. Single click reveals **one para**
  from that end; shift-click reveals **to the section boundary** (fills
  the run on that side — a chip never crosses a heading, so this never
  leaps a landmark). A single reveal repaints the file, which discards
  the chip, so `dblclick` can't fire across it — shift-click is the
  boundary gesture.
- **Outline entry:** expand a whole **section** (all collapsed runs
  within one heading's scope).
- **Ladder:** `open` expands the **document**.

State cost: the collapsible region stops being binary (folded/live) and
carries `revealedFromTop` / `revealedFromBottom` counts. These are
**ephemeral**, cleared when the global fold slider moves — same
behaviour as the existing overrides (`_setGlobalFold` is authoritative),
so partial expansions never leak into a fresh fold level.

### Layout rule — block-pairs are max-height grid rows

**Each aligned block-pair is one grid/flex row whose height is
`max(left, right)`.** This is load-bearing. Content whose height is
async or content-dependent — mermaid, KaTeX, images, a table that grew a
row — then reflows only its own row and never disturbs global alignment.
A layout that precomputes vertical offsets breaks the instant async
content settles. Build it as max-height rows and there is no
re-alignment pass and no off-by-a-few-pixels drift.

### Fidelity and sanitization

- **GFM core** (tables, task lists, strikethrough, autolinks): table
  stakes.
- **Math (KaTeX)** and **Mermaid (`securityLevel: 'strict'`)**: both
  fall into block handling — display math and mermaid are blocks, inline
  math lives inside a paragraph block; the max-height grid absorbs their
  heights.
- **Sanitization:** markdown-it HTML output goes through DOMPurify
  (untrusted `.md` can carry `<script>`, `javascript:`, tracking-pixel
  images). Math and mermaid render from their *source* delimiters via
  their own renderers, so the sanitized-HTML path and the
  controlled-renderer path never mix.

### Comments — reuse the existing anchor

No new comment machinery. scr's anchor is already `(file, side, line)`;
a rendered block carries its source line via the markdown-it token map,
so commenting a block lands on that line and round-trips unchanged.

## Consequences

- The viewer gains a **second renderer** — block diff engine, block-pair
  grid layout, run/section fold model, per-file in-body ladder + outline
  — roughly doubling the rendering surface. Effort is weeks, not a
  weekend.
- New bundle dependencies: markdown-it, DOMPurify, KaTeX, mermaid.
  Mermaid is heavy; weigh it against the value of reviewing diagrams.
  Amended at slice-4 build: only markdown-it + DOMPurify enter
  `viewer.js`. Mermaid (already vendored for the review console) and
  KaTeX are **vendored + lazy-loaded** by `<script>`/`<link>` injection
  the first time a rendered `.md` needs a diagram or math, so neither
  weighs on the base bundle (which stayed ~465 KB). Both render through
  shared modules (`mermaid.ts`, `katex.ts`) off the DOMPurify path;
  KaTeX's woff2 fonts are served from `vendor/fonts/`.
- One new server endpoint (full base+head text per file). Amended at
  slice-4 build: also a static route for the vendored KaTeX js/css +
  woff2 fonts; no other Python change.
- A third structural notion — the **document outline** — coexists with
  the LLM-semantic (segments) and tree-sitter-structural (symbols)
  models. Same posture as ADR 0001: they answer different questions,
  they do not reconcile. A term to pin in CONTEXT.md when the first
  slice lands.
- Rendered mode is second-class for the augment pipeline: intent /
  smells / segments stay in text mode. Accepted — rendered mode is a
  reading aid, not a review surface.

## Backlog (deliberately not v1)

- ~~**Intra-block word-level diff**~~ — done at slice-4 build by reusing
  the text diff's `blockDiff` + `wrapRanges` over rendered `textContent`
  (see Block-level granularity). Painted as `char-chg` marks, not
  literal `<ins>`/`<del>` elements.
- **Incremental expand promoted to the shared region model** — the
  reveal-count state is not markdown-specific; lifting it to the
  collapsible-region abstraction gives the *code* text diff GitHub-style
  expanders for free (different renderer, same state).
- **Per-heading-section LLM annotation** — the one thing that would pull
  `build_json.py` back in.
- **Comment on base-side deletions** — anchor to base line for removed
  content; the anchor model already carries `side`.
