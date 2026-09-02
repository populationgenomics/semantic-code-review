// Explainer figures — render-time sanitisation and the <figure> element.
//
// The server sanitises a figure before it writes the document
// (augment/explainer_figures.py). This runs the same rules again,
// because a document can reach a browser from a run directory this
// browser's server never wrote — a hand-edited explainer.json, or one
// copied between machines. Model output derives from a diff that may be
// hostile, which is the threat model console_render.ts already names.
//
// Two passes: DOMPurify's SVG profile removes the script/handler/
// foreignObject class of problem, then an allowlist walk removes
// everything that is a *presentation* decision, because those belong to
// viewer.css and not to the model. Ids are namespaced per rendered
// figure, so two figures on one page cannot collide over a marker even
// if they were written with the same one.
//
// The class vocabulary is the contract in docs/explainer-presentation.md.
// It is duplicated here in the one place it has to be; a Python test
// (tests/test_explainer_figures.py) fails if the two copies drift.

import DOMPurify from "dompurify";
import { renderInlineMarkdown } from "./console_render";

const FIGURE_CLASSES = new Set([
  "d-box",
  "d-box-alt",
  "d-box-acc",
  "d-box-acc2",
  "d-box-warn",
  "d-box-ok",
  "d-frame",
  "d-fill-bg",
  "t",
  "t-b",
  "t-sm",
  "t-cap",
  "t-mono",
  "t-mono-sm",
  "t-acc",
  "t-warn",
  "t-ok",
  "ln",
  "ln2",
  "ln-mut",
  "ln-warn",
  "ln-ok",
  "ln-thin",
  "head",
  "head2",
  "head-mut",
  "head-warn",
  "head-ok",
  "hl",
  "chip",
  "rule",
]);

// Mirrors TEXT_CLASSES in augment/explainer_figures.py. These are the
// only classes that set font-*; every other class is paint. The split
// decides which elements a class may appear on, and misapplication is
// not cosmetic: a line class on a glyph sets fill:none and renders it
// invisible, and `chip` on one outlines the text instead of drawing the
// pill it names. A container takes either kind, because a class on one
// is inherited styling for its children; `title` and `desc` are metadata
// and are never painted.
const TEXT_CLASSES = new Set(["t", "t-b", "t-sm", "t-cap", "t-mono", "t-mono-sm", "t-acc", "t-warn", "t-ok"]);
const CONTAINERS = ["g", "svg", "marker", "defs"];
const TEXT_ELEMENTS = new Set(["text", "tspan", ...CONTAINERS]);
const PAINT_ELEMENTS = new Set(["rect", "circle", "ellipse", "line", "polyline", "polygon", "path", ...CONTAINERS]);

/** The elements `token` may appear on; empty when it is not vocabulary. */
function classElements(token: string): Set<string> {
  if (!FIGURE_CLASSES.has(token)) return new Set();
  return TEXT_CLASSES.has(token) ? TEXT_ELEMENTS : PAINT_ELEMENTS;
}

const MARKER_REFS = ["marker-start", "marker-mid", "marker-end"];
const GLOBAL_ATTRS = ["class", "transform"];

// Mirrors _ELEMENT_ATTRS in augment/explainer_figures.py. `width` and
// `height` are absent from `svg` on purpose: the renderer sizes a figure
// from the column and its viewBox (see `drawnWidth`), so a root
// dimension would fight it.
const ELEMENT_ATTRS: Record<string, string[]> = {
  svg: ["viewBox", "preserveAspectRatio"],
  g: [],
  defs: [],
  marker: ["id", "viewBox", "refX", "refY", "markerWidth", "markerHeight", "markerUnits", "orient", "overflow"],
  rect: ["x", "y", "width", "height", "rx", "ry"],
  circle: ["cx", "cy", "r"],
  ellipse: ["cx", "cy", "rx", "ry"],
  line: ["x1", "y1", "x2", "y2", ...MARKER_REFS],
  polyline: ["points", ...MARKER_REFS],
  polygon: ["points", ...MARKER_REFS],
  path: ["d", ...MARKER_REFS],
  text: ["x", "y", "dx", "dy", "text-anchor", "dominant-baseline"],
  tspan: ["x", "y", "dx", "dy", "text-anchor", "dominant-baseline"],
  title: [],
  desc: [],
};

