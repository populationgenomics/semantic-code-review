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

interface FetchResponse {
  status: number;
  body: unknown;
}
const fetchResponses: FetchResponse[] = [];
const fetchCalls: Array<{ url: string; init: RequestInit | undefined }> = [];
// `GET /explainer` is answered by URL rather than from the positional
// queue: boot fires it behind /comments and PostModal's /post-config,
// so its position depends on wiring the test has no reason to know.
let explainerLoadResponse: FetchResponse | null = null;

function queueFetchResponse(r: FetchResponse): void {
  fetchResponses.push(r);
}

// --- Boot helper ----------------------------------------------------------

interface ViewerData {
  version?: string;
  pending?: boolean;
  explainer?: boolean;
  pr?: Record<string, unknown>;
  smells_catalogue?: Record<string, unknown>;
  files?: Array<Record<string, unknown>>;
  groups?: Array<Record<string, unknown>>;
}

interface BootOptions {
  /** Body the /comments fetch fired by Comments.init should resolve to.
   *  Defaults to an empty array. */
  comments?: unknown[];
  /** Response for the `GET /explainer` pre-fetch boot fires when
   *  `data.explainer` is set. Must be queued between /comments and
   *  anything the test adds, so it goes here rather than at the call
   *  site. */
  explainer?: FetchResponse;
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
      <div class="mode-strip">
        <button id="overview-btn" class="mode-btn" disabled></button>
      </div>
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
    <footer id="status-bar"></footer>
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
  explainerLoadResponse = data.explainer
    ? (opts.explainer ?? { status: 404, body: { error: "no explainer document" } })
    : null;
  // Execute viewer.js as a fresh IIFE in the current realm so it
  // picks up our stubs. `new Function` ensures strict-mode + clean
  // scope. The IIFE returns synchronously; the boot continues on
  // microtasks once the /data.json fetch resolves.
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  new Function(VIEWER_SRC)();
  // Drain microtasks + one macrotask tick so the fetch promise
  // chain resolves, boot() runs, Comments.init's /comments fetch
  // resolves, and all sync init lands before the test asserts.
  await new Promise<void>((r) => setTimeout(r, 0));
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
    fold_regions: [],
    ...overrides,
  };
}

/** A file the diff only adds or only deletes: every row carries a line
 *  on one side, so the other half of its grid is empty for the file's
 *  whole length. */
function makeOneSidedFile(
  idx: number, path: string, status: "added" | "deleted",
): Record<string, unknown> {
  const added = status === "added";
  const rows = added
    ? [
      { kind: "ins", old_line: null, new_line: 1, old_text: "", new_text: "first" },
      { kind: "ins", old_line: null, new_line: 2, old_text: "", new_text: "second" },
    ]
    : [
      { kind: "del", old_line: 1, new_line: null, old_text: "first", new_text: "" },
      { kind: "del", old_line: 2, new_line: null, old_text: "second", new_text: "" },
    ];
  return {
    id: `F${idx}`, path, status, language: "python",
    adds: added ? 2 : 0, dels: added ? 0 : 2,
    summary: "", head_lines: null,
    symbols: { added: [], modified: [], removed: [] },
    hunks: [makeHunkBlock(`H${idx}_0`, "the whole file", {
      header: added ? "@@ -0,0 +1,2 @@" : "@@ -1,2 +0,0 @@",
      old_start: added ? 0 : 1, old_count: added ? 0 : 2,
      new_start: added ? 1 : 0, new_count: added ? 2 : 0,
      adds: added ? 2 : 0, dels: added ? 0 : 2,
      rows,
    })],
  };
}

/** Put the shipped stylesheet in the document, for the cases whose
 *  claim is a layout decision rather than a class. jsdom has no layout
 *  but does cascade a stylesheet, so `display` and
 *  `grid-template-columns` read back off the real rules. Call after
 *  bootViewer, which writes the head itself. */
function installStylesheet(): void {
  const style = document.createElement("style");
  style.textContent = fs.readFileSync(
    path.resolve(process.cwd(), "semantic_code_review/viewer/assets/viewer.css"),
    "utf-8",
  );
  document.head.appendChild(style);
}

/** Drag a layout divider from `fromX` to `toX`. jsdom ships no
 *  PointerEvent, so the pointer type names ride on MouseEvent — which
 *  carries the `clientX` the drag reads — and `setPointerCapture` is the
 *  no-op setup.ts installs. */
function dragDivider(el: HTMLElement, fromX: number, toX: number): void {
  el.dispatchEvent(new MouseEvent("pointerdown", { clientX: fromX, button: 0, bubbles: true }));
  el.dispatchEvent(new MouseEvent("pointermove", { clientX: toX, bubbles: true }));
  el.dispatchEvent(new MouseEvent("pointerup", { clientX: toX, bubbles: true }));
}

function nudgeDivider(el: HTMLElement, key: string, shiftKey = false): void {
  el.dispatchEvent(new KeyboardEvent("keydown", { key, shiftKey, bubbles: true }));
}

function makeData(overrides: Partial<ViewerData> = {}): ViewerData {
  return {
    version: "1",
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
      head_lines: null,
      hunks: [makeHunkBlock("H0_0")],
    }],
    groups: [],
    symbols: [],
    ...overrides,
  };
}

// --- Global hooks ----------------------------------------------------------

beforeEach(() => {
  eventSourceInstances.length = 0;
  fetchResponses.length = 0;
  fetchCalls.length = 0;
  // Reset persisted viewer state between tests. The viewer restores the
  // focused sidebar pill from localStorage (sidebar.ts) and fold/focus from
  // location.hash (render.ts _restoreHash) on boot; neither is cleared by
  // wiping the DOM. Without this, a prior test's focused symbol re-applies on
  // the next boot — highlighting before the test acts and leaking symbol-hit
  // spans. node 25's timing masked it; node 20's exposed it.
  localStorage.clear();
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    EventSourceStub as unknown as typeof EventSource;
  explainerLoadResponse = null;
  vi.spyOn(globalThis, "fetch").mockImplementation(((url: string, init?: RequestInit) => {
    fetchCalls.push({ url, init });
    const next = (url === "/explainer" && explainerLoadResponse !== null)
      ? explainerLoadResponse
      : fetchResponses.shift() ?? { status: 200, body: {} };
    return Promise.resolve({
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      json: () => Promise.resolve(next.body),
    } as Response);
  }) as typeof fetch);
});

afterEach(() => {
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});


