// Vitest coverage for viewer.js — specifically the surfaces added by
// the streaming-annotation + progress-strip + lazy-fold-summary work:
//
//   - boot in pending mode wires the progress strip and per-hunk
//     intent slots show "queued"
//   - SSE event dispatch (overview / hunk-start / hunk / fold-summary /
//     done) patches the DOM and the sidebar
//   - fold-summary on first close fires POST /fold-summary and renders
//     the returned text (or the failure copy on error)
//
// The viewer is a single IIFE-wrapped bundle produced by esbuild from
// boot.ts as the entry. We mount the same DOM the static index.html
// emits, stub EventSource + fetch on the global, queue the /data.json
// response, then read viewer.js as a string and eval() it. The eval
// (rather than `import`) gives us a clean re-execution per test
// without fighting Vitest's module cache or having to wrangle dynamic
// imports.

import fs from "node:fs";
import path from "node:path";
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";
import { installTabStorage, makeStorage } from "./setup";
import { ViewState } from "../../semantic_code_review/viewer/assets/view_state";

/** The run the fixture data belongs to. The viewer keys its per-tab view
 *  state on it, so a test that pre-seeds storage has to agree. */
const RUN_ID = "test-run";

/** Boot the viewer at a collapse level, the way a reload does — through
 *  the stored record, since no view state rides in the URL any more. */
function presetCollapseLevel(level: "files" | "hunks" | "segments" | "off"): void {
  ViewState.save(RUN_ID, level, []);
}

const VIEWER_SRC = (() => {
  const bundle = path.resolve(
    process.cwd(), "semantic_code_review/viewer/assets/viewer.js",
  );
  if (!fs.existsSync(bundle)) {
    throw new Error(
      `viewer bundle missing at ${bundle}. Run \`npm run build\` first.`,
    );
  }
  return fs.readFileSync(bundle, "utf-8");
})();

// --- Stub EventSource ------------------------------------------------------
// Captures the listeners viewer.js registers; tests fire events via
// `lastEventSource().dispatch("hunk", {...})`.

interface StubEventSource {
  url: string;
  listeners: Record<string, Set<(e: MessageEvent) => void>>;
  closed: boolean;
  addEventListener(type: string, fn: (e: MessageEvent) => void): void;
  removeEventListener(type: string, fn: (e: MessageEvent) => void): void;
  close(): void;
  dispatch(type: string, data: unknown): void;
}

const eventSourceInstances: StubEventSource[] = [];

class EventSourceStub implements StubEventSource {
  url: string;
  listeners: Record<string, Set<(e: MessageEvent) => void>> = {};
  closed = false;
  constructor(url: string) {
    this.url = url;
    eventSourceInstances.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void): void {
    (this.listeners[type] ||= new Set()).add(fn);
  }
  removeEventListener(type: string, fn: (e: MessageEvent) => void): void {
    this.listeners[type]?.delete(fn);
  }
  close(): void {
    this.closed = true;
  }
  dispatch(type: string, data: unknown): void {
    const fns = this.listeners[type];
    if (!fns) return;
    const ev = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const fn of fns) fn(ev);
  }
}

function lastEventSource(): StubEventSource {
  const es = eventSourceInstances[eventSourceInstances.length - 1];
  if (!es) throw new Error("viewer.js did not open an EventSource — check session endpoint");
  return es;
}

// --- Stub fetch ------------------------------------------------------------
// Tests queue responses via `queueFetchResponse({status, body})`.
//
// `/file-text` is the exception: it is answered from `fileTexts` rather
// than the queue. The renderer asks for every file body it lays out
// (ADR 0006 slice 6 — the route is the viewer's only source of file
// content), so a queued response would be eaten by a fetch the test
// never wrote. A file no test registered is served `{base: null, head:
// null}`: the honest answer for one the route cannot serve — binary, or
// over its 2 MB per-side cap.

interface FetchResponse {
  status: number;
  body: unknown;
}
const fetchResponses: FetchResponse[] = [];
const fetchCalls: Array<{ url: string; init: RequestInit | undefined }> = [];
const fileTexts: Record<string, { base: string | null; head: string | null }> =
  Object.create(null);

function queueFetchResponse(r: FetchResponse): void {
  fetchResponses.push(r);
}

/** Give `/file-text` a body for one file. `sides` says which of them the
 *  route serves: "base" models a file whose head side is over the cap
 *  (or absent, as a deletion's is), and vice versa. Unchanged context is
 *  the same text on both sides, so one array covers both. */
function serveFileText(
  fileId: string, lines: string[], sides: "both" | "head" | "base" = "both",
): void {
  serveFileSides(
    fileId, sides === "head" ? null : lines, sides === "base" ? null : lines,
  );
}

/** The two sides separately, for a file whose pre- and post-image differ
 *  where it matters — a deletion the trailing context has to run past. */
function serveFileSides(
  fileId: string, base: string[] | null, head: string[] | null,
): void {
  fileTexts[fileId] = {
    base: base === null ? null : base.join("\n"),
    head: head === null ? null : head.join("\n"),
  };
}

/** Let a `/file-text` arrival and the repaint it coalesces into land.
 *  Two ticks: the answer settles on the first, and the repaint it
 *  schedules runs on the next. */
async function settleFileText(): Promise<void> {
  await new Promise<void>((r) => setTimeout(r, 0));
  await new Promise<void>((r) => setTimeout(r, 0));
}

// The route is a round-trip, so the first paint happens without it.
// `holdFileText` stalls every answer until `releaseFileText`, which is
// how a test looks at the viewer before its content exists.
let holdFileText = false;
const heldFileText: Array<() => void> = [];

function holdFileTextAnswers(): void {
  holdFileText = true;
}

function releaseFileText(): Promise<void> {
  holdFileText = false;
  for (const resolve of heldFileText.splice(0)) resolve();
  return settleFileText();
}

// --- Boot helper ----------------------------------------------------------

interface ViewerData {
  version?: string;
  run_id?: string;
  pending?: boolean;
  pr?: Record<string, unknown>;
  smells_catalogue?: Record<string, unknown>;
  files?: Array<Record<string, unknown>>;
  groups?: Array<Record<string, unknown>>;
}

interface BootOptions {
  /** Body the /comments fetch fired by Comments.init should resolve to.
   *  Defaults to an empty array. */
  comments?: unknown[];
  /** Wait for the `/file-text` arrivals the first render asks for, and
   *  the repaint they coalesce into. Default true; false leaves the test
   *  looking at the first paint, before any content. */
  awaitContent?: boolean;
}

async function bootViewer(data: ViewerData, opts: BootOptions = {}): Promise<void> {
  // Mount the static index.html skeleton (minus the highlight.js
  // <script> the bundle doesn't need at test time). The
  // scr-session-endpoint meta tag presence is what flips the viewer
  // into server-mediated mode; empty content means "same origin"
  // (which boot.ts then prepends to the stubbed fetch URLs).
  document.head.innerHTML = `
    <meta name="scr-session-endpoint" content="">
  `;
  document.body.innerHTML = `
    <header class="pr-bar">
      <div class="pr-title"><span class="pr-meta"></span></div>
      <div class="fold-slider">
        <button data-fold="files"></button>
        <button data-fold="hunks"></button>
        <button data-fold="segments"></button>
        <button data-fold="off"></button>
      </div>
      <button id="reset-btn"></button>
      <button id="help-btn"></button>
    </header>
    <div id="scr-progress" class="scr-progress hidden">
      <div class="scr-progress-summary">
        <span class="scr-progress-overview" data-state="pending">Overview</span>
        <span class="scr-progress-hunks">Hunks <span class="scr-progress-done">0</span>/<span class="scr-progress-total">0</span></span>
        <span class="scr-progress-detail">
          (<span class="scr-progress-running">0</span> running ·
          <span class="scr-progress-queued">0</span> queued ·
          <span class="scr-progress-failed">0</span> failed)
        </span>
      </div>
      <div class="scr-progress-grid"></div>
    </div>
    <div class="layout">
      <aside id="group-sidebar" class="group-sidebar"></aside>
      <main id="app"></main>
    </div>
    <footer id="status-bar"><span id="status-counts"></span></footer>
    <div id="help-overlay" class="help-overlay hidden"></div>
  `;
  // boot.ts fetches /data.json first thing — queue this response
  // ahead of anything the test adds so the fetch chain resolves to
  // our data before Comments.init fires /comments and before any
  // test-specific POST. Comments.init's /comments fetch is queued
  // immediately after so it consumes the comments response (or an
  // empty default) rather than whatever the test queues later.
  queueFetchResponse({ status: 200, body: data });
  queueFetchResponse({ status: 200, body: { comments: opts.comments ?? [] } });
  // Execute viewer.js as a fresh IIFE in the current realm so it
  // picks up our stubs. `new Function` ensures strict-mode + clean
  // scope. The IIFE returns synchronously; the boot continues on
  // microtasks once the /data.json fetch resolves.
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  new Function(VIEWER_SRC)();
  // Drain microtasks + macrotask ticks so the fetch promise chain
  // resolves, boot() runs, Comments.init's /comments fetch resolves, and
  // all sync init lands before the test asserts. The second tick is the
  // file content: the first render asks `/file-text` for every body it
  // lays out and the arrival repaints (coalesced through a timer).
  await new Promise<void>((r) => setTimeout(r, 0));
  if (opts.awaitContent !== false) await settleFileText();
}

function makeHunkBlock(id: string, intent = "", overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    header: "@@ -1,2 +1,2 @@",
    old_start: 1, old_count: 2, new_start: 1, new_count: 2,
    adds: 1, dels: 1,
    intent,
    smells: [],
    confidence: null,
    context: "",
    refs: [],
    line_notes: [],
    segments: [],
    rows: [
      { kind: "pair", old_line: 1, new_line: 1, old_text: "a", new_text: "a" },
      { kind: "pair", old_line: 2, new_line: 2, old_text: "b", new_text: "B" },
    ],
    ...overrides,
  };
}

function makeData(overrides: Partial<ViewerData> = {}): ViewerData {
  return {
    version: "1",
    run_id: RUN_ID,
    pending: true,
    pr: { title: "test", themes: [], symbols_added: [], symbols_modified: [], symbols_removed: [], callgraph_edges: [] },
    smells_catalogue: {},
    files: [{
      id: "F0",
      path: "a.py",
      status: "modified",
      language: "python",
      adds: 1, dels: 1,
      summary: "",
      symbols: { added: [], modified: [], removed: [] },
      hunks: [makeHunkBlock("H0_0")],
    }],
    groups: [],
    symbols: [],
    ...overrides,
  };
}

function fileEl(id: string): HTMLElement {
  const el = document.querySelector(`.file[data-id="${id}"]`) as HTMLElement | null;
  if (!el) throw new Error(`no .file[data-id=${id}] in the document`);
  return el;
}

function clickFileHeader(id: string): void {
  (fileEl(id).querySelector(".file-header") as HTMLElement).click();
}

// --- Global hooks ----------------------------------------------------------

beforeEach(() => {
  eventSourceInstances.length = 0;
  fetchResponses.length = 0;
  fetchCalls.length = 0;
  for (const k of Object.keys(fileTexts)) delete fileTexts[k];
  holdFileText = false;
  heldFileText.length = 0;
  // Reset persisted viewer state between tests. The viewer restores the
  // focused sidebar pill from localStorage (sidebar.ts) and the collapse
  // level plus every hidden span from sessionStorage (view_state.ts) on
  // boot; neither is cleared by wiping the DOM. Without this, a prior
  // test's focused symbol re-applies on the next boot — highlighting
  // before the test acts and leaking symbol-hit spans. node 25's timing
  // masked it; node 20's exposed it. A fresh Storage rather than
  // `.clear()` so a test that played a second tab hands the next one
  // back a first tab.
  localStorage.clear();
  installTabStorage();
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    EventSourceStub as unknown as typeof EventSource;
  vi.spyOn(globalThis, "fetch").mockImplementation(((url: string, init?: RequestInit) => {
    fetchCalls.push({ url, init });
    const fileText = /\/file-text\?file_idx=(\d+)/.exec(url);
    if (fileText) {
      const body = {
        file_idx: Number(fileText[1]), path: "",
        ...(fileTexts[`F${fileText[1]}`] ?? { base: null, head: null }),
      };
      const answer = { status: 200, ok: true, json: () => Promise.resolve(body) } as Response;
      if (!holdFileText) return Promise.resolve(answer);
      return new Promise<Response>((r) => heldFileText.push(() => r(answer)));
    }
    const next = fetchResponses.shift() ?? { status: 200, body: {} };
    return Promise.resolve({
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      json: () => Promise.resolve(next.body),
    } as Response);
  }) as typeof fetch);
});

afterEach(async () => {
  document.head.innerHTML = "";
  document.body.innerHTML = "";
  // A `/file-text` arrival repaints through a timer, and a test that
  // ended before one landed leaves it pending. Drain it here, with the
  // DOM already gone, so the previous bundle's render cannot fire into
  // the next test's document.
  await settleFileText();
});