const URL_REF = /^url\(#([A-Za-z_][\w.:-]*)\)$/;
const ID = /^[A-Za-z_][\w.:-]*$/;

let _seq = 0;

/** Sanitised SVG for one figure, or "" when nothing survived.
 *  `prefix` namespaces every surviving id and the `url(#…)` references
 *  that point at them. */
export function sanitizeFigureSvg(src: string, prefix: string): string {
  if (!src) return "";
  const host = DOMPurify.sanitize(src, {
    USE_PROFILES: { svg: true },
    RETURN_DOM: true,
  }) as unknown as HTMLElement;
  const root = host.firstElementChild;
  if (!root || root.tagName.toLowerCase() !== "svg") return "";
  if (!scrub(root, prefix)) return "";
  if (!root.hasAttribute("viewBox")) return "";
  return root.outerHTML;
}

/** Strip one element to the vocabulary, recursively. Returns false when
 *  the element itself does not belong, in which case the caller removes
 *  it — a disallowed element takes its subtree with it. */
function scrub(el: Element, prefix: string): boolean {
  const tag = el.tagName.toLowerCase();
  const allowed = ELEMENT_ATTRS[tag];
  if (!allowed) return false;
  const keep = new Set([...GLOBAL_ATTRS, ...allowed]);
  for (const name of [...el.getAttributeNames()]) {
    if (!keep.has(name)) {
      el.removeAttribute(name);
      continue;
    }
    const value = cleanValue(name, el.getAttribute(name) || "", tag, prefix);
    if (value === null) el.removeAttribute(name);
    else if (value !== el.getAttribute(name)) el.setAttribute(name, value);
  }
  for (const child of [...el.children]) {
    if (!scrub(child, prefix)) child.remove();
  }
  return true;
}

function cleanValue(name: string, value: string, tag: string, prefix: string): string | null {
  if (name === "class") {
    const kept = value.split(/\s+/).filter((c) => classElements(c).has(tag));
    return kept.length > 0 ? kept.join(" ") : null;
  }
  if (name === "id") return ID.test(value) ? `${prefix}-${value}` : null;
  if (MARKER_REFS.includes(name)) {
    const m = URL_REF.exec(value.trim());
    return m ? `url(#${prefix}-${m[1]})` : null;
  }
  return value;
}

/** The width the figure was drawn at, in viewBox user units, or null
 *  when the viewBox is not four usable numbers. Neither sanitiser
 *  pass reads past the attribute's presence, so a document that reached
 *  the browser without passing through one can carry anything here. */
function drawnWidth(root: Element): number | null {
  const parts = (root.getAttribute("viewBox") || "").trim().split(/[\s,]+/);
  if (parts.length !== 4) return null;
  const nums = parts.map(Number);
  if (!nums.every(Number.isFinite)) return null;
  return nums[2] > 0 ? nums[2] : null;
}

/** The whole `<figure>`: the diagram, its caption, and — when the
 *  sanitiser took something out — how much. A figure that lost
 *  everything is still rendered, as its alt text: dropping it silently
 *  is the failure the strip count exists to catch. */
export function renderFigure(figure: ExplainerFigure): HTMLElement {
  const fig = document.createElement("figure");
  fig.className = "explainer-figure";

  const svg = sanitizeFigureSvg(figure.svg, `scr-fig-${_seq++}`);
  if (svg) {
    const holder = document.createElement("div");
    holder.innerHTML = svg; // sanitised immediately above
    const node = holder.firstElementChild;
    if (node) {
      node.setAttribute("role", "img");
      node.setAttribute("aria-label", figure.alt);
      // Label sizes in the class vocabulary are px, which inside an SVG
      // are viewBox user units: rendering wider than the viewBox scales
      // every label with the drawing. Capping at the drawn width holds
      // one unit to at most one pixel, and the stylesheet's `width: 100%`
      // still shrinks a figure too wide for the column. Presentation the
      // renderer reads off the model's geometry, not the model's to pick.
      const width = drawnWidth(node);
      if (width !== null) (node as SVGElement).style.maxWidth = `${width}px`;
      fig.appendChild(node);
    }
  } else {
    const p = document.createElement("p");
    p.className = "explainer-figure-empty";
    p.textContent = figure.alt || "This diagram could not be rendered.";
    fig.appendChild(p);
  }

  if (figure.caption) {
    const cap = document.createElement("figcaption");
    // Inline markdown, like every other short string the model writes: a
    // caption names identifiers, and rendering it as text is how a
    // backtick reaches the page.
    renderInlineMarkdown(cap, figure.caption);
    fig.appendChild(cap);
  }
  if (figure.stripped > 0) {
    const note = document.createElement("p");
    note.className = "explainer-figure-stripped";
    const n = figure.stripped;
    note.textContent = `${n} ${n === 1 ? "element or attribute" : "elements and attributes"} removed by the figure sanitiser`;
    fig.appendChild(note);
  }
  return fig;
}

export const ExplainerFigures = { renderFigure, sanitizeFigureSvg };
