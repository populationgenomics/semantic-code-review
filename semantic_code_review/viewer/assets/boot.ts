// Semantic Code Review — viewer boot.
//
// Loads DATA from /data.json, wires the modules together in the right
// order, and handles the few session-level pieces that don't naturally
// belong to any single module: the counterpart's surface (the send bar,
// shaped by whether Claude or GitHub is the counterpart), SSE → patch
// dispatch, and the per-event mutators that update DATA before
// delegating to the right module.

import { Annotations } from "./annotations";
import { Comments } from "./comments";
import { Console } from "./console";
import { DataStore, type FoldRegionAddress } from "./data_store";
import { DebugDrawer } from "./debug_drawer";
import { Explainer } from "./explainer";
import { FileTextCache } from "./file_text";
import { LayoutDividers } from "./layout_dividers";
import { Prefs } from "./prefs";
import { Progress } from "./progress";
import { Render } from "./render";
import { SendBar } from "./send_bar";
import { Sidebar } from "./sidebar";
import { Sse } from "./sse";
import { ViewState } from "./view_state";

// Keep an unused import to ensure annotations.ts's window-attach side
// effects (if any are added later) execute. Type checker sees Annotations
// as used via boot's other callers too.
void Annotations;

// DATA is fetched from /data.json once the DOM is ready, then the
// modules are wired up. DATA.pending is true while the server is
// streaming overview / per-hunk events from a running augmentation
// pass; hunks without an annotation render an "analysing…" spinner
// during that window and the failure copy once the `done` event
// clears the flag — see installSessionEvents below + Render's
// renderHunkHeader.

let DATA!: ViewerData;

// SESSION_ENDPOINT is the prefix prepended to back-channel routes
// (/comments, /events, /fold-summary, /submit). Empty string means
// "same origin" — the normal production path. The review server
// always injects this meta tag; a missing tag is a broken shell, so
// fail loud rather than silently wiring the back-channel off.
const SESSION_ENDPOINT: string = (() => {
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  if (!m) throw new Error("scr-session-endpoint meta tag missing");
  return m.getAttribute("content") || "";
})();

// --- Boot ----------------------------------------------------------------

async function boot(): Promise<void> {
  // The reader's cross-run preferences, before anything that reads one:
  // the sidebar divider takes its width from them and Render.init the
  // gutter's fold, and both are on the first paint.
  await Prefs.load(SESSION_ENDPOINT);
  // The tab's record for this run, before the modules that read a part
  // of it: the sidebar its pill, the explainer its section, the renderer
  // its reveals and folds.
  if (typeof DATA.run_id !== "string" || DATA.run_id === "") {
    throw new Error("/data.json carries no run_id");
  }
  if (DATA.counterpart !== "claude" && DATA.counterpart !== "github") {
    throw new Error("/data.json carries no counterpart");
  }
  ViewState.init(DATA.run_id);
  Comments.init({
    counterpart: DATA.counterpart,
    // Whenever the store changes (initial load, save, delete, promotion,
    // Send, a frame from the server): the sidebar pills' per-file counts,
    // the manifests hidden content carries — a collapsed hunk's or
    // file's, a fold box's tree — and the send bar's draft count.
    onChange: () => {
      Sidebar.refreshFileCommentCounts();
      Render.refreshCommentManifests();
      SendBar.refresh();
    },
  });
  installCounterpartSurface(DATA);
  // The sidebar's edge is the reader's in both modes, so its divider
  // belongs to the shell rather than to either pane's renderer.
  LayoutDividers.installSidebar();
  Sidebar.init(DATA, {
    // Focusing a Symbols-axis pill search-highlights that symbol's name
    // across every diff line; any other pill (or none) clears it.
    onActivePillChange: (symbolName) => Render.setSymbolSearch(symbolName),
    // A pill click is a focus: the pill's hunks render open to their code
    // until the slider is touched (ADR 0008) — render.ts owns the state.
    onFilterChange: () => Render.applyFilterChange(),
  });
  // The lazy /file-text cache only needs the endpoint.
  FileTextCache.init(SESSION_ENDPOINT);
  // The change explainer only mounts when the server says the feature
  // is on for this review; a --no-augment run has no backend to run it.
  if (DATA.explainer) {
    Explainer.setFiles(DATA);
    Explainer.init(SESSION_ENDPOINT, DATA, {
      onChange: () => Render.render(),
      // A reference opens the file it addresses beside the document, so
      // the reader checks it without losing their place in the prose.
      // The panel's own "Open in diff" is the way on to the full ladder.
      onOpenFile: (fileId) => Render.openReference({ kind: "file", id: fileId }),
      // A hunk reference is a claim about specific lines, so the panel
      // unfolds that hunk rather than only showing the file.
      onOpenHunk: (hunkId) => Render.openReference({ kind: "hunk", id: hunkId }),
    });
    // Pick up a document another tab (or an earlier session on this run
    // dir) already paid for, so the button opens it rather than
    // offering to generate a second one. Awaited, because whether one
    // exists is what decides the mode the viewer opens in: resolving it
    // after the first paint would show the diff and then take it away.
    await Explainer.load();
  }
  Render.init(DATA);       // wires hash + keyboard + initial paint
  Progress.init(DATA);
  installPrHeader(DATA);
  installSessionEvents();
}

