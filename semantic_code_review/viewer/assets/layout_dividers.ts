// Draggable boundaries between the layout's cells.
//
// Two of them. The sidebar against the content sits at `.layout` level,
// so it holds whichever renderer owns the pane; the document against the
// detail panel is the overview split's, built with the split. Both are
// the same shape — one cell's width, dragged, nudged, remembered — so
// the geometry, the clamp, the keyboard and the persistence live here
// and each caller says only what its own cell means by a width.
//
// The width belongs to the cell *left* of a divider; the cell right of
// it takes what is left over. One number per boundary describes the
// division, so there is nothing to keep in sync, and a window too narrow
// for the number clamps rather than overwrites it: the reader's choice
// survives a small window and comes back with the room for it.

import { Annotations } from "./annotations";

/** What the owner of a boundary answers about the cell it moves. */
interface DividerSpec {
  /** Modifier class beside `layout-divider`; how a caller (or a test)
   *  addresses this boundary. */
  className: string;
  /** What the separator announces, and its tooltip's subject. */
  label: string;
  /** localStorage key holding the reader's width. Not per-run like the
   *  pill and section keys: a comfortable column is a property of the
   *  monitor, not of the diff being read on it. */
  storageKey: string;
  /** The range the cell's width may take, asked for per use — the window
   *  can have been resized since the last answer. */
  bounds: () => { min: number; max: number };
  /** The cell's width right now, for a drag starting from the
   *  stylesheet's division rather than from a stored number. */
  measure: () => number;
  /** Put `width` on the cell; `null` restores the stylesheet's own. */
  apply: (width: number | null) => void;
}

/** A divider on the page, plus the width the reader asked it for.
 *  `wanted` is theirs and is never rewritten by a clamp — what gets
 *  applied is `wanted` clamped into the current bounds. */
interface Live {
  el: HTMLElement;
  spec: DividerSpec;
  wanted: number | null;
}

const _live: Live[] = [];
let _resizeWatched = false;
let _reflowQueued = false;

const NUDGE = 16;
const NUDGE_COARSE = 64;
// Mirrors `.group-sidebar`'s flex basis in viewer.css only as a floor:
// below this the pill labels have nowhere to go.
const SIDEBAR_MIN = 160;

/** Build a divider for `spec`, wired and carrying the stored width. The
 *  caller inserts it between the two cells it divides. */
function create(spec: DividerSpec): HTMLElement {
  const el = document.createElement("div");
  el.className = `layout-divider ${spec.className}`;
  el.setAttribute("role", "separator");
  el.setAttribute("aria-orientation", "vertical");
  el.setAttribute("aria-label", spec.label);
  el.tabIndex = 0;
  el.title = `${spec.label} — drag or arrow-key; double-click to reset`;
  const live: Live = { el, spec, wanted: null };
  _live.push(live);
  el.addEventListener("pointerdown", (e) => _beginDrag(live, e));
  el.addEventListener("keydown", (e) => _onKey(live, e));
  el.addEventListener("dblclick", () => _reset(live));
  _watchResize();
  _put(live, _read(spec.storageKey));
  return el;
}

/** The sidebar against the content. A `.layout` child rather than a
 *  member of either mode's pane: the sidebar's edge is the reader's
 *  whether the pane holds the diff or the document. */
function installSidebar(): void {
  const sidebar = document.getElementById("group-sidebar");
  if (sidebar === null) throw new Error("layout shell missing #group-sidebar");
  const el = create({
    className: "layout-divider-sidebar",
    label: "Resize the sidebar",
    storageKey: "scr-sidebar-width",
    bounds: () => ({ min: SIDEBAR_MIN, max: window.innerWidth * 0.4 }),
    measure: () => sidebar.getBoundingClientRect().width,
    // The stylesheet's basis is the default, so the basis is what a
    // stored width overrides and what clearing it hands back.
    apply: (w) => { sidebar.style.flexBasis = w === null ? "" : `${Math.round(w)}px`; },
  });
  sidebar.insertAdjacentElement("afterend", el);
}

// --- Mechanics -----------------------------------------------------------