describe("pending boot", () => {
  test("progress strip shows total + every square starts queued", async () => {
    await bootViewer(makeData({
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_lines: null,
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
          adds: 0, dels: 0, summary: "", head_lines: null,
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0"), makeHunkBlock("H0_1")],
        },
        {
          id: "F1", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_lines: null,
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
        adds: 0, dels: 0, summary: "", head_lines: null,
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
          adds: 0, dels: 0, summary: "", head_lines: null,
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0", "alpha"), makeHunkBlock("H0_1", "beta")],
        },
        {
          id: "F1", path: "b.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_lines: null,
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
      adds: 0, dels: 0, summary: "", head_lines: null,
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
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 3, dels: 3, summary: "",
        // 9 lines with changed lines at 2, 5, 8 → context at 1, 3-4, 6-7, 9.
        head_lines: ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"],
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
        adds: 0, dels: 0, summary: "", head_lines: null,
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
    window.location.hash = "#fold=off"; // expand hunks so diff bodies render
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_lines: null,
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
    window.location.hash = "";
  });

  test("symbols axis nests methods under their class and filters by subtree", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_lines: null,
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

  const foldFile = (): Record<string, unknown> => ({
    id: "F0", path: "a.py", status: "modified", language: "python",
    adds: 3, dels: 3, summary: "",
    head_lines: ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"],
    symbols: { added: [], modified: [], removed: [] },
    hunks: [
      makeHunkBlock("H0", "", { old_start: 2, old_count: 1, new_start: 2, new_count: 1,
        rows: [{ kind: "pair", old_line: 2, new_line: 2, old_text: "a", new_text: "A" }] }),
      makeHunkBlock("H1", "", { old_start: 5, old_count: 1, new_start: 5, new_count: 1,
        rows: [{ kind: "pair", old_line: 5, new_line: 5, old_text: "b", new_text: "B" }] }),
      makeHunkBlock("H2", "", { old_start: 8, old_count: 1, new_start: 8, new_count: 1,
        rows: [{ kind: "pair", old_line: 8, new_line: 8, old_text: "c", new_text: "C" }] }),
    ],
  });
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
    window.location.hash = "#fold=off";
    const data = makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_lines: null,
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
    window.location.hash = "#fold=off";
    const data = makeData({
      pending: false,
      smells_catalogue: {
        perf: { label: "perf concern", severity: "minor", color: "#888" },
      },
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_lines: null,
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
    window.location.hash = "#fold=off";
    const data = makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_lines: null,
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
    window.location.hash = "#fold=off";
    await bootViewer(makeData({
      pending: false,
      files: [
        {
          id: "F0", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_lines: null,
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0")],
        },
        {
          id: "F1", path: "b.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_lines: null,
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    window.location.hash = "#fold=off";
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
    // header at indent 0, indented body. The fold_regions block is
    // server-computed; the viewer re-detects from the rows but uses
    // the block when looking up an existing summary.
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok", head_lines: null,
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          rows: [
            { kind: "ctx", old_line: 1, new_line: 1, old_text: "def foo():", new_text: "def foo():" },
            { kind: "pair", old_line: 2, new_line: 2, old_text: "    x = 1", new_text: "    x = 2" },
          ],
          fold_regions: [
            { header_idx: 0, body_start_idx: 1, body_end_idx: 1,
              context: "both", right_start: 1, right_end: 2,
              left_start: 1, left_end: 2,
              has_changes: true, summary: "" },
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

  function clickEl(el: Element): void {
    // jsdom's SVGElement doesn't expose .click(); the addEventListener
    // path needs a dispatched event. Bubbling so the .hunk-header's
    // own click handler doesn't fire from us (stopPropagation in the
    // fold-chev handler covers that).
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  }

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
    expect(marker!.classList.contains("open")).toBe(false);

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
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 3, summary: "ok", head_lines: null,
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          rows: [
            { kind: "del", old_line: 10, new_line: null, old_text: "def removed():", new_text: "" },
            { kind: "del", old_line: 11, new_line: null, old_text: "    x = 1", new_text: "" },
            { kind: "del", old_line: 12, new_line: null, old_text: "    y = 2", new_text: "" },
          ],
          fold_regions: [{
            header_idx: 0, body_start_idx: 1, body_end_idx: 2,
            context: "left", right_start: null, right_end: null,
            left_start: 10, left_end: 12, has_changes: true, summary: "",
          }],
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
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok",
        head_lines: [
          "def foo():",                  // 1 — fold header (in expanded context)
          "    x = 1",                   // 2 — body line (in expanded context)
          "    return new()",            // 3 — body line (lives inside the hunk)
        ],
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

    // Identify the row elements (one per side) we expect to hide.
    // ScrAnnotations.attach injects a .row-annotation wrapper for the
    // fold's summary box; filter it out and only count diff rows.
    const expansionRows = document.querySelectorAll(
      ".gap-expansion .half-new .row:not(.row-annotation)",
    );
    const hunkRows = document.querySelectorAll(
      ".hunk .half-new .row:not(.row-annotation)",
    );
    expect(expansionRows.length).toBe(2);
    expect(hunkRows.length).toBeGreaterThanOrEqual(1);
    // Pre-condition: all visible.
    expect((expansionRows[1] as HTMLElement).style.display).not.toBe("none");
    expect((hunkRows[0] as HTMLElement).style.display).not.toBe("none");

    // Click the chevron — body of the fold (expansion row 2 + hunk row 1)
    // should go to display:none. Header (expansion row 1) stays.
    clickEl(chevrons[0]);
    expect((expansionRows[0] as HTMLElement).style.display).not.toBe("none");
    expect((expansionRows[1] as HTMLElement).style.display).toBe("none");
    expect((hunkRows[0] as HTMLElement).style.display).toBe("none");

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
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok",
        head_lines: [
          "def foo():",                  // 1
          "    x = 1",                   // 2
          "    y = 2",                   // 3
          "",                            // 4
          "z = 5",                       // 5
          "z = 6",                       // 6
        ],
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
    // line (row 4 of head_lines). Matches Python's compute_fold_regions
    // — the algorithm doesn't crop trailing blanks.
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
    expect(marker.classList.contains("open")).toBe(false);

    // SSE arrives for the same region with the same payload.
    lastEventSource().dispatch("fold-summary", {
      file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "wraps in try/except",
    });
    await new Promise((r) => setTimeout(r, 0));

    // Fold is still collapsed; the box carries the summary text from
    // the fetch handler.
    const markerAfter = document.querySelector(".fold-chev") as SVGElement;
    expect(markerAfter.classList.contains("open")).toBe(false);
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

// --- One-sided files -------------------------------------------------------
// An added file's rows have no pre-image and a deleted file's no
// post-image, so one half of the grid is blank for its whole length. The
// renderer marks the stream and the stylesheet collapses to the live
// half; the empty one stays in the DOM for the comment gutter and the
// annotation placeholders to address.

describe("one-sided files", () => {
  function oneSidedData(status: "added" | "deleted"): ViewerData {
    return makeData({
      pending: false,
      files: [makeOneSidedFile(0, "new_thing.py", status)],
    });
  }

  test("an added file drops the old half and spans the new one", async () => {
    window.location.hash = "#fold=off";      // the code, not the summaries
    await bootViewer(oneSidedData("added"));
    installStylesheet();

    const diff = document.querySelector("#app .file .diff") as HTMLElement;
    expect(diff.classList.contains("diff-only-new")).toBe(true);
    const half = (side: string): HTMLElement =>
      diff.querySelector(`.half-${side}`) as HTMLElement;
    // In the DOM — comments and annotations address its nodes — and out
    // of the layout.
    expect(half("old")).not.toBeNull();
    expect(getComputedStyle(half("old")).display).toBe("none");
    expect(getComputedStyle(half("new")).display).toBe("grid");
    expect(getComputedStyle(diff).gridTemplateColumns).toBe("minmax(0, 1fr)");
    expect(half("new").querySelectorAll(".row")).toHaveLength(2);
  });

  test("a deleted file drops the new half", async () => {
    window.location.hash = "#fold=off";
    await bootViewer(oneSidedData("deleted"));
    installStylesheet();

    const diff = document.querySelector("#app .file .diff") as HTMLElement;
    expect(diff.classList.contains("diff-only-old")).toBe(true);
    expect(getComputedStyle(diff.querySelector(".half-new") as HTMLElement).display).toBe("none");
    expect(getComputedStyle(diff.querySelector(".half-old") as HTMLElement).display).toBe("grid");
  });

  test("a modified file keeps both halves", async () => {
    window.location.hash = "#fold=off";
    await bootViewer(makeData({ pending: false }));   // a.py, pair rows
    installStylesheet();

    const diff = document.querySelector("#app .file .diff") as HTMLElement;
    expect(diff.className).toBe("diff");
    expect(getComputedStyle(diff.querySelector(".half-old") as HTMLElement).display).toBe("grid");
    expect(getComputedStyle(diff).gridTemplateColumns).toBe("minmax(0, 1fr) minmax(0, 1fr)");
  });

  test("a comment on an added file's line anchors on the new side", async () => {
    window.location.hash = "#fold=off";
    await bootViewer(oneSidedData("added"));

    const cell = document.querySelector("#app .half-new .row .cell-lineno") as HTMLElement;
    expect(cell.textContent).toBe("1");
    cell.click();
    const ta = document.querySelector<HTMLTextAreaElement>(".comment-editor-input")!;
    ta.value = "this file needs a header";

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
    expect(posted!.file).toBe("new_thing.py");
    expect(posted!.side).toBe("new");
    expect(posted!.line).toBe(1);
    expect(document.querySelector(".comment-thread-entry")!.textContent)
      .toContain("this file needs a header");
    // The alignment placeholder went into the dropped half, where it
    // costs nothing.
    expect(document.querySelector("#app .half-old .row-placeholder")).not.toBeNull();
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
        adds: 1, dels: 1, summary: "", head_lines: null,
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

  test("flipping on fetches /file-text and renders base-left / head-right", async () => {
    await bootViewer(mdData());
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, path: "docs/x.md", base: "# Old", head: "# New\n\nhello world" },
    });
    (document.querySelector(".md-toggle") as HTMLElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));

    const grid = document.querySelector(".rmd-grid");
    expect(grid).not.toBeNull();
    expect(grid!.className).toBe("rmd-grid");   // both panes hold blocks
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
    await bootViewer(mdData());
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, path: "docs/x.md", base: "# Old", head: "# New" },
    });
    const toggle = (): HTMLElement => document.querySelector(".md-toggle") as HTMLElement;
    toggle().click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".rmd-grid")).not.toBeNull();

    toggle().click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".rmd-grid")).toBeNull();
    expect(document.querySelector(".file .hunk")).not.toBeNull();
  });

  test("re-flipping on does not re-fetch (source is cached)", async () => {
    await bootViewer(mdData());
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, path: "docs/x.md", base: "# Old", head: "# New" },
    });
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

  test("an added file's rendered diff drops the base pane", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [makeOneSidedFile(0, "docs/new.md", "added")],
    }));
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, path: "docs/new.md", base: null, head: "# New\n\nhello world" },
    });
    (document.querySelector(".md-toggle") as HTMLElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    installStylesheet();

    const grid = document.querySelector(".rmd-grid") as HTMLElement;
    expect(grid.classList.contains("rmd-only-new")).toBe(true);
    // Every base cell was an alignment pad; the head pane takes the grid.
    expect(grid.querySelectorAll(".rmd-col-old .rmd-block")).toHaveLength(0);
    expect(getComputedStyle(grid).gridTemplateColumns).toBe("1fr");
    expect(getComputedStyle(grid.querySelector(".rmd-col-old") as HTMLElement).display)
      .toBe("none");
    expect(grid.querySelectorAll(".rmd-col-new .rmd-block").length).toBeGreaterThan(0);
  });

  test("a deleted file's rendered diff drops the head pane", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [makeOneSidedFile(0, "docs/gone.md", "deleted")],
    }));
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, path: "docs/gone.md", base: "# Gone\n\nfarewell", head: null },
    });
    (document.querySelector(".md-toggle") as HTMLElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    installStylesheet();

    const grid = document.querySelector(".rmd-grid") as HTMLElement;
    expect(grid.classList.contains("rmd-only-old")).toBe(true);
    const base = grid.querySelector(".rmd-col-old") as HTMLElement;
    expect(getComputedStyle(grid.querySelector(".rmd-col-new") as HTMLElement).display)
      .toBe("none");
    expect(getComputedStyle(base).display).toBe("block");
    expect(getComputedStyle(grid).gridTemplateColumns).toBe("1fr");
  });

  test("a comment on a rendered block anchors on its source line and round-trips", async () => {
    window.location.hash = "#fold=off";  // expand the diff body so the round-trip row renders
    await bootViewer(mdData());
    queueFetchResponse({
      status: 200,
      body: { file_idx: 0, path: "docs/x.md", base: "# Old", head: "# New\n\nhello world" },
    });
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

// --- Draggable layout boundaries -------------------------------------------
// The sidebar's edge. jsdom reports every box as 0 wide, so a drag starts
// from 0 and the pointer's own travel is the width it lands on — which is
// enough for the claims here: what the drag writes, what it stores, and
// what the stored number does on the next boot.

describe("the sidebar divider", () => {
  function divider(): HTMLElement {
    return document.querySelector(".layout .layout-divider-sidebar") as HTMLElement;
  }

  function basis(): string {
    return (document.getElementById("group-sidebar") as HTMLElement).style.flexBasis;
  }

  test("it is a separator the keyboard can reach, between the sidebar and the pane", async () => {
    await bootViewer(makeData({ pending: false }));
    installStylesheet();
    const el = divider();
    expect(el.getAttribute("role")).toBe("separator");
    expect(el.getAttribute("aria-orientation")).toBe("vertical");
    expect(el.tabIndex).toBe(0);
    expect(el.previousElementSibling!.id).toBe("group-sidebar");
    expect(el.nextElementSibling!.id).toBe("app");
    expect(getComputedStyle(el).cursor).toBe("col-resize");
  });

  test("a repaint of the pane leaves it alone", async () => {
    // It is a `.layout` child, not a member of either pane, which is
    // what makes it the sidebar's edge in whichever mode is showing.
    await bootViewer(makeData({ pending: false }));
    const el = divider();
    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
    expect(divider()).toBe(el);
  });

  test("dragging writes the sidebar's basis and stores the width", async () => {
    await bootViewer(makeData({ pending: false }));
    dragDivider(divider(), 0, 300);
    expect(basis()).toBe("300px");
    expect(localStorage.getItem("scr-sidebar-width")).toBe("300");
  });

  test("the drag is held inside a floor and a share of the window", async () => {
    await bootViewer(makeData({ pending: false }));
    dragDivider(divider(), 0, 20);
    expect(basis()).toBe("160px");                 // the floor
    dragDivider(divider(), 0, 5000);
    expect(basis()).toBe("410px");                 // 40% of jsdom's 1024
  });

  test("arrow keys nudge it, Shift by more", async () => {
    await bootViewer(makeData({ pending: false }));
    const el = divider();
    dragDivider(el, 0, 300);
    nudgeDivider(el, "ArrowRight");
    expect(basis()).toBe("316px");
    nudgeDivider(el, "ArrowLeft", true);
    expect(basis()).toBe("252px");
    // A nudge is a whole gesture, so it stores where a drag stores on
    // release.
    expect(localStorage.getItem("scr-sidebar-width")).toBe("252");
    // And a key the divider has no move for is not one it swallows.
    nudgeDivider(el, "ArrowUp");
    expect(basis()).toBe("252px");
  });

  test("double-clicking hands the width back to the stylesheet", async () => {
    await bootViewer(makeData({ pending: false }));
    const el = divider();
    dragDivider(el, 0, 300);
    el.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    expect(basis()).toBe("");
    expect(localStorage.getItem("scr-sidebar-width")).toBeNull();
  });

  test("the stored width is there on the next boot", async () => {
    await bootViewer(makeData({ pending: false }));
    dragDivider(divider(), 0, 300);

    // A second boot in the same window: the DOM is rebuilt, localStorage
    // is not, which is the reload the reader sees.
    await bootViewer(makeData({ pending: false }));
    expect(basis()).toBe("300px");
    expect(divider().getAttribute("aria-valuenow")).toBe("300");
  });

  test("a stored width wider than the window clamps, and is not rewritten", async () => {
    localStorage.setItem("scr-sidebar-width", "900");
    await bootViewer(makeData({ pending: false }));
    expect(basis()).toBe("410px");
    // The room may come back — a narrow window is not a decision.
    expect(localStorage.getItem("scr-sidebar-width")).toBe("900");
  });
});

describe("overview mode (ADR 0007)", () => {
  const DOC = {
    version: 1,
    base_sha: "b", head_sha: "h",
    verdict: "narrate",
    verdict_note: "a cursor threaded through",
    figure_family: "", cast: [], toy_data: false, dropped_refs: 0,
    sections: [
      // `ready`, so entering the mode does not auto-queue a prose call
      // these cases have no stubbed response for. The auto-queue has its
      // own case below.
      {
        id: "background", kind: "background", title: "Background",
        state: "ready", body: "Ground.", refs: [], map_rows: [], subsections: [],
      },
      {
        id: "map", kind: "map", title: "Map", state: "ready", body: "",
        refs: [{ kind: "file", id: "F0" }],
        map_rows: [{ ref: { kind: "file", id: "F0" }, why: "the contract" }],
        subsections: [],
      },
    ],
  };

  async function bootWithExplainer(
    explainer: { status: number; body: unknown },
    dataOverrides: Partial<ViewerData> = {},
  ): Promise<void> {
    await bootViewer(makeData({ explainer: true, ...dataOverrides }), { explainer });
    await new Promise<void>((r) => setTimeout(r, 0));
  }

  test("the button is absent entirely when the feature is off", async () => {
    await bootViewer(makeData({ explainer: false }));
    expect(document.querySelector(".mode-strip")).toBeNull();
    // And the default-mode logic is inert with it: no document can
    // exist, so the diff is what opens.
    expect(document.querySelector("#app .file")).not.toBeNull();
    expect(window.location.hash).toContain("mode=diff");
  });

  test("the button is disabled until the overview lands, then enabled", async () => {
    await bootWithExplainer({ status: 404, body: {} }, { pending: true });
    const btn = document.getElementById("overview-btn") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    lastEventSource().dispatch("overview", { summary: "s", themes: [], groups: [] });
    await new Promise<void>((r) => setTimeout(r, 0));
    expect((document.getElementById("overview-btn") as HTMLButtonElement).disabled).toBe(false);
  });

  test("pressing with no document POSTs the skeleton and renders the Map", async () => {
    await bootWithExplainer({ status: 404, body: {} }, { pending: false });
    queueFetchResponse({ status: 200, body: DOC });
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(fetchCalls.some((c) => c.url === "/explainer/skeleton")).toBe(true);
    const rows = document.querySelectorAll("#app .explainer-map-row");
    expect(rows).toHaveLength(1);
    expect(rows[0].querySelector(".explainer-ref")!.textContent).toBe("a.py");
    // The diff is gone from the pane; overview mode replaces it.
    expect(document.querySelector("#app .file")).toBeNull();
  });

  test("entering the mode queues every section the skeleton left pending", async () => {
    // Pressing Overview is the decision to spend; a button per section
    // asks twice for one choice.
    const pending = {
      ...DOC,
      sections: DOC.sections.map((s) =>
        s.kind === "map" ? s : { ...s, state: "pending", body: "" }),
    };
    await bootWithExplainer({ status: 200, body: pending }, { pending: false });
    queueFetchResponse({ status: 200, body: pending });
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(fetchCalls.some((c) => c.url === "/explainer/section/background")).toBe(true);
  });

  test("the sidebar swaps to the section tree and back", async () => {
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    const axis = document.querySelector("#group-sidebar .group-axis") as HTMLElement;
    expect(axis.dataset.axis).toBe("explainer");
    expect(Array.from(axis.querySelectorAll(".group-btn-label")).map((e) => e.textContent))
      .toEqual(["Background", "Map"]);

    const btn = document.getElementById("overview-btn") as HTMLButtonElement;
    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector('#group-sidebar .group-axis[data-axis="explainer"]')).toBeNull();

    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector('#group-sidebar .group-axis[data-axis="explainer"]')).not.toBeNull();
  });

  test("the section tree says which section is being written", async () => {
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    const badge = (): Element | null =>
      document.querySelector('#group-sidebar [data-pill-id="background"] .group-btn-count');
    // Written, and citing nothing: no badge beside the title at all.
    expect(badge()).toBeNull();

    lastEventSource().dispatch("explainer", {
      ...DOC,
      sections: DOC.sections.map((s) =>
        s.kind === "background" ? { ...s, state: "pending", body: "" } : s),
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(badge()!.textContent).toBe("…");

    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce((() => new Promise(() => { /* never settles */ })) as unknown as typeof fetch);
    (document.querySelector('#group-sidebar [data-pill-id="background"]') as HTMLElement).click();
    expect(badge()!.textContent).toBe("writing…");
  });

  test("the mode leaves the collapse level and the reviewer's folds alone", async () => {
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    const btn = document.getElementById("overview-btn") as HTMLButtonElement;
    // Down to the diff first: the zoom this is about is one the
    // reviewer sets there.
    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
    const before = window.location.hash;
    expect(before).toContain("fold=off");

    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    // The level is untouched while in the mode; only `mode=` flips.
    expect(window.location.hash).toContain("fold=off");
    expect(window.location.hash).toContain("mode=overview");

    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(window.location.hash).toBe(before);
    expect(document.querySelector('.fold-slider button[data-fold="off"]')!.classList.contains("active"))
      .toBe(true);
  });

  test("the section tree does not touch the diff-mode sidebar pill", async () => {
    localStorage.setItem("scr-active-group:local", "files:BF0");
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    const tree = document.querySelector('#group-sidebar [data-pill-id="background"]') as HTMLElement;
    tree.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(localStorage.getItem("scr-active-group:local")).toBe("files:BF0");
    expect(localStorage.getItem("scr-explainer-section:local")).toBe("explainer:background");
  });

  test("an SSE frame from another tab fills the pane without a POST", async () => {
    await bootWithExplainer({ status: 404, body: {} }, { pending: false });
    lastEventSource().dispatch("explainer", DOC);
    await new Promise<void>((r) => setTimeout(r, 0));
    // The frame does not move the reviewer: they are reading the diff,
    // and the default applies to what the viewer opens with.
    expect(document.querySelector("#app .file")).not.toBeNull();
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelectorAll("#app .explainer-map-row")).toHaveLength(1);
    expect(fetchCalls.some((c) => c.url === "/explainer/skeleton")).toBe(false);
  });

  // --- which mode the viewer opens in --------------------------------

  test("a document already on disk is what the viewer opens on", async () => {
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    expect(document.querySelectorAll("#app .explainer-map-row")).toHaveLength(1);
    expect(document.querySelector("#app .file")).toBeNull();
    expect(
      (document.querySelector("#group-sidebar .group-axis") as HTMLElement).dataset.axis,
    ).toBe("explainer");
    expect(window.location.hash).toContain("mode=overview");
    expect((document.getElementById("overview-btn") as HTMLButtonElement).classList)
      .toContain("active");
    // Opening what is already written buys nothing.
    expect(fetchCalls.some((c) => c.url.startsWith("/explainer/"))).toBe(false);
  });

  test("with no document the diff is still what opens", async () => {
    await bootWithExplainer({ status: 404, body: {} }, { pending: false });
    expect(document.querySelector("#app .file")).not.toBeNull();
    expect(window.location.hash).toContain("mode=diff");
    expect(fetchCalls.some((c) => c.url === "/explainer/skeleton")).toBe(false);
  });

  test("a document whose verdict is not_warranted still counts as one", async () => {
    // The skeleton's answer was "read the hunks directly" — which is the
    // document, so it is what a reviewer re-opening the run should meet.
    await bootWithExplainer(
      { status: 200, body: { ...DOC, verdict: "not_warranted", sections: [] } },
      { pending: false },
    );
    expect(window.location.hash).toContain("mode=overview");
    expect(document.querySelector("#app .explainer")).not.toBeNull();
  });

  test("mode=diff in the URL outranks the document", async () => {
    // A reload of a URL the reviewer was reading the diff on: the hash
    // says which mode they chose, so the default does not apply.
    // replaceState, not an assignment to `location.hash`: the latter
    // fires `hashchange` at the listeners every earlier boot in this
    // file left on the shared window, and they repaint #app.
    window.history.replaceState(null, "", "#fold=off&mode=diff");
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    expect(document.querySelector("#app .file")).not.toBeNull();
    expect(document.querySelector("#app .explainer")).toBeNull();
    expect(window.location.hash).toContain("fold=off");
    expect(window.location.hash).toContain("mode=diff");
  });

  test("mode=overview in the URL restores the mode and the level under it", async () => {
    window.history.replaceState(null, "", "#fold=off&mode=overview");
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    expect(document.querySelectorAll("#app .explainer-map-row")).toHaveLength(1);
    expect(window.location.hash).toContain("fold=off");
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector('.fold-slider button[data-fold="off"]')!.classList)
      .toContain("active");
  });

  test("opening into a half-written document queues what it left pending", async () => {
    // The spend was authorised when the document was generated; landing
    // in it is not a second decision, and leaving prose unwritten behind
    // per-section buttons is not what the reviewer asked for.
    const half = {
      ...DOC,
      sections: DOC.sections.map((s) =>
        s.kind === "map" ? s : { ...s, state: "pending", body: "" }),
    };
    await bootWithExplainer({ status: 200, body: half }, { pending: false });
    queueFetchResponse({ status: 200, body: half });
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(fetchCalls.some((c) => c.url === "/explainer/section/background")).toBe(true);
    expect(fetchCalls.some((c) => c.url === "/explainer/skeleton")).toBe(false);
  });

  // --- the fold slider, from inside the document -----------------------

  describe("picking a collapse level leaves the document", () => {
    /** Boot into the document, with the diff waiting behind it. */
    async function bootIntoDocument(): Promise<void> {
      await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
      expect(document.querySelector("#app .explainer")).not.toBeNull();
    }

    /** What the pane, the hash and the sidebar say after the exit. */
    function expectDiffAt(level: string): void {
      expect(window.location.hash).toContain(`fold=${level}`);
      expect(window.location.hash).toContain("mode=diff");
      expect(document.querySelector("#app .explainer")).toBeNull();
      expect(document.querySelector("#app .file")).not.toBeNull();
      expect(document.querySelector('#group-sidebar .group-axis[data-axis="explainer"]')).toBeNull();
    }

    test("a slider click lands in the diff at the level it names", async () => {
      await bootIntoDocument();
      (document.querySelector('.fold-slider button[data-fold="segments"]') as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expectDiffAt("segments");
      expect(document.querySelector("#app .explainer-detail")).toBeNull();
      expect(document.querySelector('.fold-slider button[data-fold="segments"]')!.classList)
        .toContain("active");
      expect(Array.from((document.getElementById("overview-btn") as HTMLButtonElement).classList))
        .not.toContain("active");
    });

    test("keys 1-4 do the same", async () => {
      // No assertion on the mode button here: the keydown listener is on
      // `document`, which every earlier boot in this file shares, and one
      // of them was booted with the feature off — its handler removes the
      // mode strip.
      await bootIntoDocument();
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "2" }));
      await new Promise<void>((r) => setTimeout(r, 0));
      expectDiffAt("hunks");
    });

    test("the mode is still there to go back into", async () => {
      await bootIntoDocument();
      (document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));

      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect(document.querySelectorAll("#app .explainer-map-row")).toHaveLength(1);
      expect(window.location.hash).toContain("mode=overview");
      // And the level the press picked is what the diff is waiting at.
      expect(window.location.hash).toContain("fold=off");
    });

    test("the slider says where a press lands while the pane is the document", async () => {
      await bootIntoDocument();
      const off = document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement;
      expect(off.title).toBe("Leave the document and read the diff at this level");
      // The level highlight is the level a press lands on, so it stays.
      expect(document.querySelector('.fold-slider button[data-fold="hunks"]')!.classList)
        .toContain("active");

      // Leaving restores the markup's own title — empty in this
      // harness's header, which is what says the sentence came off.
      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect((document.querySelector('.fold-slider button[data-fold="off"]') as HTMLElement).title)
        .toBe("");
    });
  });

  // --- the detail panel ------------------------------------------------

  describe("a reference opens beside the document", () => {
    const FILES = [
      ...["a.py", "b.py"].map((path, i) => ({
        id: `F${i}`, path, status: "modified", language: "python",
        adds: 1, dels: 1, summary: "", head_lines: null,
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock(`H${i}_0`, "guards the path")],
      })),
      makeOneSidedFile(2, "c.py", "added"),
    ];

    // Two file references to swap between, and two inline hunk
    // references — the case that has to arrive unfolded.
    const PANEL_DOC = {
      ...DOC,
      sections: [
        { ...DOC.sections[0], body: "Ground. The guard is [H0_0], and [H2_0] is new." },
        {
          ...DOC.sections[1],
          refs: [{ kind: "file", id: "F0" }, { kind: "file", id: "F1" }],
          map_rows: [
            { ref: { kind: "file", id: "F0" }, why: "the contract" },
            { ref: { kind: "file", id: "F1" }, why: "the caller" },
          ],
        },
      ],
    };

    /** Boot into the document with the panel mounted and closed. */
    async function bootWithPanel(): Promise<HTMLElement> {
      await bootWithExplainer({ status: 200, body: PANEL_DOC }, { pending: false, files: FILES });
      return document.querySelector("#app .explainer-detail") as HTMLElement;
    }

    /** The Nth Map row's reference button, re-queried: a repaint of the
     *  document builds a new one. */
    function mapRow(n: number): HTMLElement {
      return document.querySelectorAll<HTMLElement>(".explainer-map-row .explainer-ref")[n];
    }

    /** The Nth inline hunk reference: 0 addresses the modified file's
     *  hunk, 1 the added file's. */
    function hunkRef(n = 0): HTMLElement {
      return document.querySelectorAll<HTMLElement>("#app .explainer-arrow")[n];
    }

    test("the file opens in the panel, and the document is not rebuilt", async () => {
      const panel = await bootWithPanel();
      const documentEl = document.querySelector("#app .explainer")!;
      expect(panel.hidden).toBe(true);

      mapRow(0).click();

      expect(panel.hidden).toBe(false);
      expect(panel.querySelector('.file[data-id="F0"]')).not.toBeNull();
      expect(panel.querySelector(".explainer-detail-path")!.textContent).toBe("a.py");
      // The same node, so the reader's scroll position and everything
      // else about their place in the prose survives.
      expect(document.querySelector("#app .explainer")).toBe(documentEl);
      expect(window.location.hash).toContain("mode=overview");
      expect(mapRow(0).classList.contains("explainer-ref-open")).toBe(true);
    });

    test("a hunk reference arrives unfolded, and leaves the diff's folds alone", async () => {
      const panel = await bootWithPanel();
      hunkRef().click();

      const hunk = panel.querySelector('.hunk[data-id="H0_0"]') as HTMLElement;
      expect(hunk.classList.contains("folded")).toBe(false);
      // Code, not the segment summary the `hunks` level would show.
      expect(hunk.querySelector(".diff")).not.toBeNull();

      // The panel's overrides are its own: the diff is where the
      // reviewer left it, which is what makes the return trip free.
      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect(document.querySelector('.hunk[data-id="H0_0"]')!.classList.contains("folded"))
        .toBe(true);
    });

    test("an added file spends the panel on the half that has content", async () => {
      // The panel is the surface where the dead half costs most: the
      // grid is the shared renderer's, so the collapse arrives with it.
      const panel = await bootWithPanel();
      hunkRef(1).click();

      const diff = panel.querySelector(".diff") as HTMLElement;
      expect(diff.classList.contains("diff-only-new")).toBe(true);
      expect(diff.querySelector(".half-old")).not.toBeNull();
      expect(diff.querySelectorAll(".half-new .row")).toHaveLength(2);
    });

    test("the split packs against the panel only while it is open", async () => {
      // The stylesheet keys the document cell's shrink-wrap off this
      // class: centred beside an open panel, the prose sat in a dead
      // zone on a wide window.
      const panel = await bootWithPanel();
      const split = document.querySelector("#app .explainer-split") as HTMLElement;
      expect(split.classList.contains("panel-open")).toBe(false);

      mapRow(0).click();
      expect(split.classList.contains("panel-open")).toBe(true);

      (panel.querySelector(".explainer-detail-close") as HTMLElement).click();
      expect(split.classList.contains("panel-open")).toBe(false);
    });

    test("a second reference swaps the panel in place", async () => {
      const panel = await bootWithPanel();
      mapRow(0).click();
      mapRow(1).click();

      expect(document.querySelector("#app .explainer-detail")).toBe(panel);
      expect(panel.querySelectorAll(".file")).toHaveLength(1);
      expect(panel.querySelector('.file[data-id="F1"]')).not.toBeNull();
      expect(panel.querySelector(".explainer-detail-path")!.textContent).toBe("b.py");
      expect(mapRow(0).classList.contains("explainer-ref-open")).toBe(false);
      expect(mapRow(1).classList.contains("explainer-ref-open")).toBe(true);
    });

    test("the close button and Esc both close it", async () => {
      const panel = await bootWithPanel();
      mapRow(0).click();
      (panel.querySelector(".explainer-detail-close") as HTMLElement).click();
      expect(panel.hidden).toBe(true);
      expect(document.querySelector(".explainer-ref-open")).toBeNull();
      expect(document.querySelector("#app .explainer")).not.toBeNull();

      mapRow(0).click();
      expect(panel.hidden).toBe(false);
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      expect(panel.hidden).toBe(true);
    });

    test("Open in diff leaves the mode and lands on the file", async () => {
      const panel = await bootWithPanel();
      mapRow(1).click();
      (panel.querySelector(".explainer-detail-open") as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));

      expect(window.location.hash).toContain("mode=diff");
      expect(document.querySelector("#app .explainer")).toBeNull();
      expect(document.querySelector("#app .explainer-detail")).toBeNull();
      expect(document.querySelector('#app .file[data-id="F1"]')).not.toBeNull();
    });

    test("a section write repaints the document under a panel that stays", async () => {
      const panel = await bootWithPanel();
      mapRow(0).click();
      const fileEl = panel.querySelector(".file");

      lastEventSource().dispatch("explainer", {
        ...PANEL_DOC,
        sections: PANEL_DOC.sections.map((s) =>
          s.kind === "background" ? { ...s, body: "Ground, rewritten. [H0_0]" } : s),
      });
      await new Promise<void>((r) => setTimeout(r, 0));

      expect(document.querySelector("#app .explainer")!.textContent)
        .toContain("Ground, rewritten.");
      expect(document.querySelector("#app .explainer-detail")).toBe(panel);
      expect(panel.hidden).toBe(false);
      expect(panel.querySelector(".file")).toBe(fileEl);
      expect(document.querySelector("#app .explainer-split")!.classList.contains("panel-open"))
        .toBe(true);
      // The chips were rebuilt with the prose; the mark goes back on.
      expect(mapRow(0).classList.contains("explainer-ref-open")).toBe(true);
    });

    test("a repaint from the writing ticker leaves the panel where it is", async () => {
      const panel = await bootWithPanel();
      mapRow(0).click();
      const fileEl = panel.querySelector(".file");

      // Background back to pending, then asked for: the pane repaints on
      // a timer for as long as the POST runs, which is minutes.
      lastEventSource().dispatch("explainer", {
        ...PANEL_DOC,
        sections: PANEL_DOC.sections.map((s) =>
          s.kind === "background" ? { ...s, state: "pending", body: "" } : s),
      });
      await new Promise<void>((r) => setTimeout(r, 0));

      const status = (): string =>
        document.querySelector('#app .explainer [data-section-id="background"] .explainer-status')!
          .textContent || "";
      vi.useFakeTimers();
      try {
        (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
          .mockImplementationOnce((() => new Promise(() => { /* never settles */ })) as unknown as typeof fetch);
        (document.querySelector('#group-sidebar [data-pill-id="background"]') as HTMLElement).click();
        expect(status()).toBe("Writing this section…");
        vi.advanceTimersByTime(2 * 60000);
        // The figure moved, so the ticker's repaint reached the pane.
        expect(status()).toBe("Writing this section… 2 min");
      } finally {
        vi.useRealTimers();
      }

      expect(document.querySelector("#app .explainer-detail")).toBe(panel);
      expect(panel.hidden).toBe(false);
      expect(panel.querySelector(".file")).toBe(fileEl);
    });

    test("a comment written in the panel round-trips", async () => {
      const panel = await bootWithPanel();
      hunkRef().click();

      const cell = panel.querySelector(".half-new .row .cell-lineno") as HTMLElement;
      expect(cell.textContent).toBe("1");
      cell.click();
      const ta = document.querySelector<HTMLTextAreaElement>(".comment-editor-input")!;
      expect(panel.contains(ta)).toBe(true);
      ta.value = "this guard is new";

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
      expect(posted!.file).toBe("a.py");
      expect(posted!.side).toBe("new");
      expect(posted!.line).toBe(1);
      expect(panel.querySelector(".comment-thread-entry")!.textContent)
        .toContain("this guard is new");

      // And it comes back with the file: mounting panel content replays
      // the store's threads over it, as a diff render does.
      (panel.querySelector(".explainer-detail-close") as HTMLElement).click();
      hunkRef().click();
      expect(panel.querySelector(".comment-thread-entry")!.textContent)
        .toContain("this guard is new");
    });

    // --- how wide the panel is ------------------------------------------
    // jsdom has no layout, so these read the shipped rules off the
    // cascade: the claim is which sizing the panel and the document cell
    // ask for, not the pixels they land on.

    test("the open panel takes the remainder, down to a floor", async () => {
      const panel = await bootWithPanel();
      installStylesheet();
      mapRow(0).click();

      const cs = getComputedStyle(panel);
      // Where the split divides is the reader's, so the panel has no
      // width of its own to work out: it runs from the boundary to the
      // window edge.
      expect(cs.flex).toBe("1 1 0px");
      expect(cs.minWidth).toBe("380px");
    });

    test("the document cell holds its measure until the reader says otherwise", async () => {
      const panel = await bootWithPanel();
      installStylesheet();
      const doc = document.querySelector("#app .explainer-doc") as HTMLElement;
      expect(getComputedStyle(doc).flex).toBe("1 1 0px");   // closed: the whole pane

      mapRow(0).click();
      // Packed against the panel, the cell shrink-wraps its own
      // max-width box; the prose keeps the 72ch measure inside it.
      expect(getComputedStyle(doc).flex).toBe("0 1 auto");
      expect(getComputedStyle(document.querySelector("#app .explainer") as HTMLElement).maxWidth)
        .toBe("calc(72ch + 64px)");
      expect(getComputedStyle(panel).minWidth).toBe("380px");
    });

    test("prose in the panel runs at a measure, so the width is the code's", async () => {
      // The panel is as wide as the reader left the boundary, and a
      // one-sentence summary or intent set across all of that reads as a
      // banner rather than as a note.
      await bootWithPanel();
      installStylesheet();
      mapRow(0).click();
      const capOf = (sel: string): string => getComputedStyle(
        document.querySelector(`#app .explainer-detail-body ${sel}`) as HTMLElement).maxWidth;
      expect(capOf(".file-summary")).toBe("64ch");
      expect(capOf(".hunk-intent")).toBe("64ch");

      // Scoped to the panel: the diff pane's own prose has the page's
      // width to answer to, so the same prose is uncapped there.
      (document.querySelector(".explainer-detail-open") as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect(getComputedStyle(
        document.querySelector("#app .file .file-summary") as HTMLElement).maxWidth).toBe("");
    });

    // --- where the reader puts the boundary -----------------------------
    // The document's ceiling is measured off the split, which jsdom
    // reports as 0 wide; the cases that assert a number stub that one
    // reading, the way the annotation cases inject rects.

    function docDivider(): HTMLElement {
      return document.querySelector("#app .explainer-split .layout-divider-doc") as HTMLElement;
    }

    function docCell(): HTMLElement {
      return document.querySelector("#app .explainer-doc") as HTMLElement;
    }

    /** Give the split a width to divide. */
    function splitWidth(px: number): void {
      Object.defineProperty(
        document.querySelector("#app .explainer-split") as HTMLElement,
        "clientWidth", { value: px, configurable: true },
      );
    }

    test("the boundary is a separator between the document and the panel", async () => {
      await bootWithPanel();
      const el = docDivider();
      expect(el.getAttribute("role")).toBe("separator");
      expect(el.getAttribute("aria-orientation")).toBe("vertical");
      expect(el.tabIndex).toBe(0);
      expect(el.previousElementSibling!.className).toBe("explainer-doc");
      expect(el.nextElementSibling!.classList.contains("explainer-detail")).toBe(true);
      // And the sidebar's is still there: the mode replaced the pane, not
      // the shell around it.
      expect(document.querySelector(".layout > .layout-divider-sidebar")).not.toBeNull();
    });

    test("dragging it widens the column, and the prose follows", async () => {
      await bootWithPanel();
      installStylesheet();
      mapRow(0).click();
      splitWidth(1600);

      dragDivider(docDivider(), 0, 620);
      expect(docCell().style.width).toBe("620px");
      expect(localStorage.getItem("scr-explainer-doc-width")).toBe("620");
      // The measure was the default, not a ceiling: the text takes the
      // column it was given, its padding unchanged.
      expect(getComputedStyle(document.querySelector("#app .explainer") as HTMLElement).maxWidth)
        .toBe("100%");
      expect(getComputedStyle(document.querySelector("#app .explainer") as HTMLElement).padding)
        .toBe("36px 32px 96px");
    });

    test("the drag stops at the column's floor and at the panel's", async () => {
      await bootWithPanel();
      mapRow(0).click();
      splitWidth(1600);

      dragDivider(docDivider(), 0, 100);
      expect(docCell().style.width).toBe("340px");
      dragDivider(docDivider(), 0, 5000);
      expect(docCell().style.width).toBe("1212px");   // 1600 - 380 - 8
    });

    test("arrow keys nudge the boundary", async () => {
      await bootWithPanel();
      mapRow(0).click();
      splitWidth(1600);
      const el = docDivider();

      dragDivider(el, 0, 620);
      nudgeDivider(el, "ArrowLeft");
      expect(docCell().style.width).toBe("604px");
      nudgeDivider(el, "ArrowRight", true);
      expect(docCell().style.width).toBe("668px");
      expect(localStorage.getItem("scr-explainer-doc-width")).toBe("668");
    });

    test("double-clicking hands the column back to the measure", async () => {
      await bootWithPanel();
      installStylesheet();
      mapRow(0).click();
      splitWidth(1600);
      const el = docDivider();
      dragDivider(el, 0, 620);

      el.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
      expect(docCell().style.width).toBe("");
      expect(docCell().classList.contains("explainer-doc-sized")).toBe(false);
      expect(getComputedStyle(document.querySelector("#app .explainer") as HTMLElement).maxWidth)
        .toBe("calc(72ch + 64px)");
      expect(localStorage.getItem("scr-explainer-doc-width")).toBeNull();
    });

    test("the stored column is applied when the mode paints, clamped to the room", async () => {
      localStorage.setItem("scr-explainer-doc-width", "620");
      await bootWithPanel();
      // A split that reports no width has none to give: the column
      // arrives at its floor rather than overflowing.
      expect(docCell().classList.contains("explainer-doc-sized")).toBe(true);
      expect(docCell().style.width).toBe("340px");

      // The room comes back — a narrow window was never a decision, so
      // the stored number was not rewritten to the clamp.
      splitWidth(1600);
      window.dispatchEvent(new Event("resize"));
      expect(docCell().style.width).toBe("620px");
      expect(localStorage.getItem("scr-explainer-doc-width")).toBe("620");
    });
  });
});