function bootAfterFetch(data: ViewerData): void {
  DATA = data;
  const start = (): void => { boot().catch(showBootError); };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
}

/** Put a boot failure on the page. `boot` is async, so a throw past its
 *  first `await` is a rejection nothing else would show. */
function showBootError(e: unknown): void {
  const app = document.getElementById("app") || document.body;
  const msg = document.createElement("div");
  msg.className = "boot-error";
  msg.textContent = `viewer failed to load: ${e}`;
  app.appendChild(msg);
}

fetch("/data.json", { cache: "no-store" })
  .then((r) => {
    if (!r.ok) throw new Error(`GET /data.json -> ${r.status}`);
    return r.json() as Promise<ViewerData>;
  })
  .then(bootAfterFetch)
  .catch(showBootError);

function installPrHeader(data: ViewerData): void {
  const pr = data.pr || {} as PRBlock;
  const title = pr.title || "(untitled PR)";
  document.title = title;
  const titleEl = document.querySelector(".pr-title") as HTMLElement | null;
  const metaEl = document.querySelector(".pr-title .pr-meta") as HTMLElement | null;
  if (titleEl) {
    // Title text sits before the existing .pr-meta span; insert as
    // a text node ahead of metaEl so we don't blow the span away.
    const txt = document.createTextNode(title + " ");
    if (metaEl) titleEl.insertBefore(txt, metaEl);
    else titleEl.appendChild(txt);
  }
  if (metaEl) {
    const bits: string[] = [];
    if (pr.repo) bits.push(pr.repo);
    if (pr.number != null) bits.push(`#${pr.number}`);
    const base = (pr.base_sha || "").slice(0, 8);
    const head = (pr.head_sha || "").slice(0, 8);
    if (base && head) bits.push(`${base}..${head}`);
    metaEl.textContent = bits.join(" · ");
  }
}

// --- The counterpart's surface ------------------------------------------
// One fixed place in the `.pr-bar`, decided by who the comments are for
// (ADR 0009). Claude: Send all drafts and the listening indicator.
// GitHub: Send all drafts, the pending review's state and Submit. In
// neither is there a Done: the session ends when the tab has been gone
// for the idle period.

function installCounterpartSurface(data: ViewerData): void {
  const bar = document.querySelector(".pr-bar");
  if (!bar) return;
  if (data.counterpart === "github" && !data.pending_review) {
    throw new Error("/data.json carries no pending_review for the GitHub counterpart");
  }
  SendBar.install(bar, {
    counterpart: data.counterpart,
    listening: data.listening,
    pendingReview: data.pending_review ?? null,
    draftCount: () => Comments.draftCount(),
    sendAll: () => Comments.sendAll(),
    retry: () => Comments.retry(),
    reconcile: () => Comments.reconcile(),
    submit: (event, body) => Comments.submit(event, body),
  });
}

// --- SSE wiring ----------------------------------------------------------
// Sse.connect owns the EventSource subscription + JSON-parse dispatch.
// boot.ts's handlers patch the in-memory DATA + delegate the visible
// side-effects to the right module.

