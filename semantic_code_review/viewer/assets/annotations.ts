// Annotations component for the scr diff viewer.
//
// Attach arbitrary "annotation rows" beneath any anchor row: the module
// inserts a new row after the anchor, draws an L-shaped arrow whose tip
// terminates at a chosen character in the anchor row's text, and keeps
// every sibling arrow on the same anchor aligned through insertions,
// removals, textarea resizes, and viewport reflows.
//
// The module is **diff-agnostic** — it knows about anchor rows and
// optional "shadow" rows (for layout siblings that need to stay the
// same height, e.g. opposite halves of a side-by-side diff), but it has
// no idea what "old" vs "new" means. Callers decide which anchor each
// annotation targets; styling is chosen via `variant`.
//
// Compiled by tsc to `annotations.js` alongside this file. The compiled
// output is inlined into the viewer HTML by `render_html.py` and must
// expose `window.ScrAnnotations` for the classic-script `viewer.js` to
// reach (there is no module loader in the HTML).

export type ColumnMode = "auto" | "absolute" | "explicit";
export type StackPolicy = "auto" | "fixed" | "grouped";
export type OverflowMode = "hidden" | "visible";

export interface ColumnSpec {
  mode: ColumnMode;
  // For `absolute`: 0-based character index from the first printing glyph
  // on the anchor's content cell. For `explicit`: raw pixel offset from
  // the anchor content cell's left edge. Ignored for `auto`.
  value?: number;
}

export interface StackSpec {
  policy: StackPolicy;
}

export interface LayoutOptions {
  maxWidth?: string | null;          // CSS length or null for no cap
  maxHeight?: string | null;         // CSS length or null (e.g. "3.9em")
  overflow?: OverflowMode;           // applies when maxHeight clamps
  wrap?: boolean;                    // false → nowrap, true (default) → normal
}

export interface AttachOptions {
  anchor: HTMLElement;               // row to anchor the annotation under
  shadowAnchor?: HTMLElement | null; // opposite-layout row for placeholder sync
  variant?: string;                  // "fold" | "note" | "comment" | "editor" | custom
  content: Node | string;
  column?: ColumnSpec;
  stack?: StackSpec;
  layout?: LayoutOptions;
  onInsert?: (el: HTMLElement) => void;
}

export interface AnnotationHandle {
  element: HTMLElement;
  placeholder: HTMLElement | null;
  resize(): void;
  remove(): void;
  setContent(body: Node | string): void;
}

// Private element-property keys used to associate DOM nodes with their
// annotation state. Not part of the public API; callers should use the
// returned AnnotationHandle.
interface AnnotState {
  anchor: HTMLElement;
  column: ColumnSpec;
  stack: StackSpec;
  layout: Required<LayoutOptions>;
  placeholder: HTMLElement | null;
  resizeObserver: ResizeObserver | null;
  sizeArrow: () => void;
}

interface AnnotatedElement extends HTMLElement {
  __scrAnnot?: AnnotState;
}

// SVG geometry constants for the L-shaped arrow. vLineX is the x-coord
// of the vertical segment in SVG space; tipX is where the arrowhead
// sits horizontally; head is the chevron extent in each direction.
const SVG_NS = "http://www.w3.org/2000/svg";
const ARROW_V_LINE_X = 2;
const ARROW_TIP_X = 17;
const ARROW_HEAD = 4;
const ARROW_SVG_W = 20;
const ARROW_MIN_OVERRUN = 6;

// Test-only seam: swap in a custom rect provider so Vitest + jsdom can
// assert geometry math without real layout. Production code uses the
// live Element.getBoundingClientRect / Range.getBoundingClientRect via
// the default implementation.
type RectProvider = (target: Element | Range) => DOMRect;
let rectProvider: RectProvider = (t) => t.getBoundingClientRect();

export function setRectProvider(fn: RectProvider | null): void {
  rectProvider = fn ?? ((t) => t.getBoundingClientRect());
}

function resolveLayoutDefaults(variant: string | undefined, caller: LayoutOptions | undefined): Required<LayoutOptions> {
  const base: Required<LayoutOptions> = {
    maxWidth: "64ch",
    maxHeight: null,
    overflow: "hidden",
    wrap: true,
  };
  // Per-variant defaults — any explicit caller value wins.
  if (variant === "fold") base.maxHeight = "3.9em";
  return { ...base, ...(caller ?? {}) };
}

