"use strict";
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
// SVG geometry constants for the L-shaped arrow. vLineX is the x-coord
// of the vertical segment in SVG space; tipX is where the arrowhead
// sits horizontally; head is the chevron extent in each direction.
const SVG_NS = "http://www.w3.org/2000/svg";
const ARROW_V_LINE_X = 2;
const ARROW_TIP_X = 17;
const ARROW_HEAD = 4;
const ARROW_SVG_W = 20;
const ARROW_MIN_OVERRUN = 6;
let rectProvider = (t) => t.getBoundingClientRect();
function setRectProvider(fn) {
    rectProvider = fn ?? ((t) => t.getBoundingClientRect());
}
function resolveLayoutDefaults(variant, caller) {
    const base = {
        maxWidth: "64ch",
        maxHeight: null,
        overflow: "hidden",
        wrap: true,
    };
    // Per-variant defaults — any explicit caller value wins.
    // Fold summaries clamp to ~3 lines (13px × 1.4 × 3 + padding ≈ 5em)
    // so they stay a glanceable hint while still fitting a longer
    // sentence than the old 2-line cap allowed.
    if (variant === "fold")
        base.maxHeight = "5em";
    return { ...base, ...(caller ?? {}) };
}
function applyLayoutToBox(box, layout) {
    box.style.maxWidth = layout.maxWidth ?? "none";
    box.style.maxHeight = layout.maxHeight ?? "none";
    box.style.overflow = layout.overflow;
    box.style.whiteSpace = layout.wrap ? "" : "nowrap";
    if (!layout.wrap)
        box.style.textOverflow = "ellipsis";
}
// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
function attach(opts) {
    const variant = opts.variant ?? "";
    const column = opts.column ?? { mode: "auto" };
    const stack = opts.stack ?? { policy: "auto" };
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
    let placeholder = null;
    if (opts.shadowAnchor) {
        placeholder = document.createElement("div");
        placeholder.className = "row row-placeholder";
        insertAfter(placeholder, opts.shadowAnchor);
    }
    const state = {
        anchor: opts.anchor,
        column,
        stack,
        layout,
        placeholder,
        resizeObserver: null,
        sizeArrow: () => sizeAnnotArrow(row),
    };
    row.__scrAnnot = state;
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
    if (opts.onInsert)
        opts.onInsert(row);
    return makeHandle(row);
}
function makeHandle(row) {
    return {
        element: row,
        get placeholder() {
            return row.__scrAnnot?.placeholder ?? null;
        },
        resize() {
            const state = row.__scrAnnot;
            if (state)
                state.sizeArrow();
        },
        remove() {
            const state = row.__scrAnnot;
            if (state?.resizeObserver)
                state.resizeObserver.disconnect();
            if (state?.placeholder)
                state.placeholder.remove();
            row.remove();
            // Idempotent: clear state so a repeat call is a no-op.
            row.__scrAnnot = undefined;
        },
        setContent(body) {
            const box = row.querySelector(".annot-box");
            if (box)
                setBoxContent(box, body);
        },
    };
}
function setBoxContent(box, body) {
    // Preserve any trailing child bar (Save/Cancel, Edit/Delete) that
    // the caller added via onInsert by wiping only the "primary body"
    // children. Callers who want full control should pass an empty
    // string here and mutate `handle.element.querySelector(".annot-box")`
    // themselves.
    while (box.firstChild)
        box.removeChild(box.firstChild);
    if (typeof body === "string")
        box.textContent = body;
    else
        box.appendChild(body);
}
function reflow(anchor) {
    if (anchor)
        scheduleReflow(anchor);
}
function reflowAll() {
    document.querySelectorAll(".row-annotation").forEach((r) => {
        if (r.style.display !== "none")
            sizeAnnotArrow(r);
    });
}
let viewportWatched = false;
function watchViewport() {
    if (viewportWatched)
        return;
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
function charRectInRow(row, col) {
    const rect = charRangeRect(row, col);
    return rect;
}
// Tear down an annotation row by its DOM element — the mirror of
// `attach()` for callers that have the element but not the handle
// (e.g. walking siblings to remove a comment thread on re-render).
// No-op if the element is not an annotation or has already been detached.
function detach(row) {
    const state = row.__scrAnnot;
    if (!state)
        return;
    if (state.resizeObserver)
        state.resizeObserver.disconnect();
    if (state.placeholder)
        state.placeholder.remove();
    row.remove();
    row.__scrAnnot = undefined;
}
// ---------------------------------------------------------------------------
// Internals: reflow coalescing, sizing, measurement
// ---------------------------------------------------------------------------
const _pendingReflow = new Set();
function scheduleReflow(anchor) {
    if (_pendingReflow.size === 0) {
        requestAnimationFrame(() => {
            const anchors = [..._pendingReflow];
            _pendingReflow.clear();
            for (const a of anchors)
                resizeAnnotSiblings(a);
        });
    }
    _pendingReflow.add(anchor);
}
function resizeAnnotSiblings(anchor) {
    if (!anchor.parentNode)
        return;
    const all = anchor.parentNode.querySelectorAll(".row-annotation");
    all.forEach((r) => {
        const state = r.__scrAnnot;
        if (state && state.anchor === anchor && r.style.display !== "none") {
            sizeAnnotArrow(r);
        }
    });
}
function syncPlaceholderHeight(annotRow) {
    const state = annotRow.__scrAnnot;
    if (!state || !state.placeholder)
        return;
    const h = rectProvider(annotRow).height || annotRow.offsetHeight;
    if (h > 0)
        state.placeholder.style.height = h + "px";
}
// Size + position the SVG arrow so its top terminates at the vertical
// midline of the anchor row (visually connecting to the code line, not
// to the bottom border of the row) and its bend sits at the vertical
// midline of the annotation box.
function sizeAnnotArrow(annotRow) {
    const state = annotRow.__scrAnnot;
    if (!state)
        return;
    const box = annotRow.querySelector(".annot-box");
    const svg = annotRow.querySelector("svg.annot-arrow");
    const cell = annotRow.querySelector(".cell-annotation");
    if (!box || !svg || !cell)
        return;
    // Use the rect provider (not offsetHeight) so tests can inject
    // canned geometry without real layout.
    const boxH = rectProvider(box).height;
    if (boxH <= 0)
        return;
    const anchor = state.anchor;
    // The arrow's top should rise to the anchor row's BOTTOM edge, not
    // its midline — sourcing the arrow from the bottom of the row cell
    // keeps it clear of the code text itself and reads as "this points
    // at the line above me" rather than "this crosses into that line".
    let topOverrun = ARROW_MIN_OVERRUN;
    const cellRect = rectProvider(cell);
    const anchorRect = anchorRowRect(anchor);
    if (anchorRect) {
        topOverrun = Math.max(ARROW_MIN_OVERRUN, cellRect.top - anchorRect.bottom);
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
    const path = svg.querySelector("path");
    path.setAttribute("d", `M ${ARROW_V_LINE_X} 0 L ${ARROW_V_LINE_X} ${midY} L ${ARROW_TIP_X} ${midY} ` +
        `M ${ARROW_TIP_X - ARROW_HEAD} ${midY - ARROW_HEAD} L ${ARROW_TIP_X} ${midY} L ${ARROW_TIP_X - ARROW_HEAD} ${midY + ARROW_HEAD}`);
}
// Resolve the absolute x coordinate (in document space) where the
// vertical segment of this annotation's arrow should land, based on
// column + stack policy.
function resolveAnchorX(state, annotRow, anchor, cell) {
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
function annotationsBelow(annotRow, anchor) {
    let n = 0;
    let s = annotRow.nextSibling;
    while (s) {
        if (s.nodeType === 1) {
            const el = s;
            const state = el.__scrAnnot;
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
function groupedStackColumn(annotRow, anchor) {
    // Count distinct "runs" between this annotation and the end of its
    // anchor's annotation chain. The run this row belongs to is its
    // column index from the right.
    let runs = 0;
    let lastAnchor = null;
    let s = annotRow.nextSibling;
    while (s) {
        if (s.nodeType === 1) {
            const el = s;
            const state = el.__scrAnnot;
            if (el.classList.contains("row-annotation") && state) {
                if (state.anchor !== lastAnchor)
                    runs++;
                lastAnchor = state.anchor;
                if (state.anchor !== anchor)
                    break;
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
function anchorRowRect(anchor) {
    if (!anchor.children)
        return null;
    let top = Infinity, bottom = -Infinity, left = Infinity, right = -Infinity;
    let found = false;
    for (const child of Array.from(anchor.children)) {
        const r = rectProvider(child);
        if (r.width === 0 && r.height === 0)
            continue;
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
function charCenterAt(anchorRowEl, n) {
    const r = charRangeRect(anchorRowEl, n);
    if (!r)
        return null;
    return r.left + r.width / 2;
}
function charRangeRect(anchorRowEl, n) {
    // After the per-half restructure, each anchor row has exactly two
    // children: [lineno, content]. Content cell is always at index 1.
    const contentCell = anchorRowEl.children[1];
    if (!contentCell)
        return null;
    const code = contentCell.querySelector("code") ?? contentCell;
    const texts = [];
    const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode()))
        texts.push(node);
    const chars = [];
    let seenPrinting = false;
    for (const t of texts) {
        const s = t.nodeValue ?? "";
        for (let i = 0; i < s.length; i++) {
            if (!seenPrinting) {
                if (/\s/.test(s[i]))
                    continue;
                seenPrinting = true;
            }
            chars.push({ node: t, offset: i });
        }
    }
    if (chars.length === 0)
        return null;
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
function svgAnnotArrow() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "annot-arrow");
    svg.setAttribute("viewBox", `0 0 ${ARROW_SVG_W} 14`);
    svg.setAttribute("width", String(ARROW_SVG_W));
    svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", `M ${ARROW_V_LINE_X} 0 L ${ARROW_V_LINE_X} 9 L ${ARROW_TIP_X} 9 ` +
        `M ${ARROW_TIP_X - ARROW_HEAD} 5 L ${ARROW_TIP_X} 9 L ${ARROW_TIP_X - ARROW_HEAD} 13`);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "1.4");
    p.setAttribute("stroke-linecap", "round");
    p.setAttribute("stroke-linejoin", "round");
    svg.appendChild(p);
    return svg;
}
function insertAfter(node, ref) {
    const parent = ref.parentNode;
    if (!parent)
        return;
    if (ref.nextSibling)
        parent.insertBefore(node, ref.nextSibling);
    else
        parent.appendChild(node);
}
// ---------------------------------------------------------------------------
// Facade
// ---------------------------------------------------------------------------
// The single runtime surface: assigned to window.ScrAnnotations below
// and the only thing viewer.js reaches for. tsc's `module: "none"`
// strips no code; the file is a classic script that runs top-to-bottom
// when inlined into the viewer HTML.
const Annotations = {
    attach,
    detach,
    reflow,
    reflowAll,
    watchViewport,
    charRectInRow,
    // Test-only hook; not part of the public API contract but exposed
    // for Vitest specs that want to inject canned geometry.
    _setRectProvider: setRectProvider,
};
// Register on the global. The cast to `unknown` sidesteps the need
// for a `declare global` Window augmentation (which requires a
// module context, which tsc with `module: "none"` refuses to give us).
if (typeof window !== "undefined") {
    window.ScrAnnotations = Annotations;
}