describe("pending boot", () => {
  test("progress strip shows total + every square starts queued", async () => {
    await bootViewer(makeData({
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0"), makeHunkBlock("H0_1")],
      }],
    }));
    const strip = document.getElementById("scr-progress")!;
    expect(strip.classList.contains("hidden")).toBe(false);
    expect(strip.querySelector(".scr-progress-total")!.textContent).toBe("2");
    const squares = Array.from(strip.querySelectorAll(".scr-progress-grid .sq"));
    expect(squares).toHaveLength(2);
    expect(squares.every((sq) => sq.getAttribute("data-state") === "queued")).toBe(true);
    expect(strip.querySelector(".scr-progress-queued")!.textContent).toBe("2");
    expect(strip.querySelector(".scr-progress-done")!.textContent).toBe("0");
  });

  test("hunks with empty intent render the 'queued' placeholder", async () => {
    await bootViewer(makeData());
    const intent = document.querySelector(".hunk-intent")!;
    expect(intent.classList.contains("queued")).toBe(true);
    expect(intent.textContent).toBe("queued");
  });

  test("generated/binary files are excluded from the progress grid", async () => {
    await bootViewer(makeData({
      files: [
        {
          id: "F0", path: "uv.lock", status: "generated", language: "",
          adds: 0, dels: 0, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0"), makeHunkBlock("H0_1")],
        },
        {
          id: "F1", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H1_0")],
        },
      ],
    }));
    const strip = document.getElementById("scr-progress")!;
    expect(strip.querySelector(".scr-progress-total")!.textContent).toBe("1");
    const squares = strip.querySelectorAll(".scr-progress-grid .sq");
    expect(squares).toHaveLength(1);
    expect((squares[0] as HTMLElement).dataset.id).toBe("H1_0");
  });
});


describe("streaming events", () => {
  test("hunk-start flips the square + intent slot to 'running'", async () => {
    await bootViewer(makeData());
    const es = lastEventSource();
    es.dispatch("hunk-start", { file_idx: 0, hunk_idx: 0 });
    const square = document.querySelector('.scr-progress-grid .sq[data-id="H0_0"]')!;
    expect(square.getAttribute("data-state")).toBe("running");
    const intent = document.querySelector(".hunk-intent")!;
    expect(intent.classList.contains("pending")).toBe(true);
    expect(intent.textContent).toBe("analysing…");
    const strip = document.getElementById("scr-progress")!;
    expect(strip.querySelector(".scr-progress-running")!.textContent).toBe("1");
    expect(strip.querySelector(".scr-progress-queued")!.textContent).toBe("0");
  });

  test("hunk completion patches the intent and marks the square ok", async () => {
    await bootViewer(makeData());
    const es = lastEventSource();
    es.dispatch("hunk-start", { file_idx: 0, hunk_idx: 0 });
    es.dispatch("hunk", {
      file_idx: 0, hunk_idx: 0, ok: true,
      block: makeHunkBlock("H0_0", "bump return value from 1 to 2"),
    });
    const square = document.querySelector('.scr-progress-grid .sq[data-id="H0_0"]')!;
    expect(square.getAttribute("data-state")).toBe("ok");
    const intent = document.querySelector(".hunk-intent")!;
    expect(intent.textContent).toBe("bump return value from 1 to 2");
    expect(intent.classList.contains("pending")).toBe(false);
    expect(intent.classList.contains("queued")).toBe(false);
    const strip = document.getElementById("scr-progress")!;
    expect(strip.querySelector(".scr-progress-done")!.textContent).toBe("1");
    expect(strip.querySelector(".scr-progress-failed")!.textContent).toBe("0");
  });

  test("hunk failure marks the square failed and shows the re-run copy", async () => {
    await bootViewer(makeData());
    const es = lastEventSource();
    es.dispatch("hunk", {
      file_idx: 0, hunk_idx: 0, ok: false, error: "UsageLimitExceeded: …",
    });
    const square = document.querySelector('.scr-progress-grid .sq[data-id="H0_0"]')!;
    expect(square.getAttribute("data-state")).toBe("failed");
    const intent = document.querySelector(".hunk-intent")!;
    expect(intent.classList.contains("empty")).toBe(true);
    expect(intent.textContent).toContain("may need re-run");
  });

  test("overview event populates the themes axis and the file summary", async () => {
    await bootViewer(makeData({
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0"), makeHunkBlock("H0_1")],
      }],
    }));
    // The Files axis is structural and renders from boot — no overview
    // pass needed. The Themes axis is empty until the overview SSE
    // event lands.
    const sidebar = document.getElementById("group-sidebar")!;
    expect(sidebar.classList.contains("empty")).toBe(false);
    expect(sidebar.querySelector('[data-axis="files"]')).not.toBeNull();
    expect(sidebar.querySelector('[data-axis="themes"]')).toBeNull();

    const es = lastEventSource();
    es.dispatch("overview", {
      pr: { summary: "bumps return values", themes: ["constants"], symbols_added: [], symbols_modified: [], symbols_removed: [], callgraph_edges: [] },
      groups: [
        { id: "G0", title: "return value bumps", rationale: "two related edits", hunk_ids: ["H0_0", "H0_1"] },
      ],
      files: [{ file_idx: 0, summary: "x and y bumped", language: "python", symbols: { added: [], modified: [], removed: [] } }],
    });
    const themesSection = sidebar.querySelector('[data-axis="themes"]')!;
    expect(themesSection).not.toBeNull();
    const themeBtns = themesSection.querySelectorAll(".group-btn");
    expect(themeBtns.length).toBe(1);
    expect(themeBtns[0].textContent).toContain("return value bumps");
    expect(document.querySelector(".file-summary")!.textContent).toBe("x and y bumped");
  });

  test("by-file axis renders from boot with one pill per file and filters on click", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [
        {
          id: "F0", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0", "alpha"), makeHunkBlock("H0_1", "beta")],
        },
        {
          id: "F1", path: "b.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H1_0", "gamma")],
        },
      ],
    }));
    const sidebar = document.getElementById("group-sidebar")!;
    const filesSection = sidebar.querySelector('[data-axis="files"]')!;
    expect(filesSection).not.toBeNull();
    const pills = filesSection.querySelectorAll(".group-btn");
    expect(pills).toHaveLength(2);
    expect(pills[0].textContent).toContain("a.py");
    expect(pills[0].querySelector(".group-btn-count")!.textContent).toBe("2");
    expect(pills[1].textContent).toContain("b.py");
    expect(pills[1].querySelector(".group-btn-count")!.textContent).toBe("1");

    // Click the a.py pill — the view re-renders focused on a.py: both its
    // hunks stay live, and b.py drops out entirely (no surviving hunk).
    (pills[0] as HTMLElement).click();
    expect(document.querySelector('.hunk[data-id="H0_0"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0_1"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H1_0"]')).toBeNull();
    expect(document.querySelector('.file[data-id="F1"]')).toBeNull();
    expect(document.querySelector(".file.filtered")).not.toBeNull();
    // The pill state survives the re-render.
    expect(
      document.querySelector('.group-btn[data-axis="files"][data-pill-id="BF0"]')!
        .classList.contains("active"),
    ).toBe(true);

    // Clicking it again clears the filter — b.py comes back.
    (document.querySelector('.group-btn[data-axis="files"][data-pill-id="BF0"]') as HTMLElement).click();
    expect(document.querySelector('.hunk[data-id="H1_0"]')).not.toBeNull();
    expect(document.querySelector(".group-btn-all")!.classList.contains("active")).toBe(true);
  });

  test("by-file axis groups into a directory tree: compress, sort, and subtree filter", async () => {
    const mkFile = (id: string, p: string, hid: string): Record<string, unknown> => ({
      id, path: p, status: "modified", language: "python",
      adds: 0, dels: 0, summary: "",
      symbols: { added: [], modified: [], removed: [] },
      hunks: [makeHunkBlock(hid)],
    });
    await bootViewer(makeData({
      pending: false,
      files: [
        // src/ holds two files → an interior "src" node with two leaves.
        mkFile("F0", "src/b.py", "Hb"),
        mkFile("F1", "src/a.py", "Ha"),
        // docs/guide/ is a single-child chain → compressed to "docs/guide".
        mkFile("F2", "docs/guide/intro.md", "Hi"),
      ],
    }));
    const filesSection = document.querySelector('[data-axis="files"]')!;
    const roots = filesSection.querySelectorAll(":scope > .group-tree-node");
    // Two top-level nodes, sorted alphanumerically: "docs/guide", "src".
    const rootLabel = (n: Element): string =>
      n.querySelector(":scope > .group-tree-row > .group-btn .group-btn-label")!.textContent!;
    expect(Array.from(roots).map(rootLabel)).toEqual(["docs/guide", "src"]);

    // "docs/guide" is a compressed single-child chain: one leaf under it.
    const docsNode = roots[0];
    const docsChildren = docsNode.querySelectorAll(".group-tree-children .group-btn-label");
    expect(Array.from(docsChildren).map((e) => e.textContent)).toEqual(["intro.md"]);

    // "src" holds two leaves, sorted a.py before b.py; its count is the
    // subtree hunk union (2).
    const srcNode = roots[1];
    const srcPill = srcNode.querySelector(":scope > .group-tree-row > .group-btn") as HTMLElement;
    expect(srcPill.querySelector(".group-btn-count")!.textContent).toBe("2");
    const srcChildren = srcNode.querySelectorAll(".group-tree-children .group-btn-label");
    expect(Array.from(srcChildren).map((e) => e.textContent)).toEqual(["a.py", "b.py"]);

    // The toggle collapses the directory's children in place. (Tested
    // before the filter click, which re-renders the sidebar and would
    // detach these nodes.)
    const srcToggle = srcNode.querySelector(":scope > .group-tree-row > .group-tree-toggle") as HTMLElement;
    const srcChildWrap = srcNode.querySelector(":scope > .group-tree-children") as HTMLElement;
    expect(srcChildWrap.style.display).not.toBe("none");
    srcToggle.click();
    expect(srcChildWrap.style.display).toBe("none");
    srcToggle.click();   // re-expand so the pill is reachable again

    // Clicking the "src" directory filters to its whole subtree: the
    // view re-renders focused on the two src hunks; the docs file drops
    // out entirely.
    srcPill.click();
    expect(document.querySelector('.hunk[data-id="Ha"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="Hb"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="Hi"]')).toBeNull();
  });

  test("filtering keeps focused hunks live and folds the rest into expand chips", async () => {
    const hunkAt = (id: string, line: number, oldText: string, newText: string): Record<string, unknown> =>
      makeHunkBlock(id, "renamed", {
        old_start: line, old_count: 1, new_start: line, new_count: 1,
        rows: [{ kind: "pair", old_line: line, new_line: line, old_text: oldText, new_text: newText }],
      });
    // 9 lines with changed lines at 2, 5, 8 → context at 1, 3-4, 6-7, 9.
    serveFileText("F0", ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"]);
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 3, dels: 3, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [hunkAt("H0", 2, "a", "A"), hunkAt("H1", 5, "sdf", "fgh"), hunkAt("H2", 8, "e", "E")],
      }],
      symbols: [
        { id: "SY0", title: "first", rationale: "", hunk_ids: ["H0"] },
        { id: "SY1", title: "firstlast", rationale: "", hunk_ids: ["H0", "H2"] },
      ],
    }));

    const clickPill = (id: string): void =>
      (document.querySelector(`[data-axis="symbols"] .group-btn[data-pill-id="${id}"]`) as HTMLElement).click();

    // Unfiltered: three hunk headers render.
    expect(document.querySelectorAll(".hunk-header").length).toBe(3);

    // Focus H0 + H2. Both stay live (full hunk, with header). H1 is
    // demoted — collapsed into an expand chip between them, not rendered
    // as a hunk.
    clickPill("SY1");
    expect(document.querySelector('.hunk[data-id="H0"] .hunk-header')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H2"] .hunk-header')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H1"]')).toBeNull();
    // The two live hunks are the only hunks; the demoted region is a chip.
    expect(document.querySelectorAll(".file.filtered .hunk")).toHaveLength(2);
    expect(document.querySelectorAll(".file.filtered .gap-chip").length).toBeGreaterThan(0);
    // H1's change is hidden until its chip is expanded.
    expect(document.body.textContent).not.toContain("fgh");

    // Expand the chip that swallowed H1 → its +/- lines render inline
    // (continuous diff, no hunk header), alongside the surrounding context.
    const between = Array.from(document.querySelectorAll<HTMLElement>(".file.filtered .gap-chip"))
      .find((c) => (c.textContent || "").includes("hidden"))!;
    between.click();
    const expansion = document.querySelector(".gap-expansion")!;
    expect(expansion.textContent).toContain("fgh");   // H1's demoted change
    expect(expansion.textContent).toContain("l3");     // surrounding context
    expect(expansion.querySelector(".hunk-header")).toBeNull();

    // Narrow to a single hunk — H2 now demotes too.
    clickPill("SY0");
    expect(document.querySelector('.hunk[data-id="H0"] .hunk-header')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H2"]')).toBeNull();

    // Show all restores the normal structure: three hunk headers, gaps.
    (document.querySelector(".group-btn-all") as HTMLElement).click();
    expect(document.querySelector(".file.filtered")).toBeNull();
    expect(document.querySelectorAll(".hunk-header")).toHaveLength(3);
    expect(document.querySelectorAll(".gap-chip").length).toBeGreaterThan(0);
  });


  test("symbols axis renders flat pills from boot and filters on click", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "alpha"), makeHunkBlock("H0_1", "beta")],
      }],
      symbols: [
        { id: "SY0", title: "Foo.bar", rationale: "modified function in a.py", hunk_ids: ["H0_0"] },
        { id: "SY1", title: "baz", rationale: "added function in a.py", hunk_ids: ["H0_1"] },
      ],
    }));
    const sidebar = document.getElementById("group-sidebar")!;
    const symbolsSection = sidebar.querySelector('[data-axis="symbols"]')!;
    expect(symbolsSection).not.toBeNull();
    const pills = symbolsSection.querySelectorAll(".group-btn");
    expect(pills).toHaveLength(2);
    expect(pills[0].textContent).toContain("Foo.bar");
    expect(pills[0].querySelector(".group-btn-count")!.textContent).toBe("1");

    // Click the Foo.bar pill — the view re-renders focused on H0_0; the
    // sibling hunk H0_1 drops out.
    (pills[0] as HTMLElement).click();
    expect(document.querySelector('.hunk[data-id="H0_0"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0_1"]')).toBeNull();
    expect(
      document.querySelector('.group-btn[data-axis="symbols"][data-pill-id="SY0"]')!
        .classList.contains("active"),
    ).toBe(true);

    // The symbols axis coexists with Themes/Files (Files renders from boot).
    expect(sidebar.querySelector('[data-axis="files"]')).not.toBeNull();
  });

  test("focusing a symbol pill search-highlights its name across the diff", async () => {
    presetCollapseLevel("off"); // expand hunks so diff bodies render
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "", {
          rows: [
            // "compute" appears as a whole word twice on this ctx row
            // (old + new cell); "recompute" must NOT match (substring).
            { kind: "ctx", old_line: 1, new_line: 1, old_text: "x = compute(1)", new_text: "x = compute(1)" },
            { kind: "ins", old_line: null, new_line: 2, old_text: "", new_text: "y = recompute(2)" },
          ],
        })],
      }],
      symbols: [
        { id: "SY0", title: "compute", rationale: "modified function in a.py", hunk_ids: ["H0_0"] },
      ],
    }));

    const sidebar = document.getElementById("group-sidebar")!;
    const pill = sidebar.querySelector('[data-axis="symbols"] .group-btn') as HTMLElement;
    expect(pill.textContent).toContain("compute");

    // Nothing highlighted until a symbol is focused.
    expect(document.querySelectorAll("span.symbol-hit")).toHaveLength(0);

    pill.click();
    const hits = [...document.querySelectorAll("span.symbol-hit")];
    expect(hits.map((h) => h.textContent)).toEqual(["compute", "compute"]);

    // Clearing the filter ("Show all") removes the highlight.
    (document.querySelector(".group-btn-all") as HTMLElement).click();
    expect(document.querySelectorAll("span.symbol-hit")).toHaveLength(0);
    // ...and leaves the underlying line text intact.
    const firstCell = document.querySelector('.hunk[data-id="H0_0"] .cell-content code')!;
    expect(firstCell.textContent).toBe("x = compute(1)");
  });

  test("symbols axis nests methods under their class and filters by subtree", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [
          makeHunkBlock("H0_0", "alpha"),
          makeHunkBlock("H0_1", "beta"),
          makeHunkBlock("H0_2", "gamma"),
        ],
      }],
      // Foo (class) wraps two changed methods. Its hunk_ids is the
      // subtree union; each method carries just its own.
      symbols: [{
        id: "SY0", title: "Foo", rationale: "modified class in a.py",
        hunk_ids: ["H0_0", "H0_1"],
        children: [
          { id: "SY1", title: "bar", rationale: "modified function in a.py", hunk_ids: ["H0_0"] },
          { id: "SY2", title: "baz", rationale: "added function in a.py", hunk_ids: ["H0_1"] },
        ],
      }],
    }));
    const section = document.querySelector('[data-axis="symbols"]')!;
    expect(section).not.toBeNull();

    // Class pill + a toggle; both methods render nested and expanded.
    const classPill = section.querySelector<HTMLElement>('.group-btn[data-pill-id="SY0"]')!;
    expect(classPill).not.toBeNull();
    expect(classPill.textContent).toContain("Foo");
    expect(classPill.querySelector(".group-btn-count")!.textContent).toBe("2");
    const methodPills = section.querySelectorAll(".group-tree-children .group-btn");
    expect(Array.from(methodPills).map((p) => p.querySelector(".group-btn-label")!.textContent))
      .toEqual(["bar", "baz"]);

    // Click the class → both its methods' hunks stay live; the unrelated
    // H0_2 demotes into a fold. (Filtering re-renders, so hunk elements
    // are re-queried after each click.)
    classPill.click();
    expect(document.querySelector('.hunk[data-id="H0_0"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0_1"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0_2"]')).toBeNull();
    expect(document.querySelectorAll(".file.filtered .hunk")).toHaveLength(2);

    // Click the method → only its own hunk remains.
    document.querySelector<HTMLElement>('.group-btn[data-axis="symbols"][data-pill-id="SY1"]')!.click();
    expect(document.querySelector('.hunk[data-id="H0_0"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0_1"]')).toBeNull();
    expect(document.querySelector('.hunk[data-id="H0_2"]')).toBeNull();

    // The collapse toggle hides the children without changing the filter.
    const toggle = document.querySelector<HTMLElement>('[data-axis="symbols"] .group-tree-toggle')!;
    expect(toggle.classList.contains("group-tree-toggle-leaf")).toBe(false);
    toggle.click();
    const childWrap = document.querySelector('[data-axis="symbols"] .group-tree-children') as HTMLElement;
    expect(childWrap.style.display).toBe("none");
    // Filter unaffected by collapse (no re-render) — still on SY1.
    expect(document.querySelector('.hunk[data-id="H0_1"]')).toBeNull();
  });

  // --- Fold-level ladder + focus reveal -----------------------------------

  /** Registers its own source with the `/file-text` stub — the caller
   *  boots straight after, and the unchanged context between the three
   *  hunks only exists if the route serves it. */
  const foldFile = (): Record<string, unknown> => {
    serveFileText("F0", ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"]);
    return {
    id: "F0", path: "a.py", status: "modified", language: "python",
    adds: 3, dels: 3, summary: "",
    symbols: { added: [], modified: [], removed: [] },
    hunks: [
      makeHunkBlock("H0", "", { old_start: 2, old_count: 1, new_start: 2, new_count: 1,
        rows: [{ kind: "pair", old_line: 2, new_line: 2, old_text: "a", new_text: "A" }] }),
      makeHunkBlock("H1", "", { old_start: 5, old_count: 1, new_start: 5, new_count: 1,
        rows: [{ kind: "pair", old_line: 5, new_line: 5, old_text: "b", new_text: "B" }] }),
      makeHunkBlock("H2", "", { old_start: 8, old_count: 1, new_start: 8, new_count: 1,
        rows: [{ kind: "pair", old_line: 8, new_line: 8, old_text: "c", new_text: "C" }] }),
    ],
    };
  };
  const fold = (level: string): void =>
    (document.querySelector(`.fold-slider button[data-fold="${level}"]`) as HTMLElement).click();
  const codeRows = (sel: string): number =>
    document.querySelectorAll(`${sel} .diff .half-new .row:not(.row-annotation)`).length;

  test("fold ladder reveals code only at 'off'; segment-less hunks fold as one segment", async () => {
    await bootViewer(makeData({ pending: false, files: [foldFile()], symbols: [] }));

    // Default "hunks": headers only, no segment summaries, no code.
    expect(document.querySelectorAll(".hunk-header").length).toBe(3);
    expect(document.querySelectorAll(".seg-list").length).toBe(0);
    expect(codeRows(".hunk")).toBe(0);

    // "segment": every hunk folds to one synthetic segment summary; no code.
    fold("segments");
    expect(document.querySelectorAll(".hunk .seg-list .segment").length).toBe(3);
    expect(codeRows(".hunk")).toBe(0);

    // "off": code revealed, summaries gone.
    fold("off");
    expect(document.querySelectorAll(".seg-list").length).toBe(0);
    expect(codeRows(".hunk")).toBe(3);

    // "hunk": back to headers only.
    fold("hunks");
    expect(codeRows(".hunk")).toBe(0);
    expect(document.querySelectorAll(".seg-list").length).toBe(0);
  });

  test("focus reveals the focused hunk's code; the slider still folds it to level", async () => {
    await bootViewer(makeData({
      pending: false, files: [foldFile()],
      symbols: [{ id: "SY0", title: "mid", rationale: "", hunk_ids: ["H1"] }],
    }));
    // Default "hunks" — nothing revealed yet.
    expect(codeRows('.hunk[data-id="H1"]')).toBe(0);

    // Focus H1 → its code is revealed even at "hunks" level (ephemeral).
    (document.querySelector('[data-axis="symbols"] .group-btn[data-pill-id="SY0"]') as HTMLElement).click();
    expect(codeRows('.hunk[data-id="H1"]')).toBe(1);
    expect(document.querySelector('.hunk[data-id="H0"]')).toBeNull();   // demoted

    // The slider is authoritative under a filter: "hunk" folds H1 to its
    // header (focus-reveal cleared), still filtered.
    fold("hunks");
    expect(codeRows('.hunk[data-id="H1"]')).toBe(0);
    expect(document.querySelector('.hunk[data-id="H1"] .hunk-header')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0"]')).toBeNull();

    // "off" shows its code again.
    fold("off");
    expect(codeRows('.hunk[data-id="H1"]')).toBe(1);
  });

  test("focus reveal does not leak into the unfiltered view", async () => {
    await bootViewer(makeData({
      pending: false, files: [foldFile()],
      symbols: [{ id: "SY0", title: "mid", rationale: "", hunk_ids: ["H1"] }],
    }));
    (document.querySelector('[data-axis="symbols"] .group-btn[data-pill-id="SY0"]') as HTMLElement).click();
    expect(codeRows('.hunk[data-id="H1"]')).toBe(1);

    // Show all → back to the unfiltered "hunks" level: no hunk shows code.
    (document.querySelector(".group-btn-all") as HTMLElement).click();
    expect(document.querySelectorAll(".hunk-header").length).toBe(3);
    expect(codeRows(".hunk")).toBe(0);
  });

  test("clicking a revealed hunk's header folds it (toggle flips the visible state)", async () => {
    await bootViewer(makeData({
      pending: false, files: [foldFile()],
      symbols: [{ id: "SY0", title: "mid", rationale: "", hunk_ids: ["H1"] }],
    }));
    (document.querySelector('[data-axis="symbols"] .group-btn[data-pill-id="SY0"]') as HTMLElement).click();
    expect(codeRows('.hunk[data-id="H1"]')).toBe(1);

    // One click on the header collapses it — not a no-op against the level
    // default (which would say "already folded" and flip it open).
    (document.querySelector('.hunk[data-id="H1"] .hunk-header') as HTMLElement).click();
    expect(codeRows('.hunk[data-id="H1"]')).toBe(0);
  });

  test("done event hides the progress strip and clears pending", async () => {
    await bootViewer(makeData());
    const es = lastEventSource();
    es.dispatch("done", { reason: "augment-complete" });
    const strip = document.getElementById("scr-progress")!;
    expect(strip.classList.contains("hidden")).toBe(true);
    // A hunk that never reported now renders the fail copy on next
    // render — verified indirectly: the intent slot has the empty class.
    const intent = document.querySelector(".hunk-intent")!;
    expect(intent.classList.contains("empty")).toBe(true);
  });
});