function applyLayoutToBox(box: HTMLElement, layout: Required<LayoutOptions>): void {
  box.style.maxWidth = layout.maxWidth ?? "none";
  box.style.maxHeight = layout.maxHeight ?? "none";
  box.style.overflow = layout.overflow;
  box.style.whiteSpace = layout.wrap ? "" : "nowrap";
  if (!layout.wrap) box.style.textOverflow = "ellipsis";
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

function attach(opts: AttachOptions): AnnotationHandle {
  const variant = opts.variant ?? "";
  const column: ColumnSpec = opts.column ?? { mode: "auto" };
  const stack: StackSpec = opts.stack ?? { policy: "auto" };
  const layout = resolveLayoutDefaults(variant, opts.layout);

  const row = document.createElement("div");
  row.className = "row row-annotation" + (variant ? " annot-" + variant : "");
  const cell = document.createElement("div");
  cell.className = "cell-annotation";
  cell.appendChild(svgAnnotArrow());
  const box = document.createElement("div");
  box.className = "annot-box";
  setBoxContent(box, opts.content);
  applyLayoutToBox(box, layout);
  cell.appendChild(box);
  row.appendChild(cell);

  // Insert into the DOM so .offsetHeight / getBoundingClientRect work.
  insertAfter(row, opts.anchor);

  // Shadow placeholder (opposite layout sibling needs to stay aligned).
  let placeholder: HTMLElement | null = null;
  if (opts.shadowAnchor) {
    placeholder = document.createElement("div");
    placeholder.className = "row row-placeholder";
    insertAfter(placeholder, opts.shadowAnchor);
  }

  const state: AnnotState = {
    anchor: opts.anchor,
    column,
    stack,
    layout,
    placeholder,
    resizeObserver: null,
    sizeArrow: () => sizeAnnotArrow(row),
  };
  (row as AnnotatedElement).__scrAnnot = state;

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      syncPlaceholderHeight(row);
      scheduleReflow(opts.anchor);
    });
    ro.observe(box);
    state.resizeObserver = ro;
  }

  // First-frame sync: `offsetHeight` needs layout, which hasn't happened
  // yet at construction time. Subsequent content mutations reflow via
  // the ResizeObserver.
  requestAnimationFrame(() => {
    syncPlaceholderHeight(row);
    sizeAnnotArrow(row);
  });

  if (opts.onInsert) opts.onInsert(row);

  return makeHandle(row);
}

function makeHandle(row: HTMLElement): AnnotationHandle {
  return {
    element: row,
    get placeholder(): HTMLElement | null {
      return (row as AnnotatedElement).__scrAnnot?.placeholder ?? null;
    },
    resize(): void {
      const state = (row as AnnotatedElement).__scrAnnot;
      if (state) state.sizeArrow();
    },
    remove(): void {
      const state = (row as AnnotatedElement).__scrAnnot;
      if (state?.resizeObserver) state.resizeObserver.disconnect();
      if (state?.placeholder) state.placeholder.remove();
      row.remove();
      // Idempotent: clear state so a repeat call is a no-op.
      (row as AnnotatedElement).__scrAnnot = undefined;
    },
    setContent(body: Node | string): void {
      const box = row.querySelector<HTMLElement>(".annot-box");
      if (box) setBoxContent(box, body);
    },
  };
}

function setBoxContent(box: HTMLElement, body: Node | string): void {
  // Preserve any trailing child bar (Save/Cancel, Edit/Delete) that
  // the caller added via onInsert by wiping only the "primary body"
  // children. Callers who want full control should pass an empty
  // string here and mutate `handle.element.querySelector(".annot-box")`
  // themselves.
  while (box.firstChild) box.removeChild(box.firstChild);
  if (typeof body === "string") box.textContent = body;
  else box.appendChild(body);
}

function reflow(anchor: HTMLElement | null | undefined): void {
  if (anchor) scheduleReflow(anchor);
}

