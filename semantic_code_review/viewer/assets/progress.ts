// Augmentation progress strip — the header that streams overview /
// per-hunk state while augment_run_dir is running on the server.
//
// Owns the .scr-progress DOM tree (counter + grid of squares, one
// per hunk) exclusively. The viewer's SSE handlers call into this
// module on every overview-* / hunk-start / hunk / done event; the
// per-hunk *intent slot* repaint stays in viewer.js (that's about the
// hunk DOM, not the strip).
//
// Compiled by tsc to `progress.js`. The compiled output is
// concatenated into the viewer HTML alongside `annotations.js` by
// `render_html.py`, and must expose `window.ScrProgress` for the
// classic-script `viewer.js` to reach (no module loader in the HTML).

// Top-level declarations use no `export` keyword so tsc with
// `module: "none"` puts every top-level declaration in the shared
// global namespace, so an IIFE here keeps this module's internals
// from colliding with the other Scr* modules' private helpers
// (_el, _state, _data, _cssEscape, …). Only the final
// window.ScrProgress registration escapes.

(() => {

type ProgressHunkState = "queued" | "running" | "ok" | "failed";
type ProgressOverviewState = "pending" | "running" | "ok" | "failed";

interface ProgressState {
  byId: Record<string, ProgressHunkState>;
  order: string[];
  overview: ProgressOverviewState;
}

const _state: ProgressState = {
  byId: Object.create(null),
  order: [],
  overview: "pending",
};

function _rootElement(): HTMLElement | null {
  return document.getElementById("scr-progress");
}

/** Build the grid + counters from the initial viewer data. Hidden
 *  when `data.pending` is false — non-streaming reviews don't surface
 *  the strip. Generated / binary files are excluded from the grid
 *  (same filter the terminal meter applies). */
function init(data: ViewerData): void {
  if (!data.pending) return;
  const root = _rootElement();
  if (!root) return;
  const grid = root.querySelector(".scr-progress-grid") as HTMLElement | null;
  if (!grid) return;
  grid.innerHTML = "";
  _state.order = [];
  for (const k of Object.keys(_state.byId)) delete _state.byId[k];
  for (const f of data.files || []) {
    if (f.status === "generated" || f.status === "binary") continue;
    for (const h of f.hunks || []) {
      _state.byId[h.id] = "queued";
      _state.order.push(h.id);
      const sq = document.createElement("div");
      sq.className = "sq";
      sq.dataset.id = h.id;
      sq.dataset.state = "queued";
      sq.title = `${f.path} ${h.header || ""}`.trim();
      sq.setAttribute("role", "listitem");
      sq.addEventListener("click", () => _scrollToHunk(h.id));
      grid.appendChild(sq);
    }
  }
  const total = root.querySelector(".scr-progress-total");
  if (total) total.textContent = String(_state.order.length);
  _refreshCounters();
  root.classList.remove("hidden");
}

function setHunkState(hunkId: string, state: ProgressHunkState): void {
  if (!(hunkId in _state.byId)) return;
  _state.byId[hunkId] = state;
  const root = _rootElement();
  if (!root) return;
  const sq = root.querySelector(
    `.scr-progress-grid .sq[data-id="${_cssEscape(hunkId)}"]`,
  ) as HTMLElement | null;
  if (sq) sq.dataset.state = state;
  _refreshCounters();
}

function setOverviewState(state: ProgressOverviewState): void {
  _state.overview = state;
  const root = _rootElement();
  if (!root) return;
  const el = root.querySelector(".scr-progress-overview") as HTMLElement | null;
  if (el) el.dataset.state = state;
}

function getHunkState(hunkId: string): ProgressHunkState | undefined {
  return _state.byId[hunkId];
}

/** Hide the strip — called when the streaming `done` event arrives.
 *  The strip is only useful while augmentation is in flight; once the
 *  reviewer is reading annotations, the counter would just be noise. */
function finalise(): void {
  const root = _rootElement();
  if (root) root.classList.add("hidden");
}

function _refreshCounters(): void {
  let running = 0, queued = 0, ok = 0, failed = 0;
  for (const id of _state.order) {
    const s = _state.byId[id];
    if (s === "running") running++;
    else if (s === "ok") ok++;
    else if (s === "failed") failed++;
    else queued++;
  }
  const root = _rootElement();
  if (!root) return;
  const setText = (sel: string, n: number): void => {
    const el = root.querySelector(sel);
    if (el) el.textContent = String(n);
  };
  setText(".scr-progress-done", ok + failed);
  setText(".scr-progress-running", running);
  setText(".scr-progress-queued", queued);
  setText(".scr-progress-failed", failed);
}

function _scrollToHunk(hunkId: string): void {
  const node = document.querySelector(
    '.hunk[data-id="' + _cssEscape(hunkId) + '"]',
  );
  if (node) node.scrollIntoView({ behavior: "smooth", block: "center" });
}

// CSS.escape polyfill — hunk ids are simple ASCII (`H<fi>_<hi>`) so
// the regex-fallback is safe. Kept defensive because some older
// browsers shipped without CSS.escape.
function _cssEscape(s: string): string {
  if (window.CSS && typeof CSS.escape === "function") return CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}

// The single runtime surface, mirroring ScrAnnotations.
const Progress = {
  init,
  setHunkState,
  setOverviewState,
  getHunkState,
  finalise,
};

if (typeof window !== "undefined") {
  (window as unknown as { ScrProgress: typeof Progress }).ScrProgress = Progress;
}

})();