describe("LLM observation → comment promotion", () => {
  test("Add as comment opens the editor pre-filled and saves with derived_from", async () => {
    presetCollapseLevel("off");
    const data = makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          line_notes: [{ line: 2, body: "consider using Path" }],
        })],
      }],
    });
    await bootViewer(data);
    await new Promise<void>((r) => setTimeout(r, 0));

    // The line-note annotation is attached to the row at line 2 and
    // carries the source-annotation id on its dataset.
    const noteEl = document.querySelector<HTMLElement>(
      `.row-annotation.annot-note[data-line-note-id="H0_0:line_note:2"]`,
    );
    expect(noteEl).not.toBeNull();
    const promote = noteEl!.querySelector<HTMLButtonElement>(".comment-btn-promote");
    expect(promote).not.toBeNull();

    promote!.click();
    const ta = document.querySelector<HTMLTextAreaElement>(".comment-editor-input");
    expect(ta).not.toBeNull();
    expect(ta!.value).toBe("consider using Path");

    // Capture the POST payload so we can assert derived_from is set.
    let posted: Record<string, unknown> | null = null;
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        posted = JSON.parse(init!.body as string);
        return Promise.resolve({
          status: 200, ok: true, json: () => Promise.resolve(posted),
        } as Response);
      }) as typeof fetch);

    document.querySelector<HTMLButtonElement>(".comment-btn-save")!.click();
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(posted).not.toBeNull();
    expect(posted!.body).toBe("consider using Path");
    expect(posted!.derived_from).toBe("H0_0:line_note:2");
    // Source annotation is gone from the DOM — observation transitioned
    // into the comment.
    expect(document.querySelector(
      `.row-annotation.annot-note[data-line-note-id="H0_0:line_note:2"]`,
    )).toBeNull();
  });

  test("smell pill click saves a comment immediately and detaches the pill", async () => {
    presetCollapseLevel("off");
    const data = makeData({
      pending: false,
      smells_catalogue: {
        perf: { label: "perf concern", severity: "minor", color: "#888" },
      },
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          smells: [{ tag: "perf", note: "tight loop in hot path" }],
        })],
      }],
    });
    await bootViewer(data);
    await new Promise<void>((r) => setTimeout(r, 0));

    // Smell pill carries the source id on its dataset.
    const pill = document.querySelector<HTMLElement>(
      `.smell[data-smell-id="H0_0:smell:perf"]`,
    );
    expect(pill).not.toBeNull();

    // Capture the POST /comments call triggered by the smell promote.
    let posted: Record<string, unknown> | null = null;
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        posted = JSON.parse(init!.body as string);
        return Promise.resolve({
          status: 200, ok: true, json: () => Promise.resolve(posted),
        } as Response);
      }) as typeof fetch);

    pill!.click();
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(posted).not.toBeNull();
    expect(posted!.body).toBe("perf: tight loop in hot path");
    expect(posted!.derived_from).toBe("H0_0:smell:perf");
    expect(posted!.file).toBe("a.py");
    expect(posted!.line).toBe(1);  // hunk new_start
    // Pill is gone after promotion.
    expect(document.querySelector(`.smell[data-smell-id="H0_0:smell:perf"]`)).toBeNull();
  });

  test("line_note already promoted on initial load is hidden", async () => {
    presetCollapseLevel("off");
    const data = makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "", {
          line_notes: [{ line: 1, body: "old observation" }],
        })],
      }],
    });
    await bootViewer(data, {
      comments: [{
        id: "local-1", file: "a.py", side: "new", line: 1,
        body: "promoted version", created_at: 1, updated_at: 1,
        source: "local",
        derived_from: "H0_0:line_note:1",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    // The annotation source is hidden because a local comment with
    // matching derived_from already exists.
    expect(document.querySelector(
      `.row-annotation.annot-note[data-line-note-id="H0_0:line_note:1"]`,
    )).toBeNull();
    // The promoted comment is rendered at the same line.
    expect(document.querySelector(
      '.comment-thread-entry[data-comment-id="local-1"]',
    )).not.toBeNull();
  });
});