function reflowAll(): void {
  document.querySelectorAll<HTMLElement>(".row-annotation").forEach((r) => {
    if (r.style.display !== "none") sizeAnnotArrow(r);
  });
}

let viewportWatched = false;
function watchViewport(): void {
  if (viewportWatched) return;
  viewportWatched = true;
  window.addEventListener("resize", reflowAll);
  // Re-measure once fonts finish loading — glyph metrics drive
  // character midpoint positions and cell heights.
  if (typeof document !== "undefined" && document.fonts) {
    document.fonts.ready.then(() => reflowAll());
  }
}

// Public geometry adapter — exposed so callers (e.g. future range
// annotations) can measure a character position without having to
// redo the text-node walk.
function charRectInRow(row: HTMLElement, col: number): DOMRect | null {
  const rect = charRangeRect(row, col);
  return rect;
}

// Tear down an annotation row by its DOM element — the mirror of
// `attach()` for callers that have the element but not the handle
// (e.g. walking siblings to remove a comment thread on re-render).
// No-op if the element is not an annotation or has already been detached.
function detach(row: HTMLElement): void {
  const state = (row as AnnotatedElement).__scrAnnot;
  if (!state) return;
  if (state.resizeObserver) state.resizeObserver.disconnect();
  if (state.placeholder) state.placeholder.remove();
  row.remove();
  (row as AnnotatedElement).__scrAnnot = undefined;
}

// ---------------------------------------------------------------------------
// Internals: reflow coalescing, sizing, measurement
// ---------------------------------------------------------------------------

const _pendingReflow = new Set<HTMLElement>();

function scheduleReflow(anchor: HTMLElement): void {
  if (_pendingReflow.size === 0) {
    requestAnimationFrame(() => {
      const anchors = [..._pendingReflow];
      _pendingReflow.clear();
      for (const a of anchors) resizeAnnotSiblings(a);
    });
  }
  _pendingReflow.add(anchor);
}

function resizeAnnotSiblings(anchor: HTMLElement): void {
  if (!anchor.parentNode) return;
  const all = anchor.parentNode.querySelectorAll<HTMLElement>(".row-annotation");
  all.forEach((r) => {
    const state = (r as AnnotatedElement).__scrAnnot;
    if (state && state.anchor === anchor && r.style.display !== "none") {
      sizeAnnotArrow(r);
    }
  });
}

function syncPlaceholderHeight(annotRow: HTMLElement): void {
  const state = (annotRow as AnnotatedElement).__scrAnnot;
  if (!state || !state.placeholder) return;
  const h = rectProvider(annotRow).height || annotRow.offsetHeight;
  if (h > 0) state.placeholder.style.height = h + "px";
}

// Size + position the SVG arrow so its top terminates at the vertical
// midline of the anchor row (visually connecting to the code line, not
// to the bottom border of the row) and its bend sits at the vertical
// midline of the annotation box.
function sizeAnnotArrow(annotRow: HTMLElement): void {
  const state = (annotRow as AnnotatedElement).__scrAnnot;
  if (!state) return;
  const box = annotRow.querySelector<HTMLElement>(".annot-box");
  const svg = annotRow.querySelector<SVGSVGElement>("svg.annot-arrow");
  const cell = annotRow.querySelector<HTMLElement>(".cell-annotation");
  if (!box || !svg || !cell) return;
  // Use the rect provider (not offsetHeight) so tests can inject
  // canned geometry without real layout.
  const boxH = rectProvider(box).height;
  if (boxH <= 0) return;

  const anchor = state.anchor;
  let topOverrun = ARROW_MIN_OVERRUN;
  const cellRect = rectProvider(cell);
  const anchorRect = anchorRowRect(anchor);
  if (anchorRect) {
    const anchorMidY = (anchorRect.top + anchorRect.bottom) / 2;
    topOverrun = Math.max(ARROW_MIN_OVERRUN, cellRect.top - anchorMidY);
  }
  const totalH = topOverrun + boxH;
  const midY = topOverrun + boxH / 2;

  svg.setAttribute("height", String(totalH));
  svg.setAttribute("width", String(ARROW_SVG_W));
  svg.setAttribute("viewBox", `0 0 ${ARROW_SVG_W} ${totalH}`);
  svg.style.marginTop = `-${topOverrun}px`;

  // Horizontal placement — driven by column + stack policy.
  const anchorX = resolveAnchorX(state, annotRow, anchor, cell);
  if (anchorX !== null) {
    const cs = window.getComputedStyle(cell);
    const padL = parseFloat(cs.paddingLeft) || 0;
    const marginL = anchorX - cellRect.left - padL - ARROW_V_LINE_X;
    svg.style.marginLeft = `${Math.max(0, marginL)}px`;
  }

  const path = svg.querySelector("path")!;
  path.setAttribute(
    "d",
    `M ${ARROW_V_LINE_X} 0 L ${ARROW_V_LINE_X} ${midY} L ${ARROW_TIP_X} ${midY} ` +
      `M ${ARROW_TIP_X - ARROW_HEAD} ${midY - ARROW_HEAD} L ${ARROW_TIP_X} ${midY} L ${ARROW_TIP_X - ARROW_HEAD} ${midY + ARROW_HEAD}`,
  );
}

