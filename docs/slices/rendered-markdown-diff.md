# Slices — Rendered markdown diff

Implements [ADR 0004](../adr/0004-rendered-markdown-diff.md): a per-file
toggle flipping a `.md` file's body from the text diff to a two-pane
rendered view with block-level delta, run folding, and a document
outline.

Vertical slices, ordered. Each ends in something that ships and is
exercisable on its own; later slices add capability but never block
earlier ones from landing.

## Background — where the seam falls

The text-diff renderer (`_renderDiffRows`, `hunk_layout.py`, the row
model) is untouched. Rendered mode is a **separate body renderer** the
file-level toggle switches to. Everything it needs is client-side given
two inputs: the full base+head text of the file, and the line diff
already in `ViewerData`. Python's whole contribution is an endpoint that
serves the first.

## Shared currency

Per rendered `.md` file, the browser holds:

- **Full base and head source** (fetched lazily on first toggle).
- **markdown-it token trees** for each side — block boundaries with
  `token.map = [startLine, endLine]` back into source.
- The existing **line diff** — used to classify each block (all lines
  context → unchanged; touches added lines → added/changed; touches
  removed lines → removed) and to derive vertical alignment.

The governing rule for layout, applied to every aligned block-pair:

> Each block-pair is one grid row whose height is `max(left, right)`.
> Async/variable-height content (mermaid, KaTeX, images) reflows its own
> row and never disturbs global alignment.

Graceful degradation: a file whose base or head text is unavailable, or
markdown that fails to parse, falls back to the text diff — rendered
mode is simply not offered.

---

## Slice 1 — Full-text endpoint + head-only render

The data path and the toggle, no diff yet.

- Add a server route serving the full base and head text of one file
  from the `base/`/`head/` worktrees (reuse the fold-context fetch
  path). Lazy: called when a `.md` file is first flipped to rendered
  mode.
- Add the file-level toggle to the file header for `.md` files.
- Rendered mode renders **head only**, single pane: markdown-it → GFM →
  DOMPurify. No base, no delta, no folds.

**Done when:** flipping a `.md` file shows its head version rendered
faithfully (headings, tables, links, task lists), sanitized, and
flipping back restores the text diff untouched. A non-`.md` file shows
no toggle.

## Slice 2 — Two-pane, block-level delta, comment anchors

The side-by-side rendered diff.

- Fetch and render **base and head** into two panes.
- Parse both to token trees; classify each block from the line diff;
  colour base blocks red / head blocks green / unchanged neutral.
- Lay out aligned block-pairs as **max-height grid rows**, padding the
  shorter side; unchanged runs are height-symmetric and anchor the
  alignment.
- Wire commenting: a block carries its source line via `token.map`, so a
  comment on a block reuses the existing `(file, side, line)` anchor and
  round-trips unchanged.

**Done when:** a changed `.md` renders base-left / head-right with
changed blocks highlighted and matching content vertically aligned,
comments land on the right source line and round-trip, and a rewritten
paragraph reads as all-red left / all-green right.

## Slice 3 — Run folding, landmarks, outline controls

Collapsibility and navigation.

- Fold contiguous runs of unchanged blocks into an expand chip, folding
  both panes in lockstep (runs are height-symmetric, so alignment
  holds). Break runs at unchanged headings — headings stay visible as
  landmarks.
- Apply the min-run threshold (don't collapse a 1–2 block gap) and
  context bleed (keep K unchanged blocks around each change).
- Chip chevrons at each end: single click reveals one para from that
  end, shift-click reveals to the section boundary. Reveal-count state is
  ephemeral, cleared when the fold level moves.
- Per-file **in-body** fold ladder `sections → runs → open` and a heading
  outline badged changed/unchanged; an outline entry expands its whole
  section. (Amended from the ADR's shared PR-bar slider/sidebar: rendered
  mode is per-file, and a mixed text/rendered file set can't share one
  global ladder coherently — so the controls live in each file body.)

**Done when:** a lightly-edited large doc collapses to its changed
blocks plus context with headings still visible; the chevrons, ladder,
and outline reveal at block/section/document granularity; and flipping
to text mode restores the text diff (its hunk/segment ladder and sidebar
were never touched).

## Slice 4 — Math and mermaid

Fidelity for scr's own docs and technical prose.

- Render display + inline math with KaTeX, mermaid fences with
  `securityLevel: 'strict'`, both from their source delimiters (not via
  the sanitized-HTML path).
- Verify the max-height grid absorbs their async/variable heights
  without alignment drift; a changed mermaid diagram shows old-left /
  new-right.

**Done when:** a doc with math and a mermaid diagram renders both
correctly in both panes, a changed diagram reads old-beside-new, and
alignment holds after async render settles.

---

## Not in these slices

- **Intra-block word-level diff** — inline `<ins>`/`<del>` within a
  changed block (ADR 0004 backlog). Weighed once block-level is felt in
  use.
- **Incremental expand for the code text diff** — promoting the
  reveal-count state to the shared collapsible-region model so the row
  renderer inherits GitHub-style expanders.
- **Per-heading-section LLM annotation** — the only thing that would
  pull `build_json.py` into rendered mode.