describe("sidebar comment counts", () => {
  test("Files-axis pill shows unresolved/total badge once comments load", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({
      pending: false,
      files: [
        {
          id: "F0", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0")],
        },
        {
          id: "F1", path: "b.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H1_0")],
        },
      ],
    }), {
      comments: [
        // a.py: one unresolved root, one resolved root, one reply
        // (replies don't count separately).
        {
          id: "gh-1", file: "a.py", side: "new", line: 1,
          body: "still chasing", created_at: 1, updated_at: 1,
          source: "github", author: "alice", thread_resolved: false,
        },
        {
          id: "gh-1r", file: "a.py", side: "new", line: 1,
          body: "ack", created_at: 2, updated_at: 2,
          source: "github", author: "bob",
          in_reply_to_id: "gh-1", thread_resolved: false,
        },
        {
          id: "gh-2", file: "a.py", side: "new", line: 2,
          body: "done", created_at: 3, updated_at: 3,
          source: "github", author: "alice", thread_resolved: true,
        },
        // b.py: all-resolved.
        {
          id: "gh-3", file: "b.py", side: "new", line: 1,
          body: "lgtm", created_at: 4, updated_at: 4,
          source: "github", author: "alice", thread_resolved: true,
        },
      ],
    });
    // bootViewer waits one tick; sidebar refresh runs on the SECOND
    // microtask (after Comments.init's load resolves), so one extra
    // tick lets the badge land.
    await new Promise<void>((r) => setTimeout(r, 0));

    const filesSection = document.querySelector('[data-axis="files"]')!;
    const pills = Array.from(
      filesSection.querySelectorAll<HTMLElement>(".group-btn"),
    );
    expect(pills).toHaveLength(2);

    // a.py: 1 unresolved of 2 threads (the reply doesn't add to the count).
    const aPyBadge = pills[0].querySelector(".group-btn-comments") as HTMLElement;
    expect(aPyBadge).not.toBeNull();
    expect(aPyBadge.textContent).toBe("1/2");
    expect(aPyBadge.classList.contains("has-unresolved")).toBe(true);

    // b.py: 0 unresolved of 1 — badge present but no warn styling.
    const bPyBadge = pills[1].querySelector(".group-btn-comments") as HTMLElement;
    expect(bPyBadge).not.toBeNull();
    expect(bPyBadge.textContent).toBe("0/1");
    expect(bPyBadge.classList.contains("has-unresolved")).toBe(false);
  });

  test("pills with no comments get no comment badge", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }));
    await new Promise<void>((r) => setTimeout(r, 0));
    const filesSection = document.querySelector('[data-axis="files"]')!;
    const pill = filesSection.querySelector(".group-btn") as HTMLElement;
    expect(pill).not.toBeNull();
    expect(pill.querySelector(".group-btn-comments")).toBeNull();
  });
});


describe("ingested PR comments", () => {
  test("renders author + body_html + permalink, hides edit/delete", async () => {
    const ingested = {
      id: "gh-7",
      file: "a.py",
      side: "new",
      line: 1,
      body: "Use Path.",
      body_html: "<p>Use <code>Path</code>.</p>",
      created_at: 1.0,
      updated_at: 1.0,
      source: "github",
      author: "alice",
      author_avatar_url: "https://example/alice.png",
      html_url: "https://github.com/o/r/pull/1#discussion_r7",
      in_reply_to_id: null,
    };
    // Boot with the fold mode set to "off" so all hunk rows render —
    // default fold is "hunks" which collapses the diff body.
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), { comments: [ingested] });
    // Comment re-attach happens after the store load Promise resolves.
    // One extra tick lets it settle.
    await new Promise<void>((r) => setTimeout(r, 0));

    // A single-comment thread still gets a thread annotation row.
    const annot = document.querySelector(
      '.row-annotation.annot-comment[data-thread-id="gh-7"]',
    ) as HTMLElement | null;
    expect(annot).not.toBeNull();
    expect(annot!.classList.contains("annot-comment-ingested")).toBe(true);
    const entry = annot!.querySelector(
      '.comment-thread-entry[data-comment-id="gh-7"]',
    ) as HTMLElement | null;
    expect(entry).not.toBeNull();
    // Author chip + permalink rendered.
    expect(entry!.querySelector(".comment-author")!.textContent).toBe("@alice");
    expect(entry!.querySelector<HTMLAnchorElement>(".comment-permalink")!.href)
      .toBe("https://github.com/o/r/pull/1#discussion_r7");
    // body_html injected verbatim — the <code> tag is real DOM.
    expect(entry!.querySelector(".comment-body-html code")!.textContent).toBe("Path");
    // No edit/delete buttons on ingested entries.
    expect(entry!.querySelector(".comment-btn-edit")).toBeNull();
    expect(entry!.querySelector(".comment-btn-del")).toBeNull();
    // Reply button at the bottom of the thread.
    expect(annot!.querySelector(".comment-btn-reply")).not.toBeNull();
  });

  test("thread groups parent + replies into one annotation, parent first", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [
        // Out-of-order on the wire: latest reply first. Sorted into
        // root → first-reply → second-reply by created_at.
        {
          id: "gh-3", file: "a.py", side: "new", line: 1,
          body: "later reply", created_at: 3, updated_at: 3,
          source: "github", author: "carol", in_reply_to_id: "gh-1",
        },
        {
          id: "gh-2", file: "a.py", side: "new", line: 1,
          body: "earlier reply", created_at: 2, updated_at: 2,
          source: "github", author: "bob", in_reply_to_id: "gh-1",
        },
        {
          id: "gh-1", file: "a.py", side: "new", line: 1,
          body: "parent", created_at: 1, updated_at: 1,
          source: "github", author: "alice",
        },
      ],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    // Only one annotation row for the whole thread.
    const annots = document.querySelectorAll(
      '.row-annotation.annot-comment[data-thread-id="gh-1"]',
    );
    expect(annots).toHaveLength(1);
    // Entries appear in chronological order, parent first.
    const entries = Array.from(
      (annots[0] as HTMLElement).querySelectorAll(".comment-thread-entry"),
    ) as HTMLElement[];
    expect(entries.map((e) => e.dataset.commentId)).toEqual(["gh-1", "gh-2", "gh-3"]);
    // Replies (but not the root) carry the reply-indent class.
    expect(entries[0].classList.contains("comment-thread-reply")).toBe(false);
    expect(entries[1].classList.contains("comment-thread-reply")).toBe(true);
    expect(entries[2].classList.contains("comment-thread-reply")).toBe(true);
  });

  test("shifted comment anchors at head_line with a 'was line N' chip", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [{
        id: "gh-1", file: "a.py", side: "new",
        line: 99,            // original line in commit_id's tree
        head_line: 2,        // propagated to line 2 at head (rendered data has rows at lines 1,2)
        anchor_status: "shifted",
        body: "still relevant", created_at: 1, updated_at: 1,
        source: "github", author: "alice",
        commit_id: "abc1234567890",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    // Anchor row is the one with linenumber 2 on the new side.
    const annot = document.querySelector(
      '.row-annotation.annot-comment[data-thread-id="gh-1"]',
    ) as HTMLElement | null;
    expect(annot).not.toBeNull();
    // Chip rendered with original line number.
    const chip = annot!.querySelector(".comment-anchor-chip") as HTMLElement;
    expect(chip).not.toBeNull();
    expect(chip.textContent).toBe("was line 99");
    expect(chip.classList.contains("chip-shifted")).toBe(true);
  });

  test("orphaned comment chip names the commit it was lost since", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [{
        id: "gh-1", file: "a.py", side: "new",
        line: 42, head_line: 1,
        anchor_status: "orphaned",
        body: "was this removed?", created_at: 1, updated_at: 1,
        source: "github", author: "alice",
        commit_id: "deadbeef1111",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    const chip = document.querySelector(".comment-anchor-chip") as HTMLElement;
    expect(chip).not.toBeNull();
    expect(chip.textContent).toBe("line removed since deadbee");
    expect(chip.classList.contains("chip-orphaned")).toBe(true);
  });

  test("anchored comment shows no anchor chip", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [{
        id: "gh-1", file: "a.py", side: "new",
        line: 1, head_line: 1,
        anchor_status: "anchored",
        body: "still here", created_at: 1, updated_at: 1,
        source: "github", author: "alice",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".comment-anchor-chip")).toBeNull();
  });

  test("file_gone comments are skipped (no annotation row)", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [{
        id: "gh-1", file: "a.py", side: "new",
        line: 1, head_line: null,
        anchor_status: "file_gone",
        body: "file is gone", created_at: 1, updated_at: 1,
        source: "github", author: "alice",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".row-annotation.annot-comment")).toBeNull();
  });

  test("chip is only on the thread root, not on replies", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [
        {
          id: "gh-1", file: "a.py", side: "new",
          line: 50, head_line: 1, anchor_status: "shifted",
          body: "root", created_at: 1, updated_at: 1,
          source: "github", author: "alice", commit_id: "aaa",
        },
        {
          id: "gh-2", file: "a.py", side: "new",
          line: 50, head_line: 1, anchor_status: "shifted",
          body: "reply", created_at: 2, updated_at: 2,
          source: "github", author: "bob", commit_id: "aaa",
          in_reply_to_id: "gh-1",
        },
      ],
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    const chips = document.querySelectorAll(".comment-anchor-chip");
    expect(chips).toHaveLength(1);
    // Chip lives on the root entry.
    const rootEntry = document.querySelector(
      '.comment-thread-entry[data-comment-id="gh-1"]',
    ) as HTMLElement;
    expect(rootEntry.querySelector(".comment-anchor-chip")).not.toBeNull();
  });

  test("resolved thread renders collapsed; clicking the header expands", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [
        {
          id: "gh-1", file: "a.py", side: "new", line: 1,
          body: "looks good now", created_at: 1, updated_at: 1,
          source: "github", author: "alice", thread_resolved: true,
        },
        {
          id: "gh-2", file: "a.py", side: "new", line: 1,
          body: "ack", created_at: 2, updated_at: 2,
          source: "github", author: "bob", in_reply_to_id: "gh-1",
          thread_resolved: true,
        },
      ],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    const annot = document.querySelector(
      '.row-annotation.annot-comment[data-thread-id="gh-1"]',
    ) as HTMLElement | null;
    expect(annot).not.toBeNull();
    expect(annot!.classList.contains("annot-comment-resolved")).toBe(true);
    expect(annot!.classList.contains("annot-comment-collapsed")).toBe(true);
    // Collapsed: header present, no entry bodies in the DOM.
    expect(annot!.querySelector(".comment-thread-resolved-header")).not.toBeNull();
    expect(annot!.querySelectorAll(".comment-thread-entry")).toHaveLength(0);
    // Header meta surfaces the count + author.
    expect(annot!.querySelector(".comment-thread-resolved-meta")!.textContent)
      .toContain("2 comments");
    expect(annot!.querySelector(".comment-thread-resolved-meta")!.textContent)
      .toContain("@alice");

    // Click the header → thread expands, entries appear.
    annot!.querySelector<HTMLElement>(".comment-thread-resolved-header")!.click();
    const expanded = document.querySelector(
      '.row-annotation.annot-comment[data-thread-id="gh-1"]',
    ) as HTMLElement;
    expect(expanded.classList.contains("annot-comment-collapsed")).toBe(false);
    expect(expanded.querySelectorAll(".comment-thread-entry")).toHaveLength(2);
  });

  test("Reply opens the editor and saves with in_reply_to_id set", async () => {
    presetCollapseLevel("off");
    await bootViewer(makeData({ pending: false }), {
      comments: [{
        id: "gh-1", file: "a.py", side: "new", line: 1,
        body: "parent", created_at: 1, updated_at: 1,
        source: "github", author: "alice",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    const replyBtn = document.querySelector<HTMLButtonElement>(".comment-btn-reply");
    expect(replyBtn).not.toBeNull();
    // /comments POST will be the next captured fetch — queue a 200.
    let postedBody: Record<string, unknown> | null = null;
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        postedBody = JSON.parse(init!.body as string);
        return Promise.resolve({
          status: 200, ok: true,
          json: () => Promise.resolve(postedBody),
        } as Response);
      }) as typeof fetch);

    replyBtn!.click();
    const ta = document.querySelector<HTMLTextAreaElement>(".comment-editor-input");
    expect(ta).not.toBeNull();
    ta!.value = "Acknowledged.";
    document.querySelector<HTMLButtonElement>(".comment-btn-save")!.click();
    // Let the save Promise resolve.
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(postedBody).not.toBeNull();
    expect(postedBody!.body).toBe("Acknowledged.");
    expect(postedBody!.in_reply_to_id).toBe("gh-1");
    expect(postedBody!.file).toBe("a.py");
    expect(postedBody!.line).toBe(1);
  });
});