// Resolve the absolute x coordinate (in document space) where the
// vertical segment of this annotation's arrow should land, based on
// column + stack policy.
function resolveAnchorX(
  state: AnnotState,
  annotRow: HTMLElement,
  anchor: HTMLElement,
  cell: HTMLElement,
): number | null {
  const { column, stack } = state;
  if (column.mode === "explicit" && typeof column.value === "number") {
    // Pixel offset from the annotation cell's content-left edge.
    const cellRect = rectProvider(cell);
    const cs = window.getComputedStyle(cell);
    const padL = parseFloat(cs.paddingLeft) || 0;
    return cellRect.left + padL + column.value;
  }
  if (column.mode === "absolute" && typeof column.value === "number") {
    return charCenterAt(anchor, column.value);
  }
  // Auto: pick a character based on stack policy.
  const col = stack.policy === "fixed" ? 0
    : stack.policy === "grouped" ? groupedStackColumn(annotRow, anchor)
    : annotationsBelow(annotRow, anchor);
  return charCenterAt(anchor, col);
}

// Count annotation rows that sit below `annotRow` in the DOM and share
// the same anchor. Used to stagger stacked arrow origins — each arrow
// shifts right by one character per annotation below it.
function annotationsBelow(annotRow: HTMLElement, anchor: HTMLElement): number {
  let n = 0;
  let s: Node | null = annotRow.nextSibling;
  while (s) {
    if (s.nodeType === 1) {
      const el = s as HTMLElement;
      const state = (el as AnnotatedElement).__scrAnnot;
      if (el.classList.contains("row-annotation")
          && el.style.display !== "none"
          && state && state.anchor === anchor) {
        n++;
      }
    }
    s = s.nextSibling;
  }
  return n;
}

// "grouped" stack policy: consecutive annotations with the same anchor
// share one column; a break (different anchor or non-annotation) shifts
// the next group one column right. Used when callers want to visually
// cluster related annotations.
function groupedStackColumn(annotRow: HTMLElement, anchor: HTMLElement): number {
  // Count distinct "runs" between this annotation and the end of its
  // anchor's annotation chain. The run this row belongs to is its
  // column index from the right.
  let runs = 0;
  let lastAnchor: HTMLElement | null = null;
  let s: Node | null = annotRow.nextSibling;
  while (s) {
    if (s.nodeType === 1) {
      const el = s as HTMLElement;
      const state = (el as AnnotatedElement).__scrAnnot;
      if (el.classList.contains("row-annotation") && state) {
        if (state.anchor !== lastAnchor) runs++;
        lastAnchor = state.anchor;
        if (state.anchor !== anchor) break;
      }
    }
    s = s.nextSibling;
  }
  return Math.max(0, runs - 1);
}