/** Apply `wanted` (clamped) and record it as the reader's number. */
function _put(live: Live, wanted: number | null): void {
  live.wanted = wanted;
  live.spec.apply(wanted === null ? null : _clamp(live, wanted));
  _aria(live);
  _scheduleReflow();
}

function _current(live: Live): number {
  return live.wanted === null ? live.spec.measure() : _clamp(live, live.wanted);
}

/** Hold `w` inside the cell's range. The floor wins over the ceiling
 *  where the window cannot fit both: a cell below its floor is unusable,
 *  and the row scrolls instead. */
function _clamp(live: Live, w: number): number {
  const { min, max } = live.spec.bounds();
  return Math.max(min, Math.min(max, w));
}

function _beginDrag(live: Live, e: PointerEvent): void {
  if (e.button !== 0) return;
  const startX = e.clientX;
  const startW = _current(live);
  live.el.classList.add("dragging");
  // Capture is what keeps the stream coming once the pointer outruns the
  // 8px strip, which it does on the first fast drag.
  live.el.setPointerCapture(e.pointerId);
  const move = (ev: PointerEvent): void => {
    _put(live, _clamp(live, startW + (ev.clientX - startX)));
  };
  const drop = (): void => {
    live.el.classList.remove("dragging");
    live.el.removeEventListener("pointermove", move);
    live.el.removeEventListener("pointerup", drop);
    live.el.removeEventListener("pointercancel", drop);
    _store(live);
  };
  live.el.addEventListener("pointermove", move);
  live.el.addEventListener("pointerup", drop);
  live.el.addEventListener("pointercancel", drop);
  // The gesture is a drag, not a selection of the prose it runs past.
  e.preventDefault();
}

/** Arrow keys move the boundary a line's worth, Shift a paragraph's. A
 *  keypress is a whole gesture, so it stores where a drag stores on
 *  release. */
function _onKey(live: Live, e: KeyboardEvent): void {
  const step = e.shiftKey ? NUDGE_COARSE : NUDGE;
  let delta = 0;
  if (e.key === "ArrowLeft") delta = -step;
  else if (e.key === "ArrowRight") delta = step;
  else return;
  e.preventDefault();
  _put(live, _clamp(live, _current(live) + delta));
  _store(live);
}

/** Back to the stylesheet's division, and forget the stored one — the
 *  way out of a width whose own divider has been dragged out of reach. */
function _reset(live: Live): void {
  _put(live, null);
  try {
    localStorage.removeItem(live.spec.storageKey);
  } catch (_) { /* localStorage may be unavailable */ }
}

/** Arrow geometry is measured, so every boundary move invalidates it.
 *  One pass per frame, not one per pointer event. */
function _scheduleReflow(): void {
  if (_reflowQueued) return;
  _reflowQueued = true;
  requestAnimationFrame(() => {
    _reflowQueued = false;
    Annotations.reflowAll();
  });
}

/** A narrower window clamps what is applied; the stored number stays as
 *  the reader set it, so widening restores it. */
function _watchResize(): void {
  if (_resizeWatched) return;
  _resizeWatched = true;
  window.addEventListener("resize", () => {
    for (let i = _live.length - 1; i >= 0; i--) {
      const live = _live[i];
      // A divider whose pane has been painted over is gone; its entry
      // goes with it.
      if (!live.el.isConnected) _live.splice(i, 1);
      else _put(live, live.wanted);
    }
  });
}

function _aria(live: Live): void {
  const { min, max } = live.spec.bounds();
  live.el.setAttribute("aria-valuemin", String(Math.round(min)));
  live.el.setAttribute("aria-valuemax", String(Math.round(max)));
  live.el.setAttribute("aria-valuenow", String(Math.round(_current(live))));
}

// --- Storage -------------------------------------------------------------

function _store(live: Live): void {
  if (live.wanted === null) return;
  try {
    localStorage.setItem(live.spec.storageKey, String(Math.round(live.wanted)));
  } catch (_) { /* localStorage may be unavailable */ }
}

/** The stored width, or null for none — and for a value nothing but an
 *  older version of this code could have written, which is a default
 *  rather than a page that refuses to lay out. */
function _read(key: string): number | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch (_) {
    return null;
  }
}

export const LayoutDividers = {
  installSidebar,
};