describe("lazy fold summaries", () => {
  function dataWithFold(): ViewerData {
    // Rows the file-level walker will recognise as a fold: `def foo():`
    // header at indent 0, indented body. The file's `fold_regions` are
    // the summaries the run has stored, addressed in file lines; the
    // viewer detects the region itself and matches by address.
    serveFileSides("F0", ["def foo():", "    x = 1"], ["def foo():", "    x = 2"]);
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok",
        symbols: { added: [], modified: [], removed: [] },
        fold_regions: [
          { context: "both", right_start: 1, right_end: 2,
            left_start: 1, left_end: 2, summary: "" },
        ],
        hunks: [makeHunkBlock("H0_0", "real intent", {
          rows: [
            { kind: "ctx", old_line: 1, new_line: 1, old_text: "def foo():", new_text: "def foo():" },
            { kind: "pair", old_line: 2, new_line: 2, old_text: "    x = 1", new_text: "    x = 2" },
          ],
        })],
      }],
    });
  }

  function expandHunk(): void {
    // The default fold mode is "hunks" — every hunk renders collapsed
    // and its body isn't in the DOM. Click "off" so the diff body
    // (and its fold-chev) materialises. This matches the user flow:
    // expand the fold-slider before reaching for an indent fold.
    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
  }

  /** The `CodeFold` chevron currently on screen. Re-queried after every
   *  click: hide and reveal are state changes that repaint, so the node
   *  a test clicked is not the node it should then assert on. */
  function foldChevron(): SVGElement {
    const el = document.querySelector(".fold-chev") as SVGElement | null;
    if (!el) throw new Error("no .fold-chev in the document");
    return el;
  }

  function clickEl(el: Element): void {
    // jsdom's SVGElement doesn't expose .click(); the addEventListener
    // path needs a dispatched event. Bubbling so the .hunk-header's
    // own click handler doesn't fire from us (stopPropagation in the
    // fold-chev handler covers that).
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  }

  test("a stored summary re-seeds the region the viewer detects", async () => {
    // What the wire records are for. The server does not detect — it
    // publishes the summaries it has, addressed in absolute file lines —
    // so a summary comes back for whatever the viewer detects at that
    // address, whether or not the region has a row in any hunk and
    // whatever the file's language. Collapsing it fires no request.
    const data = dataWithFold();
    (data.files![0].fold_regions as Array<Record<string, unknown>>)[0].summary =
      "sets x";
    await bootViewer(data);
    expandHunk();

    clickEl(foldChevron());

    expect(document.querySelector(".annot-box")?.textContent).toBe("sets x");
    expect(fetchCalls.filter((c) => c.url.includes("/fold-summary"))).toHaveLength(0);
  });

  test("first fold-close posts /fold-summary and renders the response", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "renames the column" },
    });

    const marker = document.querySelector(".fold-chev") as SVGElement | null;
    expect(marker).not.toBeNull();
    clickEl(marker!);
    // The click moves a hidden span and repaints, so the chevron on
    // screen is a fresh node rendered from the new state.
    expect(foldChevron().classList.contains("open")).toBe(false);

    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    const body = JSON.parse((foldCalls[0].init!.body as string));
    // The pair row inside the fold body makes this a "both" region —
    // the model gets to see a diff body for the change.
    expect(body).toEqual({
      file_idx: 0, context: "both",
      right_start: 1, right_end: 2,
      left_start: 1, left_end: 2,
    });

    // Let the fetch promise resolve.
    await new Promise((r) => setTimeout(r, 0));
    const box = document.querySelector(".annot-box");
    expect(box?.textContent).toBe("renames the column");
    expect(box?.classList.contains("pending")).toBe(false);
  });

  test("repeated fold-close while a request is in flight does not re-fire", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    let resolveFetch: (v: { status: number; body: unknown }) => void = () => undefined;
    // Override the per-test mock with a manually-resolved promise so we
    // can re-click while the request is "in flight".
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        return new Promise((r) => {
          resolveFetch = (v) =>
            r({ status: v.status, json: () => Promise.resolve(v.body) } as Response);
        });
      }) as typeof fetch);

    const marker = document.querySelector(".fold-chev") as SVGElement;
    clickEl(marker);           // open → closed: fires request
    const foldCalls = () => fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls()).toHaveLength(1);

    clickEl(marker);           // closed → open: no request
    clickEl(marker);           // open → closed: should NOT re-fire (in-flight guard)
    expect(foldCalls()).toHaveLength(1);

    resolveFetch({ status: 200, body: { file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "done" } });
    await new Promise((r) => setTimeout(r, 0));
    expect(document.querySelector(".annot-box")?.textContent).toBe("done");
  });

  test("pure-deletion fold posts side=old with old-image coordinates", async () => {
    // Base-only: the route serves no head side for a file whose head is
    // over its cap. The removed definition is in the base text, which is
    // where the region is addressed.
    serveFileText("F0", [
      "l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9",
      "def removed():", "    x = 1", "    y = 2",
    ], "base");
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 3, summary: "ok",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          old_start: 10, old_count: 3, new_start: 9, new_count: 0,
          rows: [
            { kind: "del", old_line: 10, new_line: null, old_text: "def removed():", new_text: "" },
            { kind: "del", old_line: 11, new_line: null, old_text: "    x = 1", new_text: "" },
            { kind: "del", old_line: 12, new_line: null, old_text: "    y = 2", new_text: "" },
          ],
        })],
      }],
    }));
    expandHunk();
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, context: "left", right_start: 0, right_end: 0, left_start: 10, left_end: 12, summary: "drops the removed() helper" },
    });

    const marker = document.querySelector(".fold-chev") as SVGElement | null;
    expect(marker).not.toBeNull();
    clickEl(marker!);

    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    expect(JSON.parse(foldCalls[0].init!.body as string)).toEqual({
      file_idx: 0, context: "left", left_start: 10, left_end: 12,
    });

    await new Promise((r) => setTimeout(r, 0));
    expect(document.querySelector(".annot-box")?.textContent).toBe("drops the removed() helper");
  });

  test("fold whose body spans expanded context + a hunk collapses across both", async () => {
    // A def-block opens in the expanded context above a hunk, the
    // hunk lives inside the body, and the body continues for one
    // more indented line. Folding the def-block should collapse
    // rows from both stretches.
    serveFileText("F0", [
      "def foo():",                  // 1 — fold header (in expanded context)
      "    x = 1",                   // 2 — body line (in expanded context)
      "    return new()",            // 3 — body line (lives inside the hunk)
    ]);
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "ok", {
          // Hunk covers line 3 only: replace `return old()` with `return new()`.
          old_start: 3, old_count: 1, new_start: 3, new_count: 1,
          rows: [{
            kind: "pair", old_line: 3, new_line: 3,
            old_text: "    return old()", new_text: "    return new()",
          }],
        })],
      }],
    }));

    // Unfold the hunk so its rows are visible in the file-level
    // row stream — without this, the file-level fold walker only
    // sees the expanded-context rows and the cross-stretch span
    // doesn't form.
    expandHunk();
    // Expand the gap above the hunk (covers lines 1-2).
    const chip = document.querySelector(".gap-chip") as HTMLElement;
    chip.click();

    // One fold chevron now anchors the def-block; its body spans the
    // last expanded-context row AND the pair row inside the hunk.
    const chevrons = document.querySelectorAll(".fold-chev");
    expect(chevrons.length).toBeGreaterThanOrEqual(1);

    // ScrAnnotations.attach injects a .row-annotation wrapper for the
    // fold's summary box; filter it out and only count diff rows.
    const rowTexts = (sel: string): string[] =>
      Array.from(document.querySelectorAll(`${sel} .half-new .row:not(.row-annotation)`))
        .map((r) => r.textContent ?? "");
    expect(rowTexts(".gap-expansion")).toHaveLength(2);
    expect(rowTexts(".hunk").length).toBeGreaterThanOrEqual(1);

    // Click the chevron — the fold's body (expansion row 2 + the hunk's
    // pair row) is not rendered at all; the header row stays, in the
    // container it started in.
    clickEl(chevrons[0]);
    expect(rowTexts(".gap-expansion")).toEqual(["1def foo():"]);
    expect(rowTexts(".hunk")).toEqual([]);

    // Fold-summary fires for the cross-stretch range (lines 1..3).
    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    const body = JSON.parse(foldCalls[0].init!.body as string);
    // Pair row inside the body → context is "both".
    expect(body.context).toBe("both");
    expect(body.right_start).toBe(1);
    expect(body.right_end).toBe(3);
  });

  test("expanded unchanged context exposes its own indent folds", async () => {
    // File starts with 6 lines of unchanged context above a tiny
    // hunk. The first 3 lines form a `def foo():` body — the
    // expand-context path should detect that as an indent fold and
    // attach a chevron the reviewer can click to summarise.
    serveFileText("F0", [
      "def foo():",                  // 1
      "    x = 1",                   // 2
      "    y = 2",                   // 3
      "",                            // 4
      "z = 5",                       // 5
      "z = 6",                       // 6
    ]);
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "trivial", {
          old_start: 7, old_count: 1, new_start: 7, new_count: 1,
          rows: [{ kind: "pair", old_line: 7, new_line: 7, old_text: "a", new_text: "A" }],
        })],
      }],
    }));

    // Expand the gap above the hunk.
    const chip = document.querySelector(".gap-chip") as HTMLElement;
    expect(chip).not.toBeNull();
    chip.click();

    // A fold chevron now lives inside the gap-expansion block.
    const expansion = document.querySelector(".gap-expansion") as HTMLElement;
    expect(expansion).not.toBeNull();
    const marker = expansion.querySelector(".fold-chev") as SVGElement | null;
    expect(marker).not.toBeNull();

    queueFetchResponse({
      status: 200,
      body: {
        file_idx: 0, context: "right", right_start: 1, right_end: 4,
        left_start: 0, left_end: 0, summary: "initialise x and y",
      },
    });
    clickEl(marker!);   // collapse → fires the request

    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    const body = JSON.parse(foldCalls[0].init!.body as string);
    expect(body.context).toBe("right");
    expect(body.file_idx).toBe(0);
    expect(body.right_start).toBe(1);
    // Fold ends at the row before the dedenter; that row is the blank
    // line 4. Matches Python's compute_fold_regions — the algorithm
    // doesn't crop trailing blanks.
    expect(body.right_end).toBe(4);
  });

  test("server's broadcast back to the requesting tab does not pop the fold open", async () => {
    // The server publishes a `fold-summary` SSE event to every
    // subscriber after handling the POST — including the tab that
    // issued it. Re-rendering the hunk on receipt would rebuild the
    // fold in its default-open state and clobber the user's collapse.
    await bootViewer(dataWithFold());
    expandHunk();
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "wraps in try/except" },
    });

    const marker = document.querySelector(".fold-chev") as SVGElement;
    clickEl(marker);   // collapse → POST
    expect(foldChevron().classList.contains("open")).toBe(false);

    // SSE arrives for the same region with the same payload.
    lastEventSource().dispatch("fold-summary", {
      file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "wraps in try/except",
    });
    await new Promise((r) => setTimeout(r, 0));

    // Fold is still collapsed; the box carries the summary text from
    // the fetch handler.
    expect(foldChevron().classList.contains("open")).toBe(false);
    expect(document.querySelector(".annot-box")?.textContent).toBe("wraps in try/except");
  });

  test("failure response surfaces the retry copy", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    queueFetchResponse({ status: 500, body: { error: "boom" } });

    const marker = document.querySelector(".fold-chev") as SVGElement;
    clickEl(marker);   // open → closed
    await new Promise((r) => setTimeout(r, 0));

    // After the failure path swaps in a fresh clone, the box queryable
    // by class is the new node.
    const box = document.querySelector(".annot-box");
    expect(box?.textContent).toContain("summary failed");
    expect(box?.classList.contains("failed")).toBe(true);
  });

  test("fold-summary SSE event patches DATA + DOM in tabs that did not request it", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    const es = lastEventSource();
    es.dispatch("fold-summary", {
      file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "remote summary",
    });
    // The SSE handler drops the rendered cache and replaces the hunk
    // DOM, so the new fold box's content reflects the streamed value.
    const box = document.querySelector(".annot-box");
    expect(box?.textContent).toBe("remote summary");
  });

  // --- One visibility model (ADR 0006) ------------------------------------
  //
  // The renderer's half of the model: what the reviewer hid or revealed is
  // a span, so a repaint reproduces it rather than reverting to whatever
  // the DOM happened to hold. The span algebra itself is exercised without
  // a document in tests/js/visibility.test.ts.

  test("a revealed context gap survives a re-render", async () => {
    serveFileText("F0", ["l1", "l2", "l3", "l4", "l5"]);
    await bootViewer(makeData({
      pending: true,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "ok", {
          old_start: 3, old_count: 1, new_start: 3, new_count: 1,
          rows: [{ kind: "pair", old_line: 3, new_line: 3, old_text: "l3", new_text: "L3" }],
        })],
      }],
    }));

    (document.querySelector(".gap-chip") as HTMLElement).click();
    expect(document.querySelectorAll(".gap-expansion")).toHaveLength(1);
    expect(document.body.textContent).toContain("l1");

    // Any repaint at all — here the augment pass finishing — used to put
    // the chip back, because the expansion was only ever a DOM swap.
    lastEventSource().dispatch("done", { reason: "complete" });

    expect(document.querySelectorAll(".gap-expansion")).toHaveLength(1);
    expect(document.body.textContent).toContain("l1");
  });

  test("a CodeFold inside a file survives collapsing and reopening the file", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    clickEl(foldChevron());
    expect(foldChevron().classList.contains("open")).toBe(false);
    expect(document.body.textContent).not.toContain("x = 2");

    // Collapse the whole file over the top of it, then reopen. State is
    // nested, not flattened: the container's collapse does not destroy
    // what is inside it.
    clickFileHeader("F0");
    expect(fileEl("F0").querySelector(".file-body")).toBeNull();
    clickFileHeader("F0");

    expect(foldChevron().classList.contains("open")).toBe(false);
    expect(document.body.textContent).not.toContain("x = 2");
  });

  test("picking a collapse level is a bulk action, not a reset", async () => {
    // The reviewer folds a lockfile away because they do not intend to
    // read it, then expands everything else. The manual fold-away must
    // not blow back open.
    await bootViewer(makeData({
      pending: false,
      files: [
        {
          id: "F0", path: "src/a.py", status: "modified", language: "python",
          adds: 1, dels: 1, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0", "real change")],
        },
        {
          id: "F1", path: "uv.lock", status: "modified", language: "",
          adds: 1, dels: 1, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H1_0", "regenerated")],
        },
      ],
    }));

    clickFileHeader("F1");
    expect(fileEl("F1").classList.contains("folded")).toBe(true);

    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();

    // Everything else opened to code; the lockfile is still shut.
    expect(fileEl("F0").querySelectorAll(".diff .row").length).toBeGreaterThan(0);
    expect(fileEl("F1").classList.contains("folded")).toBe(true);
    expect(fileEl("F1").querySelector(".file-body")).toBeNull();

    // Reset is the one control that retracts it.
    (document.getElementById("reset-btn") as HTMLElement).click();
    expect(fileEl("F1").classList.contains("folded")).toBe(false);
  });
});

