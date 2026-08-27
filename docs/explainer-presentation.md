# Explainer presentation contract

The closed vocabulary the [change explainer](adr/0007-change-explainer.md)
document is built from. Two consumers must agree on it: the prompt that
tells the model what it may emit, and the viewer stylesheet plus
sanitiser that render and enforce it. Drift between them is how the look
degrades — a class the model uses and the stylesheet does not define
renders unstyled; a class the stylesheet defines and the prompt does not
mention is never used.

Governing rule: **the model never chooses presentation.** It emits prose
and geometry. Every colour, font, weight, stroke width and spacing
decision lives here.

## Prose

Markdown, rendered by `markdown-it` with `html: false` and passed
through DOMPurify — the path `console_render.ts` already uses. Raw HTML
in prose is not interpreted, by configuration, not by convention.

Allowed and styled: headings (h3–h5 within a section; h2 is the section
title and is emitted by the viewer), paragraphs, ordered and unordered
lists, `code` spans, fenced code blocks (highlighted by hljs; a
`mermaid` fence is not special here), tables, emphasis, links.

Anything the model wants that markdown cannot express is a structured
field below, not markup.

### Inline references

A reference inside prose is written as a bracketed viewer id — `[F3]`
for a file, `[H3_1]` for a hunk. The renderer swaps each for a chip
after sanitisation, building DOM nodes over the sanitised tree rather
than a second HTML pass. Tokens inside `code` spans and fenced blocks
are left alone: a `[F3]` in a snippet is the snippet's.

A chip is labelled by what the reviewer can act on — the file's path,
or `path:N` for the Nth hunk of that file — not by the raw id. Clicking
a file chip leaves overview mode and scrolls; clicking a hunk chip also
unfolds it, because a hunk reference is the claim "read these lines".

A chip is not a substitute for the section's `refs` list, which is what
the sidebar counts and the coverage footer reads.

## Structured affordances

Each is a field on a section with a closed `kind` enum. The viewer
styles them; the model supplies content only.

### Callout

```
{ kind: "concept" | "edge" | "aside", title: string, body: markdown }
```

- `concept` — a definition or load-bearing idea. Accent left border.
- `edge` — an edge case or hazard. Warn left border.
- `aside` — tangential context. Secondary-accent left border.

`title` renders as a small uppercase label above the body.

### Skip box

```
{ body: markdown, target_section_id: string }
```

Renders as a dashed muted box: "If you already know X, jump to Y." Only
valid in **Background**, whose first layer is written for a reader new
to the system. `target_section_id` must resolve to a section in the same
document; an unresolvable target drops the box.

### Figure

```
{ svg: string, caption: markdown, alt: string }
```

