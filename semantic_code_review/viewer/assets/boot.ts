// Semantic Code Review — viewer boot.
//
// Loads DATA from the inline scr-data <script>, wires the modules
// together in the right order, and handles the few session-level
// pieces that don't naturally belong to any single module: the Done
// button, SSE → patch dispatch, and the per-event mutators that
// update DATA before delegating to the right window.ScrX module.
//
// Most of the actual work lives in TS modules:
//   annotations.ts → window.ScrAnnotations (rows + arrows)
//   progress.ts    → window.ScrProgress    (header strip)
//   sse.ts         → window.ScrSse         (EventSource dispatch)
//   sidebar.ts     → window.ScrSidebar     (themes + files axes)
//   folds.ts       → window.ScrFolds       (file-level fold detection)
//   comments.ts    → window.ScrComments    (reviewer comments)
//   render.ts      → window.ScrRender      (renderers + fold state)

// `module: "none"` — the IIFE keeps this module's private helpers
// (applyOverviewPatch, applyHunkPatch, …) from colliding with the
// other Scr* modules at runtime.

(() => {

interface ScrAnnotationsFacade {
  attach(opts: {
    anchor: HTMLElement;
    shadowAnchor?: HTMLElement | null;
    variant: string;
    content: Node | string;
    onInsert?: (el: HTMLElement) => void;
  }): unknown;
  detach(row: HTMLElement): void;
  reflow(anchor: HTMLElement): void;
  reflowAll(): void;
  watchViewport(): void;
}

interface ScrProgressFacade {
  init(data: ViewerData): void;
  setHunkState(hunkId: string, state: "queued" | "running" | "ok" | "failed"): void;
  setOverviewState(state: "pending" | "running" | "ok" | "failed"): void;
  getHunkState(hunkId: string): string | undefined;
  finalise(): void;
}

interface ScrSidebarFacade {
  init(data: ViewerData): void;
  render(): void;
  refreshThemes(groups: GroupBlock[]): void;
  rebuildFilesAxis(): void;
  setActivePill(pill: { axis: string; id: string } | null): void;
  applyFilter(): void;
}

interface ScrFoldsFacade {
  attachFileFolds(fileEl: HTMLElement, file: FileBlock): void;
}

interface ScrCommentsFacade {
  init(data: ViewerData): void;
  renderAll(): void;
}

interface ScrRenderFacade {
  init(data: ViewerData): void;
  render(): void;
  renderHunkReplace(file: FileBlock, hunkIdx: number): void;
  repaintHunkHeader(hunkId: string): void;
  clearRenderedDiffCache(hunkId: string): void;
}

interface ScrSseFacade {
  connect(endpoint: string, handlers: {
    overviewStart?: () => void;
    overviewFailed?: () => void;
    overview?: (payload: SseOverviewEvent) => void;
    hunkStart?: (payload: SseHunkStartEvent) => void;
    hunk?: (payload: SseHunkEvent) => void;
    foldSummary?: (payload: SseFoldSummaryEvent) => void;
    done?: (payload: SseDoneEvent) => void;
  }): EventSource | null;
}

const _win = window as unknown as {
  ScrAnnotations: ScrAnnotationsFacade;
  ScrProgress: ScrProgressFacade;
  ScrSidebar: ScrSidebarFacade;
  ScrFolds: ScrFoldsFacade;
  ScrComments: ScrCommentsFacade;
  ScrRender: ScrRenderFacade;
  ScrSse: ScrSseFacade;
  CSS?: { escape?: (s: string) => string };
};

const _dataScript = document.getElementById("scr-data");
if (!_dataScript || !_dataScript.textContent) {
  throw new Error("scr-data script tag missing — viewer cannot boot");
}
const DATA: ViewerData = JSON.parse(_dataScript.textContent);

// DATA.pending is true while the server is streaming overview /
// per-hunk events from a running augmentation pass. Hunks without
// an annotation render an "analysing…" spinner during that window
// and the failure copy once the `done` event clears the flag —
// see installSessionEvents below + ScrRender's renderHunkHeader.

const SESSION_ENDPOINT: string = (() => {
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  return m ? (m.getAttribute("content") || "") : "";
})();

// --- Module init order ----------------------------------------------------
// Sidebar's axes are computed from DATA at module init; comments need
// DATA for the localStorage key; render needs DATA to drive the initial
// paint + wire its hash/keyboard/button handlers; progress wants DATA
// to know how many hunks the strip will display.
_win.ScrSidebar.init(DATA);

// --- Boot ----------------------------------------------------------------

function boot(): void {
  _win.ScrComments.init(DATA);     // wires gutter + loads existing
  installDoneButton();
  _win.ScrRender.init(DATA);       // wires hash + keyboard + initial paint
  _win.ScrProgress.init(DATA);
  installSessionEvents();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

// --- Done button ---------------------------------------------------------
// Tells the review server we're finished. The server exits after this
// fires; comments accumulated via window.ScrComments have already
// round-tripped on each mutation. Single fetch, kept here rather than
// in comments.ts to avoid coupling "I'm done" to the comment storage
// layer.

function installDoneButton(): void {
  if (!SESSION_ENDPOINT) return;
  const bar = document.querySelector(".pr-bar");
  if (!bar) return;
  const btn = document.createElement("button");
  btn.className = "done-btn";
  btn.textContent = "Done";
  btn.title = "Finish review and return comments to the caller";
  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "Sending…";
    fetch(`${SESSION_ENDPOINT}/exit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .catch(() => { /* server may exit before responding */ })
      .finally(() => { btn.textContent = "Done ✓"; });
  });
  bar.appendChild(btn);
}

// --- SSE wiring ----------------------------------------------------------
// sse.ts owns the EventSource subscription + JSON-parse dispatch.
// boot.ts's handlers patch the in-memory DATA + delegate the visible
// side-effects to the right module.

function installSessionEvents(): void {
  if (!SESSION_ENDPOINT) return;
  const Progress = _win.ScrProgress;
  _win.ScrSse.connect(SESSION_ENDPOINT, {
    overviewStart: () => Progress.setOverviewState("running"),
    overviewFailed: () => Progress.setOverviewState("failed"),
    overview: (payload) => {
      Progress.setOverviewState("ok");
      applyOverviewPatch(payload);
    },
    hunkStart: (payload) => {
      const hunkId = `H${payload.file_idx}_${payload.hunk_idx}`;
      Progress.setHunkState(hunkId, "running");
      _win.ScrRender.repaintHunkHeader(hunkId);
    },
    hunk: (payload) => {
      Progress.setHunkState(
        `H${payload.file_idx}_${payload.hunk_idx}`,
        payload.ok ? "ok" : "failed",
      );
      applyHunkPatch(payload);
    },
    done: () => finaliseStreaming(),
    foldSummary: (payload) => applyFoldSummary(payload),
  });
}

// --- SSE → DATA patchers ------------------------------------------------

function applyOverviewPatch(payload: SseOverviewEvent): void {
  if (payload.pr) Object.assign(DATA.pr || (DATA.pr = {} as PRBlock), payload.pr);
  if (Array.isArray(payload.files)) {
    for (const fp of payload.files) {
      const f = DATA.files && DATA.files[fp.file_idx];
      if (!f) continue;
      if (fp.summary !== undefined) f.summary = fp.summary;
      if (fp.language) f.language = fp.language;
      if (fp.symbols) f.symbols = fp.symbols;
      if (fp.status) f.status = fp.status;
    }
  }
  if (Array.isArray(payload.groups)) {
    // Themes axis lives in sidebar.ts; refreshThemes mutates in
    // place. Keep DATA.groups in sync for any consumer that still
    // reads it directly.
    _win.ScrSidebar.refreshThemes(payload.groups);
    DATA.groups = payload.groups;
  }
  // PR header + sidebar live outside the hunk list and are cheap
  // to redraw; one full re-render keeps the logic consistent with
  // the initial-paint path.
  _win.ScrRender.render();
}

// Tracks the slice 5 `_failed` marker on hunks whose augmentation
// returned an error — used by the renderer to distinguish "spinner
// pending" from "model couldn't produce annotations". Not part of
// the wire shape; viewer-side only.
interface HunkBlockMutable extends HunkBlock { _failed?: boolean; }

function applyHunkPatch(payload: SseHunkEvent): void {
  const fi = payload.file_idx;
  const hi = payload.hunk_idx;
  if (!DATA.files || !DATA.files[fi]) return;
  const file = DATA.files[fi];
  if (!file.hunks || !file.hunks[hi]) return;
  if (payload.ok && payload.block) {
    file.hunks[hi] = payload.block;
  } else {
    // Failure: mark the slot so renderHunkHeader shows the re-run
    // copy instead of the pending spinner.
    file.hunks[hi].intent = "";
    (file.hunks[hi] as HunkBlockMutable)._failed = true;
  }
  _win.ScrRender.renderHunkReplace(file, hi);
}

// FoldRegion gains a transient `_inflight` flag while a local POST
// is in flight (set in folds.ts). The viewer-side patcher honours
// it to avoid stomping the in-flight fetch handler's DOM update.
interface FoldRegionMutable extends FoldRegion { _inflight?: boolean; }

function applyFoldSummary(payload: SseFoldSummaryEvent): void {
  if (!payload || payload.summary == null) return;
  if (payload.file_idx == null) return;
  const f = DATA.files && DATA.files[payload.file_idx];
  if (!f) return;
  const ctx = payload.context || "right";
  const rs = payload.right_start || 0, re_ = payload.right_end || 0;
  const ls = payload.left_start || 0, le = payload.left_end || 0;
  // Regions live on individual hunks but are addressed at the file
  // level; walk every hunk's fold_regions for the matching key.
  let region: FoldRegionMutable | null = null;
  let hostHunk: HunkBlock | null = null;
  let hostHunkIdx = -1;
  for (let hi = 0; hi < (f.hunks || []).length; hi++) {
    const h = f.hunks[hi];
    for (const r of h.fold_regions || []) {
      if (
        (r.context || "right") === ctx
        && (r.right_start || 0) === rs && (r.right_end || 0) === re_
        && (r.left_start || 0) === ls && (r.left_end || 0) === le
      ) {
        region = r; hostHunk = h; hostHunkIdx = hi; break;
      }
    }
    if (region) break;
  }
  if (!region || !hostHunk) return;
  // Idempotency: same payload, no work. Avoids a redundant re-render
  // (and the fold popping back open) when our own POST also arrives
  // via the SSE broadcast loop.
  if (region.summary === payload.summary) return;
  region.summary = payload.summary;
  // If a local POST is in flight, the fetch handler will update the
  // fold box in place; re-rendering here would rebuild the hunk and
  // pop the user's just-closed fold back open. Let the local path
  // own the DOM update.
  if (region._inflight) return;
  // Cross-tab path: replace the hunk DOM, then re-attach the
  // file-level fold pass over the freshly-rendered rows.
  _win.ScrRender.renderHunkReplace(f, hostHunkIdx);
  const fileEl = document.querySelector(
    '.file[data-id="' + _cssEscape(f.id) + '"]',
  ) as HTMLElement | null;
  if (fileEl) _win.ScrFolds.attachFileFolds(fileEl, f);
}

function finaliseStreaming(): void {
  // Drop the pending flag so any hunks the server never sent an
  // event for (filtered, skipped, crashed mid-pass) render the
  // failure copy on the next re-render instead of the spinner.
  DATA.pending = false;
  // Hide the progress strip — only useful while streaming.
  _win.ScrProgress.finalise();
  _win.ScrRender.render();
}

// Minimal CSS.escape polyfill — only needed because some older
// browsers ship without `CSS.escape`. File and hunk ids are simple
// ASCII identifiers, so escaping is a defensive measure.
function _cssEscape(s: string): string {
  if (_win.CSS && typeof _win.CSS.escape === "function") return _win.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}

})();