// --- The manifest (ADR 0006, slice 5) -------------------------------------
//
// Hiding a range must not erase what is inside it. Each hide is headed
// by the notes it covers, and every run of lines the renderer skips
// leaves something standing in the gutter's place. What belongs in a
// manifest, given spans and comments, is settled without a document in
// tests/js/manifest.test.ts; these are the renderer's claims.

describe("collapsed content is a manifest, not an absence", () => {
  /** One file, ten lines, two hunks (line 2 and line 8) with unchanged
   *  context above, between and below. */
  function twoHunkFile(lines: string[] | null): ViewerData {
    if (lines) serveFileText("F0", lines);
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 2, dels: 2, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [
          makeHunkBlock("H0_0", "first", {
            old_start: 2, old_count: 1, new_start: 2, new_count: 1,
            rows: [{ kind: "pair", old_line: 2, new_line: 2, old_text: "l2", new_text: "L2" }],
          }),
          makeHunkBlock("H0_1", "second", {
            old_start: 8, old_count: 1, new_start: 8, new_count: 1,
            rows: [{ kind: "pair", old_line: 8, new_line: 8, old_text: "l8", new_text: "L8" }],
          }),
        ],
      }],
    });
  }

  const LINES = ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9", "l10"];

  function comment(
    id: string, line: number, body: string, extra: Record<string, unknown> = {},
  ): Record<string, unknown> {
    return {
      id, file: "a.py", side: "new", line, body,
      created_at: 1, updated_at: 1, ...extra,
    };
  }

  /** Every manifest entry under `root`, as "line text". */
  function entries(root: ParentNode): string[] {
    return Array.from(root.querySelectorAll(".manifest-entry")).map((e) => {
      const line = e.querySelector(".manifest-line")!.textContent;
      const text = e.querySelector(".manifest-text")!.textContent;
      return `${line} ${text}`;
    });
  }

  /** The store load resolves after the first paint, so the repaint that
   *  fills the manifests is one tick behind boot. */
  function settleComments(): Promise<void> {
    return new Promise<void>((r) => setTimeout(r, 0));
  }

  test("collapsing a file shows its unresolved comments as one-line entries", async () => {
    presetCollapseLevel("off");
    await bootViewer(twoHunkFile(LINES), {
      comments: [
        comment("c1", 2, "needs a null check\nand a test"),
        comment("gh1", 8, "settled", { source: "github", thread_resolved: true }),
        comment("c3", 8, "still wrong", { side: "old" }),
      ],
    });
    await settleComments();

    clickFileHeader("F0");

    // The unresolved pair, at their line numbers, one line each; the
    // resolved thread is finished business and is not here. Which side a
    // note sits on is half of where it is, so the two columns are not
    // interchangeable.
    expect(entries(fileEl("F0").querySelector(".manifest-col-old")!))
      .toEqual(["8 still wrong"]);
    expect(entries(fileEl("F0").querySelector(".manifest-col-new")!))
      .toEqual(["2 needs a null check"]);
  });

  test("a comment is coloured as a comment, an LLM annotation as an annotation", async () => {
    const data = twoHunkFile(LINES);
    (data.files![0].hunks as Array<Record<string, unknown>>)[0].line_notes =
      [{ line: 2, body: "unchecked index" }];
    presetCollapseLevel("off");
    await bootViewer(data, { comments: [comment("c1", 8, "needs a null check")] });
    await settleComments();

    clickFileHeader("F0");

    const kinds = Array.from(fileEl("F0").querySelectorAll(".manifest-entry"))
      .map((e) => e.className);
    expect(kinds).toEqual([
      "manifest-entry manifest-annotation",
      "manifest-entry manifest-comment",
    ]);
  });

  test("a hunk collapsed from the start heads itself once the store loads", async () => {
    // The comment store resolves after the first paint, so this hide was
    // already in place — the default level here, restored view state in
    // a real session — before there was anything to list. Without a
    // repaint when the store lands it would head an empty list forever.
    await bootViewer(twoHunkFile(LINES), {
      comments: [comment("c1", 2, "needs a null check")],
    });
    await settleComments();

    expect(entries(fileEl("F0").querySelector('.hunk[data-id="H0_0"]')!))
      .toEqual(["2 needs a null check"]);
    // ... and only in the hunk that covers it.
    expect(entries(fileEl("F0").querySelector('.hunk[data-id="H0_1"]')!)).toEqual([]);
  });

  test("a collapsed context gap heads itself with the notes on those lines", async () => {
    presetCollapseLevel("off");
    await bootViewer(twoHunkFile(LINES), {
      comments: [comment("c1", 5, "this loop is the hot path")],
    });
    await settleComments();

    const chip = fileEl("F0").querySelectorAll(".gap-chip")[1] as HTMLElement;
    expect(entries(chip)).toEqual(["5 this loop is the hot path"]);
  });

  test("a manifest entry is not a way to open what it stands in for", async () => {
    // Not navigable until testing shows people expect it (ADR 0006) —
    // and a click on an entry must not reach the chip underneath.
    presetCollapseLevel("off");
    await bootViewer(twoHunkFile(LINES), {
      comments: [comment("c1", 5, "this loop is the hot path")],
    });
    await settleComments();

    const chip = fileEl("F0").querySelectorAll(".gap-chip")[1] as HTMLElement;
    (chip.querySelector(".manifest-entry") as HTMLElement).click();

    expect(fileEl("F0").querySelectorAll(".gap-expansion")).toHaveLength(0);
    expect(document.body.textContent).not.toContain("l5");
  });

  test("a hunk folded to its segments heads them with its whole body's notes", async () => {
    // A segment span covers head lines only, and the seg-list stands in
    // for the entire body — base side and context rows included.
    const data = twoHunkFile(LINES);
    (data.files![0].hunks as Array<Record<string, unknown>>)[0].rows = [
      { kind: "del", old_line: 2, new_line: null, old_text: "gone", new_text: "" },
    ];
    presetCollapseLevel("segments");
    await bootViewer(data, {
      comments: [comment("c1", 2, "why was this dropped?", { side: "old" })],
    });
    await settleComments();

    const hunk = fileEl("F0").querySelector('.hunk[data-id="H0_0"]')!;
    expect(hunk.querySelector(".seg-list")).not.toBeNull();
    expect(entries(hunk.querySelector(".manifest-col-old")!))
      .toEqual(["2 why was this dropped?"]);
  });

  test("a collapsed CodeFold heads itself, minus the line still on screen", async () => {
    presetCollapseLevel("off");
    serveFileSides("F0", ["def foo():", "    x = 1"], ["def foo():", "    x = 2"]);
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          rows: [
            { kind: "ctx", old_line: 1, new_line: 1, old_text: "def foo():", new_text: "def foo():" },
            { kind: "pair", old_line: 2, new_line: 2, old_text: "    x = 1", new_text: "    x = 2" },
          ],
        })],
      }],
    }), {
      comments: [
        comment("c1", 1, "on the header line"),
        comment("c2", 2, "inside the fold"),
      ],
    });
    await settleComments();

    (document.querySelector(".fold-chev") as SVGElement)
      .dispatchEvent(new MouseEvent("click", { bubbles: true }));

    // The header row is the one line of the span that still renders, so
    // its own comment is on screen rather than in the list.
    expect(entries(fileEl("F0"))).toEqual(["2 inside the fold"]);
    expect(document.querySelector(".annot-manifest")).not.toBeNull();
  });
});

describe("no collapsed state leaves the gutter jumping", () => {
  /** Line-number jumps in one side's gutter that nothing stands in for.
   *
   *  Walks the file body in DOM order, reading the numbers off the rows
   *  of one half and treating anything that presents a hide — a gap chip
   *  or band, a hunk header, a segment summary, a CodeFold chevron — as
   *  an account of the lines skipped after it. A pair left over is a
   *  gutter that jumps with nothing said about the gap: the shape of
   *  issue #10's report, "the hunk was folded ... with a linenumber
   *  jump".
   */
  function unexplainedJumps(
    fileElement: HTMLElement, side: "old" | "new",
  ): Array<[number, number]> {
    const jumps: Array<[number, number]> = [];
    let previous: number | null = null;
    let accounted = true;
    const nodes = fileElement.querySelectorAll(
      `.gap-chip, .hunk-header, .segment, .half-${side} > .row`,
    );
    for (const node of Array.from(nodes)) {
      if (!node.classList.contains("row")) { accounted = true; continue; }
      const cell = node.querySelector(`.cell-lineno-${side}`);
      if (!cell || cell.classList.contains("empty")) continue;  // one-sided row
      const line = parseInt(cell.textContent || "", 10);
      if (isNaN(line)) continue;
      if (previous !== null && line !== previous + 1 && !accounted) {
        jumps.push([previous, line]);
      }
      previous = line;
      // A collapsed CodeFold keeps its first line as the header the
      // chevron hangs off; that chevron is what stands in for the rest.
      // It hangs on whichever half carries the text, and the halves are
      // drawn side by side, so it accounts for both gutters — hence the
      // paired row (the renderer's `_scrPair`, which the comment layer
      // reads the same way).
      const paired = (node as { _scrPair?: HTMLElement })._scrPair;
      accounted = node.querySelector(".fold-chev") !== null
        || (paired?.querySelector(".fold-chev") ?? null) !== null;
    }
    return jumps;
  }

  /** `lines` is the source `/file-text` serves for the file; null models
   *  one it cannot serve at all. */
  function twoHunks(lines: string[] | null): ViewerData {
    if (lines) serveFileText("F0", lines);
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 2, dels: 2, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [
          makeHunkBlock("H0_0", "first", {
            old_start: 2, old_count: 1, new_start: 2, new_count: 1,
            rows: [{ kind: "pair", old_line: 2, new_line: 2, old_text: "l2", new_text: "L2" }],
          }),
          makeHunkBlock("H0_1", "second", {
            old_start: 8, old_count: 1, new_start: 8, new_count: 1,
            rows: [{ kind: "pair", old_line: 8, new_line: 8, old_text: "l8", new_text: "L8" }],
          }),
        ],
      }],
    });
  }

  const LINES = ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9", "l10"];

  function bothSides(): Array<[number, number]> {
    const el = fileEl("F0");
    return [...unexplainedJumps(el, "old"), ...unexplainedJumps(el, "new")];
  }

  test("the walker catches a gutter that skips lines with nothing in between", () => {
    // The check has to be able to fail, or the tests below say nothing.
    document.body.innerHTML = `
      <div class="file" data-id="F0"><div class="file-body"><div class="diff">
        <div class="half half-new">
          <div class="row"><span class="cell cell-lineno cell-lineno-new">2</span></div>
          <div class="row"><span class="cell cell-lineno cell-lineno-new">8</span></div>
        </div>
      </div></div></div>`;
    expect(unexplainedJumps(fileEl("F0"), "new")).toEqual([[2, 8]]);
  });

  for (const level of ["files", "hunks", "segments", "off"] as const) {
    test(`the ${level} level accounts for every line it hides`, async () => {
      presetCollapseLevel(level);
      await bootViewer(twoHunks(LINES));
      expect(bothSides()).toEqual([]);
    });
  }

  test("an expanded gap between two collapsed ones stays continuous", async () => {
    presetCollapseLevel("off");
    await bootViewer(twoHunks(LINES));
    (fileEl("F0").querySelectorAll(".gap-chip")[1] as HTMLElement).click();
    expect(fileEl("F0").querySelectorAll(".gap-expansion")).toHaveLength(1);
    expect(bothSides()).toEqual([]);
  });

  test("a CodeFold hides its body behind the line the chevron sits on", async () => {
    presetCollapseLevel("off");
    serveFileSides(
      "F0",
      ["def foo():", "    a = 1", "    b = 1", "done()"],
      ["def foo():", "    a = 1", "    b = 2", "done()"],
    );
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "intent", {
          old_start: 1, old_count: 4, new_start: 1, new_count: 4,
          rows: [
            { kind: "ctx", old_line: 1, new_line: 1, old_text: "def foo():", new_text: "def foo():" },
            { kind: "ctx", old_line: 2, new_line: 2, old_text: "    a = 1", new_text: "    a = 1" },
            { kind: "pair", old_line: 3, new_line: 3, old_text: "    b = 1", new_text: "    b = 2" },
            { kind: "ctx", old_line: 4, new_line: 4, old_text: "done()", new_text: "done()" },
          ],
        })],
      }],
    }));
    (document.querySelector(".fold-chev") as SVGElement)
      .dispatchEvent(new MouseEvent("click", { bubbles: true }));

    // Lines 2-3 are inside the collapsed definition; line 1 keeps the
    // chevron, so the 1 -> 4 step is accounted for.
    expect(fileEl("F0").querySelectorAll(".half-new > .row .cell-lineno-new").length)
      .toBeLessThan(4);
    expect(bothSides()).toEqual([]);
  });

  test("a file the route cannot serve names the gap between its hunks", async () => {
    // Both sides null: a binary file, or one over `/file-text`'s 2 MB
    // per-side cap. There are no rows to stand a gap chip in front of.
    // The next `@@` header already keeps the gutter honest; the band is
    // what says the lines exist at all, and gives the notes on them
    // somewhere to be.
    presetCollapseLevel("off");
    await bootViewer(twoHunks(null));

    expect(bothSides()).toEqual([]);
    const band = fileEl("F0").querySelector(".gap-absent");
    expect(band).not.toBeNull();
    expect(band!.textContent).toContain("5 unchanged lines");
  });

  test("the band carries the notes on the lines it cannot show", async () => {
    presetCollapseLevel("off");
    await bootViewer(twoHunks(null), {
      comments: [{
        id: "c1", file: "a.py", side: "new", line: 5, body: "still relevant",
        created_at: 1, updated_at: 1,
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    const band = fileEl("F0").querySelector(".gap-absent")!;
    expect(band.querySelector(".manifest-text")!.textContent).toBe("still relevant");
  });

  // The case slice 5 left false, and the reason slice 6 exists: under a
  // filter the region swallows the demoted hunks as well as the context,
  // and with no context to put between them their rows were spliced
  // together — the gutter stepped 2 -> 5 -> 8 with nothing to say so.

  /** Three hunks (lines 2, 5, 8) and a symbols pill covering the first
   *  and last, so the middle one demotes into the region between them. */
  function threeHunksOnePill(lines: string[] | null): ViewerData {
    if (lines) serveFileText("F0", lines);
    const at = (id: string, line: number): Record<string, unknown> =>
      makeHunkBlock(id, "changed", {
        old_start: line, old_count: 1, new_start: line, new_count: 1,
        rows: [{
          kind: "pair", old_line: line, new_line: line,
          old_text: `l${line}`, new_text: `L${line}`,
        }],
      });
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 3, dels: 3, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [at("H0_0", 2), at("H0_1", 5), at("H0_2", 8)],
      }],
      symbols: [{ id: "SY0", title: "ends", rationale: "", hunk_ids: ["H0_0", "H0_2"] }],
    });
  }

  function focusPill(): void {
    document.querySelector<HTMLElement>(
      '[data-axis="symbols"] .group-btn[data-pill-id="SY0"]',
    )!.click();
  }

  test("a demoted hunk expands with its context, under a filter", async () => {
    presetCollapseLevel("off");
    await bootViewer(threeHunksOnePill(LINES));
    focusPill();

    // Chips: line 1 above the first hunk, 3-7 between (the demoted hunk
    // and its context), 9-10 below.
    (fileEl("F0").querySelectorAll(".gap-chip")[1] as HTMLElement).click();

    // The demoted hunk's own row is there, and so is every line between
    // it and the hunks either side.
    const shown = Array.from(
      fileEl("F0").querySelectorAll(".gap-expansion .half-new .cell-lineno-new"),
    ).map((c) => c.textContent);
    expect(shown).toEqual(["3", "4", "5", "6", "7"]);
    expect(bothSides()).toEqual([]);
  });

  test("with no content the demoted hunk stands alone, between two bands", async () => {
    // Nothing to fill the gaps with, so the run breaks at them rather
    // than splicing: the region either side of the demoted hunk is its
    // own, and a band names the lines in between.
    presetCollapseLevel("off");
    await bootViewer(threeHunksOnePill(null));
    focusPill();

    for (const chip of Array.from(fileEl("F0").querySelectorAll<HTMLElement>(".gap-chip"))) {
      if (!chip.classList.contains("gap-absent")) chip.click();
    }
    const bands = Array.from(fileEl("F0").querySelectorAll(".gap-absent"))
      .map((b) => b.textContent);
    expect(bands).toEqual([
      "⋯2 unchanged lines (not loaded)",   // 3-4, before the demoted hunk
      "⋯2 unchanged lines (not loaded)",   // 6-7, after it
    ]);
    expect(bothSides()).toEqual([]);
  });
});

