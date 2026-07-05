// Semantic Code Review — viewer boot.
//
// Loads DATA from the inline scr-data <script>, wires the modules
// together in the right order, and handles the few session-level
// pieces that don't naturally belong to any single module: the Done
// button, SSE → patch dispatch, and the per-event mutators that
// update DATA before delegating to the right module.

import { Annotations } from "./annotations";
import { Comments } from "./comments";
import { Console } from "./console";
import { DataStore, type FoldRegionAddress } from "./data_store";
import { Folds } from "./folds";
import { PostModal } from "./post_modal";
import { Progress } from "./progress";
import { Render } from "./render";
import { Sidebar } from "./sidebar";
import { Sse } from "./sse";

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
// (/exit, /comments, /events, /fold-summary). Empty string means
// "same origin" — the normal production path. The meta tag is
// absent when boot.ts is exercised outside the review server
// (jsdom tests), in which case those features are wired off.
const SESSION_ENDPOINT: string | null = (() => {
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  return m ? (m.getAttribute("content") || "") : null;
})();

// --- Boot ----------------------------------------------------------------

function boot(): void {
  Comments.init(DATA, {
    // Sidebar pills carry per-file unresolved/total counts; repaint
    // them whenever the store changes (initial load, save, delete).
    onChange: () => Sidebar.refreshFileCommentCounts(),
  });
  installDoneButton();
  Sidebar.init(DATA, {
    // Focusing a Symbols-axis pill search-highlights that symbol's name
    // across every diff line; any other pill (or none) clears it.
    onActivePillChange: (symbolName) => Render.setSymbolSearch(symbolName),
    // A filter change re-renders and reveals the focused hunks' code
    // (ephemeral focus-reveal) — driven from render.ts.
    onFilterChange: () => Render.applyFilterChange(),
  });
  Render.init(DATA);       // wires hash + keyboard + initial paint
  Progress.init(DATA);
  installPrHeader(DATA);
  installSessionEvents();
}

function bootAfterFetch(data: ViewerData): void {
  DATA = data;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}

fetch("/data.json", { cache: "no-store" })
  .then((r) => {
    if (!r.ok) throw new Error(`GET /data.json -> ${r.status}`);
    return r.json() as Promise<ViewerData>;
  })
  .then(bootAfterFetch)
  .catch((e) => {
    const app = document.getElementById("app") || document.body;
    const msg = document.createElement("div");
    msg.className = "boot-error";
    msg.textContent = `viewer failed to load: ${e}`;
    app.appendChild(msg);
  });

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

// --- Done button ---------------------------------------------------------
// Tells the review server we're finished. The server exits after this
// fires; comments accumulated via Comments have already round-tripped on
// each mutation. Single fetch, kept here rather than in comments.ts to
// avoid coupling "I'm done" to the comment storage layer.

function installDoneButton(): void {
  if (SESSION_ENDPOINT === null) return;
  const bar = document.querySelector(".pr-bar");
  if (!bar) return;
  const endpoint = SESSION_ENDPOINT;
  const btn = document.createElement("button");
  btn.className = "done-btn";
  btn.textContent = "Done";
  btn.title = "Finish review and return comments to the caller";

  // Default behaviour: POST /exit and let the server tear down. In
  // `scr pr` mode, PostModal.install swaps this for an opener that
  // pops the confirm-and-post modal first; the modal then triggers
  // /exit itself once the reviewer either posts or closes it.
  let onClick = (): void => {
    btn.disabled = true;
    btn.textContent = "Sending…";
    fetch(`${endpoint}/exit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .catch(() => { /* server may exit before responding */ })
      .finally(() => { btn.textContent = "Done ✓"; });
  };

  btn.addEventListener("click", () => onClick());
  bar.appendChild(btn);

  // Fire-and-forget: the modal infra is best-effort. If /post-config
  // fails or the server isn't in posting mode, the default exit
  // handler stays in place.
  PostModal.install(endpoint).then((result) => {
    if (result.onDoneClick) {
      onClick = result.onDoneClick;
      btn.title = "Review what will be posted before sending to GitHub";
    }
  }).catch((e) => {
    console.warn("post modal: install failed, keeping default Done", e);
  });
}

// --- SSE wiring ----------------------------------------------------------
// Sse.connect owns the EventSource subscription + JSON-parse dispatch.
// boot.ts's handlers patch the in-memory DATA + delegate the visible
// side-effects to the right module.

function installSessionEvents(): void {
  if (SESSION_ENDPOINT === null) return;
  // The console is a live-session feature (it talks to /console/ask on
  // the review server); mount it only when a session endpoint exists.
  // The console asker is wired server-side only when augmentation
  // completes; a page that booted mid-augment (DATA.pending) keeps the
  // input disabled until the augment-complete `done` event below.
  Console.init(SESSION_ENDPOINT, { ready: !DATA.pending });
  Sse.connect(SESSION_ENDPOINT, {
    overviewStart: () => Progress.setOverviewState("running"),
    overviewFailed: () => Progress.setOverviewState("failed"),
    overview: (payload) => {
      Progress.setOverviewState("ok");
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
    },
    foldSummary: (payload) => applyFoldSummary(payload),
    // Console stream (Slice 2): the worker fans deltas/tool-activity
    // out here; Console filters by its own console_id and ignores the
    // rest. The single EventSource is shared with the augment events.
    consoleDelta: (payload) => Console.onDelta(payload),
    consoleTool: (payload) => Console.onTool(payload),
    consoleDone: (payload) => Console.onDone(payload),
    consoleError: (payload) => Console.onError(payload),
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
  // Cross-tab path: the resolved region tells us which hunk hosts it
  // so we can replace just that hunk's DOM, then re-attach folds over
  // the freshly-rendered rows.
  const resolved = DataStore.findFoldRegion(DATA, addr);
  if (!resolved) return;
  Render.renderHunkReplace(resolved.file, resolved.hostHunkIdx);
  const fileEl = document.querySelector(
    '.file[data-id="' + _cssEscape(resolved.file.id) + '"]',
  ) as HTMLElement | null;
  if (fileEl) Folds.attachFileFolds(fileEl, resolved.file);
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