See [Figures](#figures). `alt` is required and becomes the SVG's
`aria-label`.

### Term list

```
{ terms: [{ term: string, definition: markdown }] }
```

Renders as a definition list. For introducing several names at once
without a paragraph each.

### Citation line

```
{ sources: [path] }
```

Not a model-supplied field: `sources` is recorded from the tool surface
as a pass runs, so it states what was opened rather than what the model
says it opened. Rendered under Background as a muted line, **including
when it is empty** — a section citing nothing is the case the affordance
exists to make visible.

### Map row

The **Map** section is a list of these, in reading order:

```
{ ref: FileRef, why: markdown }
```

Renders as a two-column table — `read` / `because`. `ref` is a file
reference; see the ADR's anchor rules.

### Toy-data notice

A document-level boolean. When any section uses invented examples, the
footer states it: identifiers, counts and values in worked examples are
illustrative. Intuition normally sets it.

## Figures

Inline SVG in a structured slot — not embedded in prose markdown.

### Sanitiser rules

Enforced server-side before the document is written, and again by
DOMPurify's SVG profile at render:

- Presentation attributes are **stripped**: `fill`, `stroke`,
  `stroke-width`, `stroke-dasharray`, `style`, `font-family`,
  `font-size`, `font-weight`, `color`, `opacity`.
- `class` is filtered to the vocabulary below; unknown classes are
  removed.
- `<script>`, event handlers (`on*`), `<foreignObject>`, external
  references (`href`, `xlink:href` to anything but a same-document
  `#id`) are removed.
- Allowed elements: `svg`, `g`, `defs`, `marker`, `rect`, `circle`,
  `ellipse`, `line`, `polyline`, `polygon`, `path`, `text`, `tspan`,
  `title`, `desc`.
- `viewBox` is required; `width`/`height` are stripped (the stylesheet
  sizes figures to the measure).

A figure that loses content to sanitisation is kept, and the strip count
is recorded — the same drop/count/surface policy references use.

### Class vocabulary

Every class resolves to a theme custom property, so a figure is correct
in both colour schemes without the model knowing which is active.

**Boxes and frames**

| class | use |
|---|---|
| `d-box` | default container |
| `d-box-alt` | container one step raised from `d-box` |
| `d-box-acc` | container carrying the primary accent |
| `d-box-acc2` | container carrying the secondary accent |
| `d-box-warn` | container marking a hazard or failure path |
| `d-box-ok` | container marking a success path |
| `d-frame` | dashed boundary — a trust region, a deployment, a scope |
| `d-fill-bg` | surface-filled shape, for overlaying other shapes |

**Text**

| class | use |
|---|---|
| `t` | body label |
| `t-b` | emphasised label — a component name |
| `t-sm` | secondary label |
| `t-cap` | small uppercase caption, for region titles |
| `t-mono` | identifier, call, or literal |
| `t-mono-sm` | secondary identifier |
| `t-acc` / `t-warn` / `t-ok` | short status labels |

**Lines and heads**

| class | use |
|---|---|
| `ln` | primary flow |
| `ln2` | secondary flow, distinguished from `ln` |
| `ln-mut` | dashed, de-emphasised relation |
| `ln-warn` / `ln-ok` | failure / success flow |
| `ln-thin` | structural rule, not a flow |
| `head` / `head2` / `head-mut` / `head-warn` / `head-ok` | arrow heads, paired with the line class of the same name |

**Marks**

| class | use |
|---|---|
| `hl` | highlight over an existing shape |
| `chip` | small accent pill, for a value or count |
| `rule` | thick rounded separator |

Arrow heads are `<marker>` elements defined in the figure's own `defs`,
with the head class applied to the marker's `path`. Marker ids must be
unique within the figure; the renderer namespaces them per figure so two
figures on one page cannot collide.

### Consistency across a document

The skeleton pass fixes, for the whole document:

- the **figure family** — which shapes carry which meaning, so a
  component is drawn the same way in every figure;
- the **recurring cast** — which components appear, and which data
  object is traced through worked examples.

Section prose passes are told the family and cast rather than choosing
them. This is what makes the figures read as one document rather than
three that each invented a visual language.

Layout is not fixed. Positions are the model's, which is the point —
a figure can place two framed regions side by side above a third. What
is fixed is what things look like, not where they go.

## Typography

Overview mode has its own type scale; it does not inherit the diff's
dense monospace chrome.

- Body: serif stack, ~18px, line-height ~1.62.
- Measure: ~72ch for prose. Figures may exceed it slightly.
- Headings, captions, labels and table headers: the UI sans stack, so
  chrome reads as chrome and prose reads as prose.
- Code spans and blocks: `--mono`, as elsewhere in the viewer.

## Tokens

The classes above resolve to viewer.css custom properties. Existing
tokens are reused (`--bg`, `--bg-alt`, `--bg-panel`, `--fg`,
`--fg-muted`, `--fg-dim`, `--border`, `--accent`, `--ok`, `--warn`,
`--mono`). Added for this surface:

| token | use |
|---|---|
| `--accent2` | secondary accent, for a second flow or actor |
| `--box` | figure container fill |
| `--box-alt` | raised figure container fill |
| `--accent-soft` / `--accent2-soft` | tinted fills behind their accents |
| `--warn-soft` / `--ok-soft` | tinted fills behind their accents |
| `--serif` | reading typeface stack |

Each is defined in both the default (dark) block and the
`prefers-color-scheme: light` block, matching the file's existing
structure.