// --- Content arrives after first paint (ADR 0006, slice 6) ----------------
//
// `/file-text` is the viewer's only source of file content, and it
// answers after the first render. What that costs is fold affordances and
// gap chips for one paint; what it must not cost is state — a persisted
// `HiddenSpan` describes itself, so restoring one needs no content, and
// the repaint an arrival triggers must not undo a reveal.

describe("file content arrives asynchronously", () => {
  const LINES_TEN = ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9", "l10"];

  /** One file, one hunk at line 5, so there is a gap either side of it. */
  function gappyFile(id: string, path: string): Record<string, unknown> {
    const n = id.replace("F", "");
    return {
      id, path, status: "modified", language: "python",
      adds: 1, dels: 1, summary: "",
      symbols: { added: [], modified: [], removed: [] },
      hunks: [makeHunkBlock(`H${n}_0`, "changed", {
        old_start: 5, old_count: 1, new_start: 5, new_count: 1,
        rows: [{ kind: "pair", old_line: 5, new_line: 5, old_text: "l5", new_text: "L5" }],
      })],
    };
  }

  function oneFold(): ViewerData {
    serveFileSides("F0", ["def foo():", "    x = 1"], ["def foo():", "    x = 2"]);
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "intent", {
          old_start: 1, old_count: 2, new_start: 1, new_count: 2,
          rows: [
            { kind: "ctx", old_line: 1, new_line: 1, old_text: "def foo():", new_text: "def foo():" },
            { kind: "pair", old_line: 2, new_line: 2, old_text: "    x = 1", new_text: "    x = 2" },
          ],
        })],
      }],
    });
  }

  test("a fold affordance arrives with the content, not before it", async () => {
    presetCollapseLevel("off");
    holdFileTextAnswers();
    await bootViewer(oneFold());
    // First paint: the diff is there, the chevron is not — a region
    // detected off the rendered rows would be addressed by them.
    expect(document.querySelectorAll(".diff .row").length).toBeGreaterThan(0);
    expect(document.querySelector(".fold-chev")).toBeNull();

    await releaseFileText();
    expect(document.querySelector(".fold-chev")).not.toBeNull();
  });

  test("a CodeFold restored from view state is collapsed at first paint", async () => {
    // The ordering claim: state does not wait for content. The span is
    // the whole of the collapse, and the renderer honours it before
    // anything has been detected — the chevron to reopen it is what
    // arrives late.
    presetCollapseLevel("off");
    ViewState.save(RUN_ID, "off", [{
      fileId: "F0",
      spans: [{
        id: "cf:F0:both:1-2:1-2", fileId: "F0", owner: "user", kind: "codefold",
        right: { start: 1, end: 2 }, left: { start: 1, end: 2 },
      }],
      marks: [["cf:F0:both:1-2:1-2", "user"]],
    }]);

    holdFileTextAnswers();
    await bootViewer(oneFold());
    expect(document.body.textContent).not.toContain("x = 2");
    // Hidden with nothing detected: the span is the whole of the state.
    expect(document.querySelector(".fold-chev")).toBeNull();

    await releaseFileText();
    // Still collapsed, and now with the chevron that reopens it: the
    // detector re-found the region rather than a differently-addressed
    // one that would have left the span hiding lines nothing offers back.
    expect(document.body.textContent).not.toContain("x = 2");
    const chev = document.querySelector(".fold-chev") as SVGElement;
    expect(chev.classList.contains("open")).toBe(false);
    chev.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(document.body.textContent).toContain("x = 2");
  });

  test("a late arrival does not re-seed a gap the reviewer opened", async () => {
    // A file the reviewer opens later fetches its content then, and the
    // repaint that lands with it runs the whole renderer — including the
    // seeding of every gap in every other file.
    serveFileText("F0", LINES_TEN);
    serveFileText("F1", LINES_TEN);
    presetCollapseLevel("files");   // no file body renders, so nothing is asked for
    await bootViewer(makeData({
      pending: false,
      files: [gappyFile("F0", "a.py"), gappyFile("F1", "b.py")],
    }));
    expect(fetchCalls.filter((c) => c.url.includes("/file-text"))).toHaveLength(0);

    clickFileHeader("F0");
    await settleFileText();
    (fileEl("F0").querySelector(".gap-chip") as HTMLElement).click();
    expect(fileEl("F0").querySelectorAll(".gap-expansion")).toHaveLength(1);

    clickFileHeader("F1");                  // opens F1 → fetch → repaint
    await settleFileText();
    expect(fetchCalls.filter((c) => c.url.includes("file_idx=1"))).toHaveLength(1);
    expect(fileEl("F0").querySelectorAll(".gap-expansion")).toHaveLength(1);
  });

  test("context after a pure deletion pairs the sides right", async () => {
    // Git writes a zero-count side as the line *before* the hunk
    // (`@@ -10,3 +9,0 @@` deletes base 10-12 after head line 9), so
    // reading the header literally resumes the head side one line early
    // and pairs every later context row against the wrong base line.
    const base = Array.from({ length: 20 }, (_, i) => `l${i + 1}`);
    const head = base.filter((_, i) => i < 9 || i > 11);
    serveFileSides("F0", base, head);
    presetCollapseLevel("off");
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 3, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "drop three", {
          old_start: 10, old_count: 3, new_start: 9, new_count: 0,
          rows: [10, 11, 12].map((n) => ({
            kind: "del", old_line: n, new_line: null,
            old_text: `l${n}`, new_text: "",
          })),
        })],
      }],
    }));

    // Expand the run below the deletion.
    const chips = fileEl("F0").querySelectorAll<HTMLElement>(".gap-chip");
    chips[chips.length - 1].click();
    const rows = Array.from(
      fileEl("F0").querySelectorAll(".gap-expansion:last-of-type .half-new > .row"),
    ).map((r) => [
      (r as HTMLElement & { _scrPair?: HTMLElement })._scrPair!
        .querySelector(".cell-lineno-old")!.textContent,
      r.querySelector(".cell-lineno-new")!.textContent,
    ]);

    // Head 10 is base 13: the three deleted lines are behind it.
    expect(rows[0]).toEqual(["13", "10"]);
    expect(rows[rows.length - 1]).toEqual(["20", "17"]);
  });

  test("a file the route cannot serve is not asked twice", async () => {
    // A failed or empty answer is an answer: re-requesting on every
    // render would be one fetch per repaint for the rest of the session.
    await bootViewer(makeData({ pending: false }));   // nothing registered
    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
    (document.querySelector('.fold-slider button[data-fold="files"]') as HTMLElement).click();
    await settleFileText();

    expect(fetchCalls.filter((c) => c.url.includes("/file-text"))).toHaveLength(1);
  });
});

// --- Per-tab persistence (ADR 0006, slice 4) ------------------------------
//
// View state lives in `sessionStorage` keyed by run, and nowhere else. A
// reload is modelled by booting the bundle a second time against the same
// storage; a second tab by swapping in a fresh one. The storage layer
// itself — the record shape, and what it does with a malformed one — is
// exercised without a document in tests/js/view_state.test.ts.

describe("view state survives a reload, per tab", () => {
  function twoFiles(): ViewerData {
    return makeData({
      pending: false,
      files: [
        {
          id: "F0", path: "src/a.py", status: "modified", language: "python",
          adds: 1, dels: 1, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0", "real change")],
        },
        {
          id: "F1", path: "uv.lock", status: "modified", language: "",
          adds: 1, dels: 1, summary: "",
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H1_0", "regenerated")],
        },
      ],
    });
  }

  test("a reload restores what the reviewer folded by hand", async () => {
    await bootViewer(twoFiles());
    clickFileHeader("F1");
    expect(fileEl("F1").classList.contains("folded")).toBe(true);

    await bootViewer(twoFiles());   // reload: same tab, same storage

    expect(fileEl("F1").classList.contains("folded")).toBe(true);
    expect(fileEl("F0").classList.contains("folded")).toBe(false);
  });

  test("a reload restores the collapse level", async () => {
    await bootViewer(twoFiles());
    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
    expect(fileEl("F0").querySelectorAll(".diff .row").length).toBeGreaterThan(0);

    await bootViewer(twoFiles());

    expect(fileEl("F0").querySelectorAll(".diff .row").length).toBeGreaterThan(0);
    const active = document.querySelector(".fold-slider button.active") as HTMLElement;
    expect(active.dataset.fold).toBe("off");
    // Carried by the stored record, not by the `#fold=` key that used to
    // do this job.
    expect(window.location.hash).toBe("");
  });

  test("a reload restores a reveal, not just a hide", async () => {
    // The marks ledger is what makes this work: the renderer re-seeds
    // every gap it lays out, so persisting the hidden spans alone would
    // put the chip back on the next boot.
    const data = (): ViewerData => {
      serveFileText("F0", ["l1", "l2", "l3", "l4", "l5"]);
      return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "ok", {
          old_start: 3, old_count: 1, new_start: 3, new_count: 1,
          rows: [{ kind: "pair", old_line: 3, new_line: 3, old_text: "l3", new_text: "L3" }],
        })],
      }],
      });
    };

    await bootViewer(data());
    (document.querySelector(".gap-chip") as HTMLElement).click();
    expect(document.querySelectorAll(".gap-expansion")).toHaveLength(1);

    await bootViewer(data());

    expect(document.querySelectorAll(".gap-expansion")).toHaveLength(1);
    expect(document.body.textContent).toContain("l1");
  });

  test("two tabs are independent", async () => {
    await bootViewer(twoFiles());
    clickFileHeader("F1");
    expect(fileEl("F1").classList.contains("folded")).toBe(true);

    // A second tab on the same origin gets its own sessionStorage, so it
    // opens at the defaults rather than inheriting the first tab's folds.
    const firstTab = installTabStorage();
    await bootViewer(twoFiles());
    expect(fileEl("F1").classList.contains("folded")).toBe(false);

    // ...and folding in the second tab left the first tab's record alone.
    clickFileHeader("F0");
    installTabStorage(firstTab!);
    await bootViewer(twoFiles());
    expect(fileEl("F1").classList.contains("folded")).toBe(true);
    expect(fileEl("F0").classList.contains("folded")).toBe(false);
  });

  test("no view state rides in the URL", async () => {
    await bootViewer(twoFiles());
    expect(window.location.hash).toBe("");

    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
    clickFileHeader("F1");

    expect(window.location.hash).toBe("");
  });

  test("a write the browser refuses leaves the viewer working", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    installTabStorage({
      ...makeStorage(),
      setItem: () => { throw new DOMException("quota", "QuotaExceededError"); },
    });

    await bootViewer(twoFiles());
    clickFileHeader("F1");

    // In-memory state is unaffected; only the reload survives is lost.
    expect(fileEl("F1").classList.contains("folded")).toBe(true);
    expect(warn).toHaveBeenCalled();
  });
});