function installSessionEvents(): void {
  // The console asker is wired server-side only when augmentation
  // completes; a page that booted mid-augment (DATA.pending) keeps the
  // input disabled until the augment-complete `done` event below.
  Console.init(SESSION_ENDPOINT, { ready: !DATA.pending });
  // Debug drawer: gated on the server's --debug flag. Mounted before the
  // SSE wiring so the buffered `debug-log` replay lands in it.
  if (DATA.debug) DebugDrawer.init();
  Sse.connect(SESSION_ENDPOINT, {
    overviewStart: () => Progress.setOverviewState("running"),
    overviewFailed: () => Progress.setOverviewState("failed"),
    overview: (payload) => {
      Progress.setOverviewState("ok");
      // The skeleton is seeded with the overview and the symbol delta,
      // both of which are now on disk — the button can be pressed.
      Render.markExplainerReady();
      applyOverviewPatch(payload);
    },
    hunkStart: (payload) => {
      const hunkId = `H${payload.file_idx}_${payload.hunk_idx}`;
      Progress.setHunkState(hunkId, "running");
      Render.repaintHunkHeader(hunkId);
    },
    hunk: (payload) => {
      Progress.setHunkState(
        `H${payload.file_idx}_${payload.hunk_idx}`,
        payload.ok ? "ok" : "failed",
      );
      applyHunkPatch(payload);
    },
    done: () => {
      finaliseStreaming();
      // Augmentation is complete: the server has now installed the
      // console asker, so unlock the prompt.
      Console.markReady();
      // Backstop for a page that missed the `overview` frame (a failed
      // overview pass, or a tab that connected after it was replayed).
      Render.markExplainerReady();
    },
    foldSummary: (payload) => applyFoldSummary(payload),
    // Another tab pressed the button; adopt what it paid for.
    explainer: (payload) => Explainer.onEvent(payload),
    // Console stream (Slice 2): the worker fans deltas/tool-activity
    // out here; Console filters by its own console_id and ignores the
    // rest. The single EventSource is shared with the augment events.
    consoleDelta: (payload) => Console.onDelta(payload),
    consoleTool: (payload) => Console.onTool(payload),
    consoleDone: (payload) => Console.onDone(payload),
    consoleError: (payload) => Console.onError(payload),
    // Only fires when the server is in --debug mode (it emits no
    // `debug-log` frames otherwise); the drawer is mounted above.
    debugLog: (payload) => DebugDrawer.onLog(payload),
    // The comment lifecycle (ADR 0009): every store change the session
    // makes — another tab's edit, a Send landing as delivered, Claude's
    // reply, a withdrawal, a Submit turning comments upstream — and
    // whether a `--wait` is attached.
    comment: (payload) => Comments.onRemote(payload),
    commentDeleted: (payload) => Comments.onRemoved(payload.id),
    listening: (payload) => SendBar.setListening(payload.listening),
    // PR mode: what GitHub does not hold as it stands, and the review
    // once submitted.
    pendingReview: (payload) => SendBar.setPendingReview(payload),
  });
}

// --- SSE → DATA patchers ------------------------------------------------
// Each handler is a three-step shape: ask DataStore to mutate, then
// hand the right view back to the right module to repaint. Mutation
// logic itself lives in data_store.ts.

function applyOverviewPatch(payload: SseOverviewEvent): void {
  const { groupsChanged } = DataStore.applyOverview(DATA, payload);
  if (groupsChanged && payload.groups) {
    // The themes axis is a sidebar concern; the DataStore wrote
    // DATA.groups, but the rendered axis lives in module-private
    // state we have to nudge separately.
    Sidebar.refreshThemes(payload.groups);
  }
  // PR header + sidebar live outside the hunk list and are cheap
  // to redraw; one full re-render keeps the logic consistent with
  // the initial-paint path.
  Render.render();
}

function applyHunkPatch(payload: SseHunkEvent): void {
  const file = (payload.ok && payload.block)
    ? DataStore.replaceHunk(DATA, payload.file_idx, payload.hunk_idx, payload.block)
    : DataStore.markHunkFailed(DATA, payload.file_idx, payload.hunk_idx);
  if (file) Render.renderHunkReplace(file, payload.hunk_idx);
}

function applyFoldSummary(payload: SseFoldSummaryEvent): void {
  if (!payload || payload.summary == null || payload.file_idx == null) return;
  const addr: FoldRegionAddress = {
    file_idx: payload.file_idx,
    context: payload.context || "right",
    right_start: payload.right_start || 0,
    right_end: payload.right_end || 0,
    left_start: payload.left_start || 0,
    left_end: payload.left_end || 0,
  };
  const outcome = DataStore.applyFoldSummary(DATA, addr, payload.summary);
  if (outcome !== "applied") return;
  // Cross-tab path: the summary is on the region object now; re-attach
  // the fold chrome on every rendered copy of the file (the diff pane's
  // and, if open, the explainer panel's) so the fold box shows it. The
  // rows keep their state, so a fold the reviewer closed stays closed.
  const resolved = DataStore.findFoldRegion(DATA, addr);
  if (!resolved) return;
  const fileEls = document.querySelectorAll(
    '.file[data-id="' + _cssEscape(resolved.file.id) + '"]',
  );
  for (const fileEl of Array.from(fileEls) as HTMLElement[]) {
    Render.attachFileFolds(fileEl, resolved.file);
  }
}

function finaliseStreaming(): void {
  DataStore.finalisePending(DATA);
  // Hide the progress strip — only useful while streaming.
  Progress.finalise();
  Render.render();
}

// Minimal CSS.escape polyfill — only needed because some older
// browsers ship without `CSS.escape`. File and hunk ids are simple
// ASCII identifiers, so escaping is a defensive measure.
function _cssEscape(s: string): string {
  const w = window as unknown as { CSS?: { escape?: (s: string) => string } };
  if (w.CSS && typeof w.CSS.escape === "function") return w.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}
