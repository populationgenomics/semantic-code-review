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

Nor does the reviewed repository. `[augment].explainer_prompt` (ADR
0007's second addendum) adds house style — voice, level of detail — to
the explainer passes' guidance, and it does not widen this vocabulary.
What it asks for beyond the closed set is dropped by the same two
mechanisms everything else is: the sanitiser's element and `class`
allowlists, and reference validation by membership. Neither consults the
prompt.

## Prose

Markdown, rendered by `markdown-it` with `html: false` and passed
through DOMPurify — the path `console_render.ts` already uses. Raw HTML
in prose is not interpreted, by configuration, not by convention.

Fenced code blocks are highlighted by hljs and are a first-class part of
the prose, not a fallback: the prompt asks for a verbatim extract where
the code says it better than a sentence about the code would — a
signature that is the whole contract, a guard whose condition is the
point. Bounded to the lines that carry the claim, with the claim stated
next to them, and anchored by a reference so the reader can reach the
full context. An unbounded quote is the diff again, which the reviewer
already has.

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

A chip is labelled by what the reviewer can act on, never by the raw
id: the path's basename for a file reference; for a hunk reference the
basename alone where its file has one hunk, and `basename · hunk N`
where it has more. Not `path:N` — that is the universal file:line form,
and on a diff of new files every chip read `sheaf.md:1`. Where the
prose has just named the path the label would be a repetition, so the
chip is a bare arrow. The `title` states the target in full either
way — `hunk 2 of 5 in path`.

Clicking a chip opens that file in a detail panel beside the document,
so the code and the sentence that sent the reader to it are on screen
together; a hunk chip opens with its hunk unfolded, because a hunk
reference is the claim "read these lines". The panel's "Open in diff" is
the way on to the full ladder.

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
everything. `caption` is inline markdown, as every short string the
model writes is. `stripped` is the sanitiser's, not the model's — it is
set on the way to disk and rendered under the caption. Each prose call
rewrites the whole document, so it is the highest count any write
recorded: the second pass over an already-clean figure finds nothing to
remove, and that is not the figure losing nothing.

### Term list

```
{ terms: [{ term: string, definition: markdown }] }
```

Renders after the section's prose and figures, above its subsections.
It consolidates names the prose has already introduced rather than
introducing them, so a definition can lean on what the reader just
read; ahead of the prose it was a glossary met before anything that
made it mean something.

Each entry renders as a **concept callout** — a definition is exactly
what that kind is for, so the two share one visual instead of inventing
a second way to say the same thing. Stacked tighter than a standalone
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
says it opened. Rendered under any prose section as a muted line,
**including when it is empty** — a section citing nothing is the case
the affordance exists to make visible.

The list belongs to the *call*, not the section: every section one pass
wrote carries the same one, and the line renders once, under the last of
them. Two identical citation sentences under a merged pair say nothing
the first one did not.

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
- `viewBox` is required on the root; `width`/`height` there are stripped.
  The renderer sizes the figure instead: `width: 100%` of the column,
  capped by an inline `max-width` of the viewBox's own width. Label sizes
  in the class vocabulary are px, which inside an SVG are viewBox user
  units, so a figure rendered wider than it was drawn magnifies every
  label; the cap holds one unit to at most one pixel, and a figure
  narrower than the column is centred. The prompt asks for a canonical
  650-unit width, which is about what the figure well is: the 72ch text
  measure plus the figure's own -4ch bleed each side.
  `width`/`height` on a `rect` are geometry and stay.
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

### Quantity and kind

The prompt asks for figures freely — a section of a large change usually
earns two or three — and names the kinds worth reaching for beyond a
component-and-arrow diagram: a state machine, a fan-out, a decision
ladder, a screen sketch, a before-and-after pair, a trust boundary.

That is a deliberate reversal. The guidance originally closed with "draw
a figure only where a spatial relationship is doing work", which read as
a discouragement and produced two figures where the hand-run prior art
produced seven on the same change. The mechanism was never the
constraint; the dial was set conservatively and the ambition of the
repertoire was never named at all.

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

- Body: serif stack, 17px, line-height 1.65.
- Measure: 72ch of text. The pane's `max-width` is that plus its two
  32px side paddings, which `box-sizing: border-box` would otherwise
  charge to the measure; the column is centred by auto side margins
  rather than page padding, so it holds inside a container of any width.
  Figures may exceed it slightly.
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