describe("review console", () => {
  // The console_id the module stamped on the most recent /console/ask;
  // SSE frames must carry it to be accepted.
  function lastConsoleId(): string {
    const ask = fetchCalls.filter((c) => c.url.includes("/console/ask")).pop();
    if (!ask) throw new Error("no /console/ask POST captured");
    return (JSON.parse(ask.init!.body as string) as { console_id: string }).console_id;
  }

  async function ask(question: string): Promise<string> {
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;
    input.value = question;
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));
    return lastConsoleId();
  }

  test("submitting a question POSTs /console/ask and streams the answer over SSE", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input");
    expect(input).not.toBeNull();

    const id = await ask("why pagination?");
    const askCalls = fetchCalls.filter((c) => c.url.includes("/console/ask"));
    expect(askCalls.length).toBe(1);

    // The drawer reveals with the question; the answer is empty until
    // the stream lands (the POST body carries no answer).
    const drawer = document.querySelector(".console-drawer");
    expect(drawer?.classList.contains("hidden")).toBe(false);
    expect(document.querySelector(".console-q")?.textContent).toBe("why pagination?");
    expect(input!.value).toBe(""); // cleared, ready for the next turn

    const es = lastEventSource();
    es.dispatch("console-tool", { console_id: id, label: "grep list_users" });
    es.dispatch("console-delta", { console_id: id, text: "pagination threads " });
    es.dispatch("console-delta", { console_id: id, text: "page/size through list_users" });
    es.dispatch("console-done", { console_id: id, answer: "pagination threads page/size through list_users" });

    // Tool activity surfaced, deltas accumulated, pending marker dropped.
    expect(document.querySelector(".console-tool")?.textContent).toBe("grep list_users");
    // The answer is rendered markdown (Slice 3): a single paragraph here.
    const text = document.querySelector(".console-a .console-text");
    expect(text?.querySelector("p")?.textContent).toBe("pagination threads page/size through list_users");
    expect(document.querySelector(".console-a")?.classList.contains("console-pending")).toBe(false);
  });

  test("a non-streaming backend's single console-done renders the whole answer", async () => {
    await bootViewer(makeData({ pending: false }));
    const id = await ask("x");
    // Zero deltas, one done carrying the full text (the Slice 5 CLI shape).
    lastEventSource().dispatch("console-done", { console_id: id, answer: "all at once" });
    expect(document.querySelector(".console-a .console-text p")?.textContent).toBe("all at once");
  });

  test("rendered markdown neutralises script-laden output", async () => {
    await bootViewer(makeData({ pending: false }));
    const id = await ask("x");
    const es = lastEventSource();
    es.dispatch("console-delta", { console_id: id, text: "<script>alert(1)</script>" });
    es.dispatch("console-done", { console_id: id, answer: "<script>alert(1)</script>" });

    // markdown-it runs with html:false and the output is DOMPurify'd, so
    // the tag is inert text, never a live <script> node.
    const text = document.querySelector(".console-a .console-text")!;
    expect(text.querySelector("script")).toBeNull();
    expect(text.textContent).toContain("<script>alert(1)</script>");
  });

  test("frames tagged with another tab's console_id are ignored", async () => {
    await bootViewer(makeData({ pending: false }));
    await ask("x");
    lastEventSource().dispatch("console-delta", { console_id: "some-other-tab", text: "not mine" });
    // Still the pending placeholder — the foreign delta didn't accumulate.
    expect(document.querySelector(".console-a .console-dots")).not.toBeNull();
  });

  test("a console-error frame surfaces the error inline", async () => {
    await bootViewer(makeData({ pending: false }));
    const id = await ask("x");
    lastEventSource().dispatch("console-error", { console_id: id, error: "console unavailable" });

    const answer = document.querySelector(".console-a")!;
    expect(answer.classList.contains("console-error")).toBe(true);
    expect(answer.querySelector(".console-text")?.textContent).toBe("console unavailable");
  });

  test("an immediate non-ok POST fails the turn inline", async () => {
    await bootViewer(makeData({ pending: false }));
    queueFetchResponse({ status: 409, body: { error: "console unavailable" } });

    await ask("x");
    const answer = document.querySelector(".console-a")!;
    expect(answer.classList.contains("console-error")).toBe(true);
    expect(answer.querySelector(".console-text")?.textContent).toBe("console unavailable");
  });

  test("input is gated while augmentation is pending, then unlocked on done", async () => {
    await bootViewer(makeData({ pending: true }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;
    // Disabled with an explanatory placeholder until analysis lands.
    expect(input.disabled).toBe(true);
    expect(input.placeholder).toContain("analysis");

    // The augment-complete event installs the asker server-side; the
    // prompt unlocks to match.
    lastEventSource().dispatch("done", { reason: "augment-complete" });
    expect(input.disabled).toBe(false);
    expect(input.placeholder).toContain("Ask about this change");
  });

  test("an immediate non-ok POST clears busy so the reviewer can retry", async () => {
    await bootViewer(makeData({ pending: false }));
    queueFetchResponse({ status: 409, body: { error: "review console not ready yet" } });

    await ask("x");
    // The failed turn ends: Stop is hidden and the input is no longer busy,
    // so a second question can be asked rather than the console wedging.
    const stop = document.querySelector<HTMLButtonElement>(".console-stop")!;
    expect(stop.classList.contains("hidden")).toBe(true);
    expect(document.querySelector(".console-input")?.classList.contains("busy")).toBe(false);

    await ask("y");
    const askCalls = fetchCalls.filter((c) => c.url.includes("/console/ask"));
    expect(askCalls.length).toBe(2);
  });

  test("Stop cancels the in-flight turn via /console/cancel", async () => {
    await bootViewer(makeData({ pending: false }));
    const id = await ask("why?");

    const stop = document.querySelector<HTMLButtonElement>(".console-stop")!;
    expect(stop.classList.contains("hidden")).toBe(false); // visible while busy
    es_clickStop(stop);
    await new Promise<void>((r) => setTimeout(r, 0));

    const cancel = fetchCalls.filter((c) => c.url.includes("/console/cancel"));
    expect(cancel.length).toBe(1);
    expect((JSON.parse(cancel[0].init!.body as string) as { console_id: string }).console_id).toBe(id);

    // The worker acknowledges with a cancelled done; the partial answer stays.
    lastEventSource().dispatch("console-delta", { console_id: id, text: "partial" });
    lastEventSource().dispatch("console-done", { console_id: id, cancelled: true });
    const answer = document.querySelector(".console-a")!;
    expect(answer.classList.contains("console-cancelled")).toBe(true);
    expect(answer.querySelector(".console-text p")?.textContent).toBe("partial");
  });

  function es_clickStop(stop: HTMLButtonElement): void {
    stop.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  }

  test("Esc cancels an in-flight turn before it collapses the drawer", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;
    const id = await ask("q");
    expect(document.querySelector(".console-q")).not.toBeNull();

    // First Esc, while busy: cancels but leaves the drawer open.
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(fetchCalls.some((c) => c.url.includes("/console/cancel"))).toBe(true);
    expect(document.querySelector(".console-drawer")?.classList.contains("hidden")).toBe(false);
    expect(fetchCalls.some((c) => c.url.includes("/console/reset"))).toBe(false);

    // The worker finishes the cancelled turn, clearing the busy state.
    lastEventSource().dispatch("console-done", { console_id: id, cancelled: true });

    // Second Esc, idle: collapses, clears the transcript, resets the server.
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".console-drawer")?.classList.contains("hidden")).toBe(true);
    expect(document.querySelector(".console-q")).toBeNull();
    expect(fetchCalls.some((c) => c.url.includes("/console/reset"))).toBe(true);
  });
});

// --- Rendered markdown mode (ADR 0004 slice 2) -----------------------------
// End-to-end through the bundled viewer: the per-file toggle appears only
// for .md files; flipping it fetches /file-text and swaps the body from
// the text diff to the two-pane rendered block diff (and back). Comments
// authored on a rendered block round-trip through the same anchor.

describe("rendered markdown mode", () => {
  function mdData(): ViewerData {
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "docs/x.md", status: "modified", language: "markdown",
        adds: 1, dels: 1, summary: "",
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0")],
      }],
    });
  }

  test("a markdown file gets a Rendered toggle", async () => {
    await bootViewer(mdData());
    const toggle = document.querySelector(".md-toggle");
    expect(toggle).not.toBeNull();
    expect(toggle!.textContent).toBe("Rendered");
  });

  test("a non-markdown file gets no toggle", async () => {
    await bootViewer(makeData({ pending: false }));  // a.py
    expect(document.querySelector(".md-toggle")).toBeNull();
  });

  test("flipping on renders base-left / head-right from the served source", async () => {
    serveFileSides("F0", ["# Old"], ["# New", "", "hello world"]);
    await bootViewer(mdData());
    (document.querySelector(".md-toggle") as HTMLElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));

    const grid = document.querySelector(".rmd-grid");
    expect(grid).not.toBeNull();
    // textContent, not innerHTML: the changed heading carries intra-block
    // sub-diff (.char-chg) spans around the changed word.
    expect(grid!.querySelector(".rmd-col-old .rmd-block h1")!.textContent).toBe("Old");
    const headText = Array.from(grid!.querySelectorAll(".rmd-col-new .rmd-block"))
      .map((b) => b.textContent).join(" ");
    expect(headText).toContain("New");
    expect(headText).toContain("hello world");
    // The text-diff body is gone while rendered.
    expect(document.querySelector(".file .hunk")).toBeNull();
    expect(fetchCalls.some((c) => c.url.includes("/file-text?file_idx=0"))).toBe(true);
    expect(document.querySelector(".md-toggle")!.textContent).toBe("Diff");
  });

  test("flipping back restores the text diff untouched", async () => {
    serveFileSides("F0", ["# Old"], ["# New"]);
    await bootViewer(mdData());
    const toggle = (): HTMLElement => document.querySelector(".md-toggle") as HTMLElement;
    toggle().click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".rmd-grid")).not.toBeNull();

    toggle().click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".rmd-grid")).toBeNull();
    expect(document.querySelector(".file .hunk")).not.toBeNull();
  });

  test("flipping does not re-fetch — the text diff and rendered mode share one source", async () => {
    serveFileSides("F0", ["# Old"], ["# New"]);
    await bootViewer(mdData());
    const toggle = (): HTMLElement => document.querySelector(".md-toggle") as HTMLElement;
    toggle().click();                                   // on — fetches
    await new Promise<void>((r) => setTimeout(r, 0));
    toggle().click();                                   // off
    await new Promise<void>((r) => setTimeout(r, 0));
    toggle().click();                                   // on again — cached
    await new Promise<void>((r) => setTimeout(r, 0));

    const hits = fetchCalls.filter((c) => c.url.includes("/file-text")).length;
    expect(hits).toBe(1);
    expect(document.querySelector(".rmd-grid")).not.toBeNull();
  });

  test("a comment on a rendered block anchors on its source line and round-trips", async () => {
    presetCollapseLevel("off");  // expand the diff body so the round-trip row renders
    serveFileSides("F0", ["# Old"], ["# New", "", "hello world"]);
    await bootViewer(mdData());
    (document.querySelector(".md-toggle") as HTMLElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));

    // The head heading block sits on source line 1 (a diff row). Its
    // hover affordance opens the editor anchored there.
    const headBlock = document.querySelector(".rmd-col-new .rmd-block")!;
    const addBtn = headBlock.parentElement!.querySelector<HTMLButtonElement>(".rmd-comment-btn")!;
    addBtn.click();
    const ta = document.querySelector<HTMLTextAreaElement>(".comment-editor-input")!;
    expect(ta).not.toBeNull();
    ta.value = "reads well";

    let posted: Record<string, unknown> | null = null;
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        posted = JSON.parse(init!.body as string);
        return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(posted) } as Response);
      }) as typeof fetch);
    document.querySelector<HTMLButtonElement>(".comment-btn-save")!.click();
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(posted).not.toBeNull();
    expect(posted!.file).toBe("docs/x.md");
    expect(posted!.side).toBe("new");
    expect(posted!.line).toBe(1);

    // Flip back to the text diff: the comment surfaces on the new-side
    // row at line 1 — same anchor, no new machinery.
    (document.querySelector(".md-toggle") as HTMLElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".comment-thread-entry")!.textContent).toContain("reads well");
  });
});
