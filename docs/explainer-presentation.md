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

The one affordance that is **markdown, not a schema field** — GitHub's
alert convention, a blockquote whose first line is a bracketed kind:

```
> [!NOTE]      → concept — a definition or load-bearing idea. Accent left border.
> [!WARNING]   → edge    — an edge case or hazard. Warn left border.
> [!TIP]       → aside   — tangential context. Secondary-accent left border.
```

Markdown has no callout of its own, so the general rule above would make
this a field. It is not, for one reason: a callout's *position* is most
of what it is for, and a field hangs a detached list off the section with
no place in the prose. The blockquote keeps it where the model put it.

The marker is consumed and replaced by a fixed uppercase label; the model
does not choose the label, so the rule that it never chooses presentation
survives. Three kinds only. An unrecognised marker stays an ordinary
blockquote rather than being restyled as a meaning nobody asked for.

Rendered by `_calloutify` over the sanitised DOM, for the same reason
`_chipify` runs there: nothing goes back through `innerHTML`.

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
{ svg: string, caption: markdown, alt: string, stripped: int }
```

See [Figures](#figures). `alt` is required and becomes the SVG's
`aria-label`; it is also what renders in place of a figure that lost
everything. `stripped` is the sanitiser's, not the model's — it is set
on the way to disk and rendered under the caption.

### Term list

```
{ terms: [{ term: string, definition: markdown }] }
```

For introducing several names at once without a paragraph each. Each
entry renders as a **concept callout** — a definition is exactly what
that kind is for, so the two share one visual instead of inventing a
second way to say the same thing. Stacked tighter than a standalone
callout: a run of them is a glossary, not a run of interruptions.

`term` is rendered as inline markdown, not text. These are the model's
words and often identifiers, so they carry code spans, and unlike an
alert's fixed label the title is not uppercased — `ServiceImpl<typeof
Workbench>` in caps is not the name of anything.

The same holds for every short schema string the model writes: a
subsection's title goes through the same inline render. A field rendered
as `textContent` while its body goes through markdown is how a backtick
reaches the page.

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

Enforced server-side on the way to disk (`augment/explainer_figures.py`,
called from `save_explainer`, so every route that writes a document gets
it), and again at render behind DOMPurify's SVG profile
(`viewer/assets/explainer_figure.ts`), because a document can reach a
browser from a run directory that server never wrote.

- Allowed elements: `svg`, `g`, `defs`, `marker`, `rect`, `circle`,
  `ellipse`, `line`, `polyline`, `polygon`, `path`, `text`, `tspan`,
  `title`, `desc`. Anything else goes with its subtree — `<script>`,
  `<foreignObject>`, `<use>`, `<image>`.
- Attributes are an **allowlist**, per element: the geometry each
  element needs, plus `class` and `transform`. That removes the
  presentation attributes the model must not choose — `fill`, `stroke`,
  `stroke-width`, `stroke-dasharray`, `style`, `font-family`,
  `font-size`, `font-weight`, `color`, `opacity` — and everything else
  nobody enumerated, including `on*` handlers and every namespaced
  attribute (`xlink:href`).
- No element in the allowlist can carry an `href`, so external
  references have nothing to hang on. The one surviving reference form
  is `marker-start`/`-mid`/`-end`, which must match `url(#id)` exactly.
- `class` is filtered to the vocabulary below; unknown classes are
  removed, and a `class` left empty goes with them.
- `viewBox` is required on the root; `width`/`height` there are stripped
  (the stylesheet sizes figures to the measure). `width`/`height` on a
  `rect` are geometry and stay.
- A DTD ends the figure rather than being sanitised around: nothing in
  the vocabulary needs one, and entity expansion is the only way an SVG
  this size is expensive to parse.

A figure that loses content to sanitisation is kept and the strip count
goes in its `stripped` field — the same drop/count/surface policy
references use. A figure that loses *everything* (unparseable, wrong
root, no `viewBox`) is still kept, rendering as its `alt` text.

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
with the head class applied to the marker's `path`. Ids are namespaced
per figure by both sanitiser passes — by section id and index on the way
to disk, by a render-scoped counter in the browser — so two figures on
one page cannot collide whatever the model called them.

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
- Headings, captions, labels and table headers: `--ui`, the sans stack
  the rest of the viewer uses, so chrome reads as chrome and prose reads
  as prose.
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
| `--ui` | the viewer's sans stack, named so the chrome inside a serif surface can ask for it |

Each is defined in both the default (dark) block and the
`prefers-color-scheme: light` block, matching the file's existing
structure. `--serif` carries the same value in both; `--ui` is defined
once, at `:root`, because `body` uses it too.