// Return the bounding rect of the anchor row in document space. Anchor
// rows use `display: contents` (or may contain empty cells with no
// layout box), so `anchor.getBoundingClientRect()` returns {0,0,0,0}
// in Chromium. Walk the cells — any non-empty child has a real rect
// whose top/bottom match the grid track — and return the union.
function anchorRowRect(anchor: HTMLElement): { top: number; bottom: number; left: number; right: number } | null {
  if (!anchor.children) return null;
  let top = Infinity, bottom = -Infinity, left = Infinity, right = -Infinity;
  let found = false;
  for (const child of Array.from(anchor.children)) {
    const r = rectProvider(child);
    if (r.width === 0 && r.height === 0) continue;
    top = Math.min(top, r.top);
    bottom = Math.max(bottom, r.bottom);
    left = Math.min(left, r.left);
    right = Math.max(right, r.right);
    found = true;
  }
  return found ? { top, bottom, left, right } : null;
}

// Horizontal pixel midpoint of the nth character after (and including)
// the first non-whitespace character on the anchor row's content cell.
// n=0 → first printing char's midpoint.
function charCenterAt(anchorRowEl: HTMLElement, n: number): number | null {
  const r = charRangeRect(anchorRowEl, n);
  if (!r) return null;
  return r.left + r.width / 2;
}

function charRangeRect(anchorRowEl: HTMLElement, n: number): DOMRect | null {
  // After the per-half restructure, each anchor row has exactly two
  // children: [lineno, content]. Content cell is always at index 1.
  const contentCell = anchorRowEl.children[1] as HTMLElement | undefined;
  if (!contentCell) return null;
  const code = contentCell.querySelector("code") ?? contentCell;
  const texts: Text[] = [];
  const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode())) texts.push(node as Text);

  const chars: Array<{ node: Text; offset: number }> = [];
  let seenPrinting = false;
  for (const t of texts) {
    const s = t.nodeValue ?? "";
    for (let i = 0; i < s.length; i++) {
      if (!seenPrinting) {
        if (/\s/.test(s[i])) continue;
        seenPrinting = true;
      }
      chars.push({ node: t, offset: i });
    }
  }
  if (chars.length === 0) return null;

  const target = chars[Math.min(n, chars.length - 1)];
  const range = document.createRange();
  range.setStart(target.node, target.offset);
  range.setEnd(target.node, target.offset + 1);
  const r = rectProvider(range);
  if (!r.width && !r.height) {
    // Zero-size range; fall back to first printing character.
    const first = chars[0];
    const r0 = document.createRange();
    r0.setStart(first.node, first.offset);
    r0.setEnd(first.node, first.offset + 1);
    return rectProvider(r0);
  }
  return r;
}

function svgAnnotArrow(): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "annot-arrow");
  svg.setAttribute("viewBox", `0 0 ${ARROW_SVG_W} 14`);
  svg.setAttribute("width", String(ARROW_SVG_W));
  svg.setAttribute("height", "14");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute(
    "d",
    `M ${ARROW_V_LINE_X} 0 L ${ARROW_V_LINE_X} 9 L ${ARROW_TIP_X} 9 ` +
      `M ${ARROW_TIP_X - ARROW_HEAD} 5 L ${ARROW_TIP_X} 9 L ${ARROW_TIP_X - ARROW_HEAD} 13`,
  );
  p.setAttribute("fill", "none");
  p.setAttribute("stroke", "currentColor");
  p.setAttribute("stroke-width", "1.4");
  p.setAttribute("stroke-linecap", "round");
  p.setAttribute("stroke-linejoin", "round");
  svg.appendChild(p);
  return svg;
}

function insertAfter(node: Node, ref: Node): void {
  const parent = ref.parentNode;
  if (!parent) return;
  if (ref.nextSibling) parent.insertBefore(node, ref.nextSibling);
  else parent.appendChild(node);
}

// ---------------------------------------------------------------------------
// Facade
// ---------------------------------------------------------------------------

export const Annotations = {
  attach,
  detach,
  reflow,
  reflowAll,
  watchViewport,
  charRectInRow,
  // Test-only hooks; not part of the public API contract but exposed
  // for Vitest specs that want to inject canned geometry.
  _setRectProvider: setRectProvider,
};

// Globals for the classic-script viewer to pick up. The tsconfig uses
// `module: "none"`, so `export` statements are type-only; these globals
// are the actual runtime surface.
declare global {
  interface Window {
    ScrAnnotations: typeof Annotations;
  }
}

(function registerGlobals(): void {
  if (typeof window !== "undefined") {
    window.ScrAnnotations = Annotations;
  }
})();

