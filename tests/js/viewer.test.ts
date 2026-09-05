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

/** Queue the `/file-text` payload an expand chip (or the md toggle)
 *  fetches for file `idx`. Sides are whole-file text, null for a side the
 *  file has no version on. */
function queueFileText(idx: number, path: string, base: string | null, head: string | null): void {
  queueFetchResponse({ status: 200, body: { file_idx: idx, path, base, head } });
}

/** Let a fetch promise chain settle (a chip's expand, a toggle's flip). */
function tick(): Promise<void> {
  return new Promise<void>((r) => setTimeout(r, 0));
}

/** "l1".."l9", one per line — the text behind the nine-line fixtures. */
const nineLines = Array.from({ length: 9 }, (_, i) => `l${i + 1}`).join("\n") + "\n";

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
        <button data-fold="code"></button>
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

/** The `+N` / `-M` texts of a Files-axis pill's line-count badge. */
function lineCounts(pill: Element): [string, string] {
  const badge = pill.querySelector(".group-btn-count.group-btn-lines");
  if (!badge) throw new Error("pill has no line-count badge");
  return [badge.querySelector(".adds")!.textContent!, badge.querySelector(".dels")!.textContent!];
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
    spans: [],
    rows: [
      { kind: "pair", old_line: 1, new_line: 1, old_text: "a", new_text: "a" },
      { kind: "pair", old_line: 2, new_line: 2, old_text: "b", new_text: "B" },
    ],
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
    summary: "", head_line_count: null,
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
    pr: { title: "test", themes: [], callgraph_edges: [] },
    smells_catalogue: {},
    files: [{
      id: "F0",
      path: "a.py",
      status: "modified",
      language: "python",
      adds: 1, dels: 1,
      summary: "",
      symbols: { added: [], modified: [], removed: [] },
      head_line_count: null,
      hunks: [makeHunkBlock("H0_0")],
    }],
    groups: [],
    symbols: [],
    ...overrides,
  };
}

// --- Page-level listeners --------------------------------------------------
// Every test evals its own copy of the bundle, and each copy binds page-
// level handlers — render.ts's keydown on `document`, console.ts's on
// `window`. Wiping innerHTML detaches the DOM a past copy was built over
// but not its handlers, so one keypress in a later test runs every
// earlier boot's handler too; the ones that read a surface off the live
// document by id (the help overlay) then act on it, and a layered
// dismissal appears to skip a layer. Record what each boot binds and
// unbind it after the test.

interface BoundListener {
  target: EventTarget;
  type: string;
  fn: EventListenerOrEventListenerObject;
  opts: boolean | AddEventListenerOptions | undefined;
}
const boundListeners: BoundListener[] = [];
const realAddEventListener: Array<[EventTarget, EventTarget["addEventListener"]]> = [];

function recordPageListeners(): void {
  for (const target of [document, window] as EventTarget[]) {
    const real = target.addEventListener.bind(target);
    realAddEventListener.push([target, real]);
    target.addEventListener = ((
      type: string,
      fn: EventListenerOrEventListenerObject,
      opts?: boolean | AddEventListenerOptions,
    ) => {
      boundListeners.push({ target, type, fn, opts });
      real(type, fn, opts);
    }) as EventTarget["addEventListener"];
  }
}

function dropPageListeners(): void {
  for (const [target, real] of realAddEventListener) target.addEventListener = real;
  realAddEventListener.length = 0;
  for (const l of boundListeners) l.target.removeEventListener(l.type, l.fn, l.opts);
  boundListeners.length = 0;
}

// --- Global hooks ----------------------------------------------------------

beforeEach(() => {
  recordPageListeners();
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
  dropPageListeners();
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});


describe("pending boot", () => {
  test("progress strip shows total + every square starts queued", async () => {
    await bootViewer(makeData({
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_line_count: null,
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
          adds: 0, dels: 0, summary: "", head_line_count: null,
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0"), makeHunkBlock("H0_1")],
        },
        {
          id: "F1", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_line_count: null,
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
        adds: 0, dels: 0, summary: "", head_line_count: null,
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
      pr: { summary: "bumps return values", themes: ["constants"], callgraph_edges: [] },
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
          adds: 9, dels: 1, summary: "", head_line_count: null,
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0", "alpha"), makeHunkBlock("H0_1", "beta")],
        },
        {
          id: "F1", path: "b.py", status: "modified", language: "python",
          adds: 0, dels: 4, summary: "", head_line_count: null,
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
    // A file pill is sized by changed lines (matching its file header),
    // not by hunk count; a zero side still renders.
    expect(pills[0].textContent).toContain("a.py");
    expect(lineCounts(pills[0])).toEqual(["+9", "-1"]);
    expect(pills[1].textContent).toContain("b.py");
    expect(lineCounts(pills[1])).toEqual(["+0", "-4"]);
    expect(document.querySelector('.file[data-id="F0"] .file-meta')!.textContent).toBe("+9-1");

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
    const mkFile = (id: string, p: string, hid: string, adds: number, dels: number): Record<string, unknown> => ({
      id, path: p, status: "modified", language: "python",
      adds, dels, summary: "", head_line_count: null,
      symbols: { added: [], modified: [], removed: [] },
      hunks: [makeHunkBlock(hid)],
    });
    await bootViewer(makeData({
      pending: false,
      files: [
        // src/ holds two files → an interior "src" node with two leaves.
        mkFile("F0", "src/b.py", "Hb", 5, 2),
        mkFile("F1", "src/a.py", "Ha", 10, 3),
        // docs/guide/ is a single-child chain → compressed to "docs/guide".
        mkFile("F2", "docs/guide/intro.md", "Hi", 1, 0),
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

    // "src" holds two leaves, sorted a.py before b.py; its pill is the
    // subtree's changed-line sum (its hunk_ids stay the subtree union).
    const srcNode = roots[1];
    const srcPill = srcNode.querySelector(":scope > .group-tree-row > .group-btn") as HTMLElement;
    expect(lineCounts(srcPill)).toEqual(["+15", "-5"]);
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

  test("by-file axis line counts: a directory sums every descendant, not just its direct files", async () => {
    const mkFile = (id: string, p: string, adds: number, dels: number): Record<string, unknown> => ({
      id, path: p, status: "modified", language: "python",
      adds, dels, summary: "", head_line_count: null,
      symbols: { added: [], modified: [], removed: [] },
      hunks: [makeHunkBlock(`H${id.slice(1)}_0`), makeHunkBlock(`H${id.slice(1)}_1`)],
    });
    await bootViewer(makeData({
      pending: false,
      groups: [{ id: "G0", title: "theme", rationale: "", hunk_ids: ["H0_0", "H1_0", "H2_1"] }],
      files: [
        // pkg/ holds one file directly and two more beneath pkg/sub/, so
        // the pkg total differs from the sum over its direct files (7/1).
        mkFile("F0", "pkg/top.py", 7, 1),
        mkFile("F1", "pkg/sub/x.py", 20, 0),
        mkFile("F2", "pkg/sub/y.py", 0, 6),
        mkFile("F3", "other.py", 2, 2),
      ],
    }));
    const pillFor = (title: string): Element => {
      const label = Array.from(document.querySelectorAll('[data-axis="files"] .group-btn-label'))
        .find((e) => e.textContent === title);
      if (!label) throw new Error(`no Files pill titled ${title}`);
      return label.closest(".group-btn")!;
    };
    // Leaves equal their file's header counts.
    expect(lineCounts(pillFor("top.py"))).toEqual(["+7", "-1"]);
    expect(lineCounts(pillFor("x.py"))).toEqual(["+20", "-0"]);
    // pkg/sub sums its two files; pkg sums pkg/sub plus its own file.
    expect(lineCounts(pillFor("sub"))).toEqual(["+20", "-6"]);
    expect(lineCounts(pillFor("pkg"))).toEqual(["+27", "-7"]);
    // The roots together account for every file: pkg (27/7) + other.py (2/2).
    const roots = document.querySelectorAll('[data-axis="files"] > .group-tree-node > .group-tree-row .group-btn');
    expect(Array.from(roots).map(lineCounts)).toEqual([["+2", "-2"], ["+27", "-7"]]);
    // The Files pill is the one that changed: Themes still counts hunks.
    const themePill = document.querySelector('[data-axis="themes"] .group-btn')!;
    expect(themePill.querySelector(".group-btn-count")!.textContent).toBe("3");
    expect(themePill.querySelector(".group-btn-lines")).toBeNull();
    // The directory pill still filters to its whole subtree.
    (pillFor("pkg") as HTMLElement).click();
    expect(document.querySelector('.hunk[data-id="H2_1"]')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H3_0"]')).toBeNull();
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
        head_line_count: 9,
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
    queueFileText(0, "a.py", null, nineLines);
    between.click();
    await tick();
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
        adds: 0, dels: 0, summary: "", head_line_count: null,
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
    window.location.hash = "#fold=code"; // expand hunks so diff bodies render
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_line_count: null,
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
        adds: 0, dels: 0, summary: "", head_line_count: null,
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
    head_line_count: 9,
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

  test("the ladder is files | hunks | code: only 'code' shows rows", async () => {
    await bootViewer(makeData({ pending: false, files: [foldFile()], symbols: [] }));
    expect(document.querySelectorAll(".fold-slider button").length).toBe(3);

    // Default "hunks": headers only, no code.
    expect(document.querySelectorAll(".hunk-header").length).toBe(3);
    expect(codeRows(".hunk")).toBe(0);

    // "code": every hunk is open to its rows.
    fold("code");
    expect(window.location.hash).toContain("fold=code");
    expect(codeRows(".hunk")).toBe(3);

    // "hunks": back to headers only; "files": headers of files only.
    fold("hunks");
    expect(codeRows(".hunk")).toBe(0);
    fold("files");
    expect(document.querySelectorAll(".hunk-header").length).toBe(0);
    expect(document.querySelector(".file")!.classList.contains("folded")).toBe(true);
  });

  test("key 3 selects 'code'; there is no key 4", async () => {
    await bootViewer(makeData({ pending: false, files: [foldFile()], symbols: [] }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "3" }));
    expect(window.location.hash).toContain("fold=code");
    expect(document.querySelector('.fold-slider button[data-fold="code"]')!.classList).toContain("active");
    expect(codeRows(".hunk")).toBe(3);

    fold("hunks");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "4" }));
    expect(window.location.hash).toContain("fold=hunks");
    expect(codeRows(".hunk")).toBe(0);
  });

  test.each(["definitions", "segments", "off"])("an old 'fold=%s' hash lands on 'code' and is rewritten", async (old) => {
    window.location.hash = `#fold=${old}`;
    await bootViewer(makeData({ pending: false, files: [foldFile()], symbols: [] }));
    expect(window.location.hash).toContain("fold=code");
    expect(window.location.hash).not.toContain(old);
    expect(document.querySelector('.fold-slider button[data-fold="code"]')!.classList).toContain("active");
    expect(codeRows(".hunk")).toBe(3);
  });

  /** A file whose one hunk adds two functions inside a class, with the
   *  class itself reaching past the hunk on both ends (its opener at
   *  line 1 is undisclosed), and a module-level span outside the class.
   *  The regions are the server's, enclosing first. */
  const spansFile = (spans: Record<string, unknown>[]): Record<string, unknown> => {
    const rows = [4, 5, 6, 7, 8, 9, 10, 11, 12].map((n) =>
      ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text:
        n === 5 ? "    def alpha(self):" : n === 8 ? "    def beta(self):" : n === 12 ? "X = 1" : `        l${n}` }));
    rows[0] = { kind: "ctx", old_line: 3, new_line: 4, old_text: "    pass", new_text: "    pass" };
    const region = (over: Record<string, unknown>): Record<string, unknown> => ({
      context: "right", left_start: null, left_end: null, has_changes: true, summary: "", ...over,
    });
    return {
      id: "F0", path: "a.py", status: "modified", language: "python",
      adds: 8, dels: 0, summary: "", head_line_count: 20,
      symbols: { added: [], modified: [], removed: [] },
      fold_regions: [
        region({ right_start: 1, right_end: 11, qualified_name: "Foo", kind: "class" }),
        region({ right_start: 5, right_end: 7, qualified_name: "Foo.alpha", kind: "method" }),
        region({ right_start: 8, right_end: 11, qualified_name: "Foo.beta", kind: "method" }),
      ],
      hunks: [makeHunkBlock("H0_0", "adds two methods", {
        old_start: 3, old_count: 1, new_start: 4, new_count: 9, rows, spans,
        smells: [{ tag: "long-method", note: "" }], context: "why the hunk", refs: [],
      })],
    };
  };
  const span = (id: string, start: number, end: number, intent: string): Record<string, unknown> =>
    ({ id, start, end, intent, smells: [], context: "", refs: [] });
  const SPANS = [
    span("H0_0:span:5-7", 5, 7, "alpha does A"),
    span("H0_0:span:9-10", 9, 10, "beta's guard"),
    span("H0_0:span:10-10", 10, 10, "callout"),
    span("H0_0:span:12-12", 12, 12, "module constant"),
  ];

  test("at 'hunks' a hunk is its header and intent; nothing mentions a span", async () => {
    await bootViewer(makeData({ pending: false, files: [spansFile(SPANS)], symbols: [] }));
    const hunk = document.querySelector(".hunk")!;
    expect(hunk.querySelector(".hunk-pos")!.textContent).toBe("@@ -1,2 +1,2 @@");
    expect(hunk.querySelector(".hunk-intent")!.textContent).toBe("adds two methods");
    expect(hunk.querySelector(".hunk-header .smell")!.textContent).toBe("long-method");
    expect(hunk.querySelector(".hunk-header .context-icon")).not.toBeNull();
    // No span text, range, bracket, note or tree anywhere in the hunk.
    const text = hunk.textContent!;
    for (const s of SPANS) {
      expect(text).not.toContain(s.intent as string);
      expect(text).not.toContain(`+${s.start}`);
    }
    expect(hunk.querySelector(".span-mark, .span-text, .label-tree, .label-row")).toBeNull();
    expect(codeRows(".hunk")).toBe(0);
  });

  test("at 'code' a hunk's spans are marks and text blocks in the gutter; the definitions are chevrons", async () => {
    await bootViewer(makeData({ pending: false, files: [spansFile(SPANS)], symbols: [] }));
    fold("code");
    expect(codeRows(".hunk")).toBe(9);
    // Multi-line spans bar, single-line spans dot; every span has a text
    // block in the gutter and nothing in the code column; no tree until a
    // definition is collapsed.
    expect(document.querySelectorAll('.span-mark[data-span-id="H0_0:span:5-7"]').length).toBe(3);
    expect(document.querySelectorAll('.span-mark[data-span-id="H0_0:span:9-10"]').length).toBe(2);
    expect(document.querySelectorAll(".half-new > .span-text").length).toBe(4);
    expect(document.querySelectorAll(".span-dot").length).toBe(2);
    expect(document.querySelectorAll(".row-annotation[data-span-id]").length).toBe(0);
    // The label trees exist only inside the (hidden) fold boxes.
    for (const tree of document.querySelectorAll<HTMLElement>(".label-tree")) {
      expect((tree.closest(".row-annotation") as HTMLElement).style.display).toBe("none");
    }
    // One chevron per definition with two or more rows on screen.
    expect(document.querySelectorAll(".fold-chev").length).toBe(3);
  });

  test("a hunk touching no definition has no fold: its spans are on the code alone", async () => {
    const rows = [1, 2, 3, 4, 5, 6].map((n) =>
      ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text: `l${n}` }));
    const file = {
      id: "F0", path: "a.py", status: "modified", language: "python",
      adds: 6, dels: 0, summary: "", head_line_count: 6,
      symbols: { added: [], modified: [], removed: [] },
      fold_regions: [],
      hunks: [makeHunkBlock("H0_0", "whole hunk", {
        old_start: 0, old_count: 0, new_start: 1, new_count: 6, rows,
        spans: [
          span("H0_0:span:1-4", 1, 4, "region"),
          span("H0_0:span:2-3", 2, 3, "inner"),
          span("H0_0:span:3-3", 3, 3, "callout"),
          span("H0_0:span:6-6", 6, 6, "tail note"),
        ],
      })],
    };
    await bootViewer(makeData({ pending: false, files: [file], symbols: [] }));
    expect(document.querySelector(".hunk")!.textContent).not.toContain("region");

    fold("code");
    expect(document.querySelectorAll(".fold-chev").length).toBe(0);
    expect(document.querySelector(".label-tree")).toBeNull();
    // Bars over exactly their code rows (the callout's note row at 3 gets
    // its own segment of the bars running through it).
    const codeMarks = (id: string): number =>
      document.querySelectorAll(`.row:not(.row-annotation) > .cell-gutter-bars > .span-mark[data-span-id="${id}"]`).length;
    expect(codeMarks("H0_0:span:1-4")).toBe(4);
    expect(codeMarks("H0_0:span:2-3")).toBe(2);
    expect(document.querySelector('.span-text[data-span-id="H0_0:span:1-4"] .span-text-intent')!.textContent).toBe("region");
    expect(document.querySelector('.span-text[data-span-id="H0_0:span:3-3"] .span-text-intent')!.textContent).toBe("callout");
    expect(document.querySelectorAll(".row-annotation").length).toBe(0);
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

    // "code" shows its code again.
    fold("code");
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

  // --- Focus is the one reveal that unfolds (ADR 0008 slice 6) -----------

  test("expanding a chip reveals rows at the current depth and does not unfold", async () => {
    await bootViewer(makeData({ pending: false, files: [foldFile()], symbols: [] }));
    expect(window.location.hash).toContain("fold=hunks");
    const chip = document.querySelector(".gap-chip") as HTMLElement;
    queueFileText(0, "a.py", null, nineLines);
    chip.click();
    await tick();

    // The context is on screen; the hunks stay folded to their headers,
    // the level is unchanged and nothing was written to the hash.
    expect(document.querySelector(".gap-expansion")).not.toBeNull();
    expect(document.querySelectorAll(".hunk-header").length).toBe(3);
    expect(codeRows(".hunk")).toBe(0);
    expect(document.querySelectorAll(".hunk.folded").length).toBe(3);
    expect(window.location.hash).toBe("#fold=hunks&mode=diff");
  });

  test("a pill click is a focus: it opens the file and the hunk's code, even at 'files'", async () => {
    await bootViewer(makeData({
      pending: false, files: [foldFile()],
      symbols: [{ id: "SY0", title: "mid", rationale: "", hunk_ids: ["H1"] }],
    }));
    fold("files");
    expect(document.querySelector(".file")!.classList.contains("folded")).toBe(true);

    (document.querySelector('[data-axis="symbols"] .group-btn[data-pill-id="SY0"]') as HTMLElement).click();
    expect(document.querySelector(".file")!.classList.contains("folded")).toBe(false);
    expect(codeRows('.hunk[data-id="H1"]')).toBe(1);
    // Ephemeral: no override reaches the hash.
    expect(window.location.hash).toBe("#fold=files&mode=diff");

    // The slider clears it: back to file headers, still filtered.
    fold("files");
    expect(document.querySelector(".file")!.classList.contains("folded")).toBe(true);
    fold("hunks");
    expect(codeRows('.hunk[data-id="H1"]')).toBe(0);
    expect(document.querySelector('.hunk[data-id="H1"] .hunk-header')).not.toBeNull();
    expect(document.querySelector('.hunk[data-id="H0"]')).toBeNull();
  });

  test("a filter restored at boot is not a focus: the diff opens filtered, at its level", async () => {
    localStorage.setItem("scr-active-group:local", "symbols:SY0");
    await bootViewer(makeData({
      pending: false, files: [foldFile()],
      symbols: [{ id: "SY0", title: "mid", rationale: "", hunk_ids: ["H1"] }],
    }));
    // Filtered — H0 and H2 are demoted — but H1 is a header, not code.
    expect(document.querySelector('.hunk[data-id="H0"]')).toBeNull();
    expect(document.querySelector('.hunk[data-id="H1"] .hunk-header')).not.toBeNull();
    expect(codeRows('.hunk[data-id="H1"]')).toBe(0);

    // The gesture itself still is one: the active pill toggles off, and
    // picking it again focuses.
    const pill = (): HTMLElement =>
      document.querySelector('[data-axis="symbols"] .group-btn[data-pill-id="SY0"]') as HTMLElement;
    pill().click();
    expect(document.querySelectorAll(".hunk-header").length).toBe(3);
    expect(codeRows(".hunk")).toBe(0);
    pill().click();
    expect(document.querySelector('.hunk[data-id="H0"]')).toBeNull();
    expect(codeRows('.hunk[data-id="H1"]')).toBe(1);
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


describe("the span gutter at the right edge: spans on visible code (ADR 0008)", () => {
  type Row = Record<string, unknown>;
  const span = (id: string, start: number, end: number, intent: string, smells: unknown[] = []): Row =>
    ({ id, start, end, intent, smells, context: "", refs: [] });
  const fold = (level: string): void =>
    (document.querySelector(`.fold-slider button[data-fold="${level}"]`) as HTMLElement).click();

  /** A nine-line file whose one hunk inserts lines 4..8. */
  function gutterFile(spans: Row[]): Row {
    const rows = [4, 5, 6, 7, 8].map((n) =>
      ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text: `l${n}` }));
    return {
      id: "F0", path: "a.py", status: "modified", language: "python",
      adds: 5, dels: 0, summary: "", head_line_count: 9,
      symbols: { added: [], modified: [], removed: [] },
      fold_regions: [],
      hunks: [makeHunkBlock("H0_0", "adds five lines", {
        old_start: 3, old_count: 0, new_start: 4, new_count: 5, rows, spans,
      })],
    };
  }
  /** A span over 4..7, one nested inside it over 6..7, a callout on 5
   *  (inside the outer span) and another on 8. */
  const NESTED = [
    span("H0_0:span:4-7", 4, 7, "the outer edit"),
    span("H0_0:span:6-7", 6, 7, "the inner edit", [{ tag: "dead-code", note: "" }]),
    span("H0_0:span:5-5", 5, 5, "a callout"),
    span("H0_0:span:8-8", 8, 8, "another callout"),
  ];

  const newRows = (): HTMLElement[] =>
    Array.from(document.querySelectorAll<HTMLElement>(".half-new .row:not(.row-annotation)"));
  const rowOfLine = (line: number): HTMLElement =>
    newRows().find((r) => r.querySelector(".cell-lineno")!.textContent === String(line))!;
  /** The lines carrying a mark for `spanId`, with each mark's kind. */
  function marks(spanId: string): Array<[number, string]> {
    const out: Array<[number, string]> = [];
    for (const row of newRows()) {
      const m = row.querySelector<HTMLElement>(`.cell-gutter-bars .span-mark[data-span-id="${spanId}"]`);
      if (!m) continue;
      const kind = Array.from(m.classList).find((c) => c.startsWith("span-") && c !== "span-mark")!;
      out.push([Number(row.querySelector(".cell-lineno")!.textContent), kind]);
    }
    return out;
  }
  const textOf = (spanId: string): HTMLElement | null =>
    document.querySelector<HTMLElement>(`.half-new > .span-text[data-span-id="${spanId}"]`);
  const gridRow = (el: HTMLElement): string => el.style.gridRow;
  const oldRows = (): HTMLElement[] =>
    Array.from(document.querySelectorAll<HTMLElement>(".half-old .row:not(.row-annotation):not(.row-placeholder)"));
  const minHeights = (rows: HTMLElement[]): string[] => rows.map((r) => r.style.minHeight);

  /** Stand in for layout, which jsdom has none of: every visible new-half
   *  row measures 20px tall, stacked from 0 in document order at its
   *  natural height (a stretch shows only in `style.minHeight`); a text
   *  block's body measures the height `heights` gives its span (0 if
   *  unnamed). Then pokes a row so the placement pass runs. */
  async function layoutGeometry(heights: Record<string, number>): Promise<void> {
    const half = document.querySelector<HTMLElement>(".hunk .half-new")!;
    for (const row of Array.from(half.children) as HTMLElement[]) {
      if (!row.classList.contains("row")) continue;
      row.getBoundingClientRect = (): DOMRect => {
        let top = 0;
        for (let s = row.previousElementSibling as HTMLElement | null; s; s = s.previousElementSibling as HTMLElement | null) {
          if (s.classList.contains("row") && s.style.display !== "none") top += 20;
        }
        return { top, bottom: top + 20, height: 20 } as DOMRect;
      };
    }
    for (const body of half.querySelectorAll<HTMLElement>(".span-text-body")) {
      const h = heights[body.dataset.spanId!] ?? 0;
      body.getBoundingClientRect = (): DOMRect => ({ height: h } as DOMRect);
    }
    newRows()[0].style.setProperty("--poke", String(Math.random()));
    await tick();
  }

  test("every new-half row carries the gutter's cells after its content cell, bars then text", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile([])], symbols: [] }));
    fold("code");
    for (const row of newRows()) {
      expect(Array.from(row.children).map((c) => c.className.split(" ")[0]))
        .toEqual(["cell", "cell", "cell-gutter-bars", "cell-gutter-text"]);
      expect(row.children[1].classList.contains("cell-content")).toBe(true);
    }
    // No span: the gutter is zero wide and no row is placed explicitly.
    const body = document.querySelector<HTMLElement>(".file-body")!;
    expect(body.style.getPropertyValue("--span-bars-w")).toBe("0px");
    expect(body.style.getPropertyValue("--span-text-w")).toBe("0px");
    expect(newRows().every((r) => r.style.gridRow === "")).toBe(true);
  });

  test("a multi-line span is a bar over exactly its rows with its text placed on them", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile(NESTED)], symbols: [] }));
    fold("code");

    expect(marks("H0_0:span:4-7")).toEqual([[4, "span-bar-top"], [5, "span-bar"], [6, "span-bar"], [7, "span-bar-bottom"]]);
    // The gutter has a fixed width: two bar columns and the text column.
    const body = document.querySelector<HTMLElement>(".file-body")!;
    expect(body.style.getPropertyValue("--span-bars-w")).toBe("16px");
    expect(body.style.getPropertyValue("--span-text-w")).toBe("26ch");

    // Every row is placed explicitly, and nothing of a span sits between
    // the rows — the outer span's text block sits on its first row, line
    // 4, hanging down from there.
    expect(newRows().map(gridRow)).toEqual(["1", "2", "3", "4", "5"]);
    expect(document.querySelector(".row-annotation")).toBeNull();
    const outer = textOf("H0_0:span:4-7")!;
    expect(outer).not.toBeNull();
    expect(outer.querySelector(".span-text-intent")!.textContent).toBe("the outer edit");
    expect(gridRow(outer)).toBe("1");
    expect(outer.style.display).toBe("");
  });

  test("nested spans are parallel bars one column apart; the child's text starts on the child's first row", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile(NESTED)], symbols: [] }));
    fold("code");

    expect(marks("H0_0:span:6-7")).toEqual([[6, "span-bar-top"], [7, "span-bar-bottom"]]);
    const depth = (spanId: string): string =>
      document.querySelector<HTMLElement>(`.span-mark[data-span-id="${spanId}"]`)!.style.getPropertyValue("--depth");
    expect(depth("H0_0:span:4-7")).toBe("0");
    expect(depth("H0_0:span:6-7")).toBe("1");
    const inner = textOf("H0_0:span:6-7")!;
    expect(gridRow(inner)).toBe("3");
    expect(inner.querySelector(".span-text-intent")!.textContent).toBe("the inner edit");
    expect(inner.querySelector(".smell")!.textContent).toBe("dead-code");
    // The pill row leads the block — beside the bar's first row, not
    // wherever a long intent happens to end — with the smells, then the
    // promote affordance; a span with no smells still has the row, for
    // the affordance.
    const body = inner.querySelector(".span-text-body")!;
    expect(Array.from(body.children).map((c) => c.className)).toEqual(["span-text-pills", "span-text-intent"]);
    expect(Array.from(body.querySelector(".span-text-pills")!.children).map((c) => c.className.split(" ")[0]))
      .toEqual(["smell", "span-promote"]);
    expect(Array.from(textOf("H0_0:span:4-7")!.querySelector(".span-text-pills")!.children).map((c) => c.className))
      .toEqual(["span-promote"]);

    // Single-line spans take the same form: a dot on their row at their
    // depth, and their text block in the gutter on that row — nothing in
    // the code column, no arrow.
    expect(marks("H0_0:span:5-5")).toEqual([[5, "span-dot"]]);
    expect(depth("H0_0:span:5-5")).toBe("1");
    expect(marks("H0_0:span:8-8")).toEqual([[8, "span-dot"]]);
    expect(depth("H0_0:span:8-8")).toBe("0");
    const callout = textOf("H0_0:span:5-5")!;
    expect(callout).not.toBeNull();
    expect(gridRow(callout)).toBe("2");
    expect(callout.querySelector(".span-text-intent")!.textContent).toBe("a callout");
    expect(gridRow(textOf("H0_0:span:8-8")!)).toBe("5");
    expect(document.querySelector(".row-annotation, .annot-arrow")).toBeNull();
  });

  test("a block carries its span's depth and mark kind, which its bracket to the mark is drawn from", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile(NESTED)], symbols: [] }));
    fold("code");
    // The block's `--depth` is the mark's: the bracket's upright stands
    // in the same column, at any nesting.
    for (const [id, depth] of [["H0_0:span:4-7", "0"], ["H0_0:span:6-7", "1"], ["H0_0:span:5-5", "1"], ["H0_0:span:8-8", "0"]]) {
      const block = textOf(id)!;
      expect(block.style.getPropertyValue("--depth")).toBe(depth);
      expect(document.querySelector<HTMLElement>(`.span-mark[data-span-id="${id}"]`)!.style.getPropertyValue("--depth")).toBe(depth);
    }
    // A single-line span's block is marked `dot`, since its upright
    // meets a dot at the row's middle and not a bar at its top.
    const bodyOf = (id: string): HTMLElement => textOf(id)!.querySelector<HTMLElement>(".span-text-body")!;
    expect(bodyOf("H0_0:span:5-5").classList.contains("dot")).toBe(true);
    expect(bodyOf("H0_0:span:8-8").classList.contains("dot")).toBe(true);
    expect(bodyOf("H0_0:span:4-7").classList.contains("dot")).toBe(false);
    expect(bodyOf("H0_0:span:6-7").classList.contains("dot")).toBe(false);
  });

  test("a single-line span's text joins the waterfall: it is pushed below its parent's, stretching the row before it", async () => {
    // config.py's +81 inside +76..+81: the parent's text runs down from 76
    // and the child's starts at 81 — inside the parent's — so the child
    // moves down by the overlap and line 80 grows by it.
    await bootViewer(makeData({ pending: false, files: [gutterFile([
      span("H0_0:span:4-8", 4, 8, "the parent, at length"),
      span("H0_0:span:8-8", 8, 8, "the child, longer still"),
    ])], symbols: [] }));
    fold("code");
    expect(marks("H0_0:span:8-8")).toEqual([[8, "span-dot"]]);
    // Line 8 is 80..100; a 90px parent block from 0 ends at 90, so the
    // child's block moves to 90 and line 7 grows by 10; its 30px then run
    // 10 past the hunk's shifted bottom (110), so line 8 grows by 10 too.
    await layoutGeometry({ "H0_0:span:4-8": 90, "H0_0:span:8-8": 30 });
    expect(textOf("H0_0:span:8-8")!.style.display).toBe("");
    expect(minHeights(newRows())).toEqual(["", "", "", "30px", "30px"]);
    expect(minHeights(oldRows())).toEqual(["", "", "", "30px", "30px"]);
  });

  test("a row inserted later is placed too, and the text blocks move with the rows", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile(NESTED)], symbols: [] }));
    fold("code");
    // A comment thread attached under line 4 — the same insertion any
    // annotation makes — takes a track; the rows below shift and the text
    // blocks follow their first rows.
    (rowOfLine(4).querySelector(".cell-lineno") as HTMLElement).click();
    await tick();
    const editor = rowOfLine(4).nextElementSibling as HTMLElement;
    expect(editor.classList.contains("row-annotation")).toBe(true);
    expect(editor.style.gridRow).toBe("2");
    expect(newRows().map(gridRow)).toEqual(["1", "3", "4", "5", "6"]);
    expect(gridRow(textOf("H0_0:span:4-7")!)).toBe("1");
    expect(gridRow(textOf("H0_0:span:5-5")!)).toBe("3");
    expect(gridRow(textOf("H0_0:span:6-7")!)).toBe("4");
  });

  test("a rationale taller than the hunk stretches only the last row; the old half's pair follows", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile([span("H0_0:span:4-5", 4, 5, "six lines of text")])], symbols: [] }));
    fold("code");
    // Five 20px rows; a 130px block from the top of line 4 runs 30px past
    // the bottom of line 8, so line 8 — and only line 8 — grows by 30.
    await layoutGeometry({ "H0_0:span:4-5": 130 });
    expect(minHeights(newRows())).toEqual(["", "", "", "", "50px"]);
    expect(minHeights(oldRows())).toEqual(["", "", "", "", "50px"]);
    expect(textOf("H0_0:span:4-5")!.style.transform).toBe("");
    // A block that fits leaves every row at its natural height.
    await layoutGeometry({ "H0_0:span:4-5": 100 });
    expect(minHeights(newRows())).toEqual(["", "", "", "", ""]);
    expect(minHeights(oldRows())).toEqual(["", "", "", "", ""]);
  });

  test("a block that would start inside the one above pushes its row down by exactly the overlap", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile([
      span("H0_0:span:4-5", 4, 5, "long"),
      span("H0_0:span:7-8", 7, 8, "short"),
    ])], symbols: [] }));
    fold("code");
    // Line 7's top is 60px; the first block ends at 130px. The row above
    // line 7 — line 6 — grows by the 70px overlap; the second block then
    // ends at 140px, inside the hunk's now-170px height, so line 8 does
    // not grow.
    await layoutGeometry({ "H0_0:span:4-5": 130, "H0_0:span:7-8": 10 });
    expect(minHeights(newRows())).toEqual(["", "", "90px", "", ""]);
    expect(minHeights(oldRows())).toEqual(["", "", "90px", "", ""]);
    expect(gridRow(textOf("H0_0:span:7-8")!)).toBe("4");
    expect(textOf("H0_0:span:7-8")!.style.transform).toBe("");
  });

  test("a parent's text runs past its child's first row; the row before the child stretches", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile([
      span("H0_0:span:4-8", 4, 8, "the parent, at length"),
      span("H0_0:span:6-8", 6, 8, "the child"),
    ])], symbols: [] }));
    fold("code");
    // The parent's block is not cut off at line 5: it runs to 70px, and
    // line 5 grows by 30 so the child's block starts clear of it at 70.
    await layoutGeometry({ "H0_0:span:4-8": 70, "H0_0:span:6-8": 10 });
    expect(textOf("H0_0:span:4-8")!.style.display).toBe("");
    expect(textOf("H0_0:span:6-8")!.style.display).toBe("");
    expect(minHeights(newRows())).toEqual(["", "50px", "", "", ""]);
    expect(minHeights(oldRows())).toEqual(["", "50px", "", "", ""]);
  });

  test("two spans starting on one row chain their blocks, outermost first, with no stretch between", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile([
      span("H0_0:span:4-7", 4, 7, "the whole thing"),
      span("H0_0:span:4-7:2", 4, 7, "the same rows, again"),
    ])], symbols: [] }));
    fold("code");
    expect(marks("H0_0:span:4-7").length).toBe(4);
    expect(marks("H0_0:span:4-7:2").length).toBe(4);
    const outer = textOf("H0_0:span:4-7")!;
    const inner = textOf("H0_0:span:4-7:2")!;
    expect(gridRow(outer)).toBe("1");
    expect(gridRow(inner)).toBe("1");
    expect(outer.querySelector(".span-text-body")!.classList.contains("chained")).toBe(false);
    expect(inner.querySelector(".span-text-body")!.classList.contains("chained")).toBe(true);
    await layoutGeometry({ "H0_0:span:4-7": 30, "H0_0:span:4-7:2": 20 });
    expect(outer.style.transform).toBe("");
    expect(inner.style.transform).toBe("translateY(30px)");
    expect(minHeights(newRows())).toEqual(["", "", "", "", ""]);
  });

  test("hiding a block's first row hides the block and releases the rows it stretched", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile([span("H0_0:span:4-5", 4, 5, "tall")])], symbols: [] }));
    fold("code");
    await layoutGeometry({ "H0_0:span:4-5": 130 });
    expect(minHeights(oldRows())).toEqual(["", "", "", "", "50px"]);
    rowOfLine(4).style.display = "none";
    await tick();
    expect(textOf("H0_0:span:4-5")!.style.display).toBe("none");
    expect(minHeights(newRows())).toEqual(["", "", "", "", ""]);
    expect(minHeights(oldRows())).toEqual(["", "", "", "", ""]);
  });

  test("the marks survive a chip disclosing rows above them", async () => {
    await bootViewer(makeData({ pending: false, files: [gutterFile(NESTED)], symbols: [] }));
    fold("code");
    const before = marks("H0_0:span:4-7");

    const chip = document.querySelector(".gap-chip") as HTMLElement;   // "expand 3 lines above"
    expect(chip.textContent).toContain("above");
    queueFileText(0, "a.py", null, nineLines);
    chip.click();
    await tick();
    expect(document.querySelector(".gap-expansion")).not.toBeNull();

    // The disclosed rows are another grid; the hunk's rows, marks and text
    // placement are untouched.
    expect(marks("H0_0:span:4-7")).toEqual(before);
    expect(document.querySelectorAll(".gap-expansion .span-mark, .gap-expansion .span-text").length).toBe(0);
    expect(gridRow(textOf("H0_0:span:4-7")!)).toBe("1");
    // The disclosed rows carry the gutter cells too, so the strip is
    // continuous down the file.
    expect(document.querySelector(".gap-expansion .half-new .row .cell-gutter-text")).not.toBeNull();
  });

  test("a bar runs through the comment rows inside its span, at the span's depth", async () => {
    // Threads on 5 (inside the outer span 4..7), on 6 (inside it and the
    // inner span 6..7) and on 7 (the outer span's last row: outside).
    const comment = (id: string, side: "old" | "new", line: number): Record<string, unknown> =>
      ({ id, file: "a.py", side, line, body: id, created_at: 1, updated_at: 1, source: "local", derived_from: null });
    await bootViewer(
      makeData({ pending: false, files: [gutterFile(NESTED)], symbols: [] }),
      { comments: [comment("c5", "new", 5), comment("c6", "new", 6), comment("c7", "new", 7)] },
    );
    fold("code");
    await tick();
    const segments = (row: HTMLElement): Array<[string, string]> =>
      Array.from(row.querySelectorAll<HTMLElement>(":scope > .cell-gutter-bars > .span-mark"))
        .map((m) => [m.dataset.spanId!, m.style.getPropertyValue("--depth")]);
    const threadAfter = (line: number): HTMLElement => {
      const next = rowOfLine(line).nextElementSibling as HTMLElement;
      expect(next.classList.contains("annot-comment")).toBe(true);
      return next;
    };
    expect(segments(threadAfter(5))).toEqual([["H0_0:span:4-7", "0"]]);
    expect(segments(threadAfter(6))).toEqual([["H0_0:span:4-7", "0"], ["H0_0:span:6-7", "1"]]);
    expect(segments(threadAfter(7))).toEqual([]);
    expect(threadAfter(7).querySelector(":scope > .cell-gutter-bars")).toBeNull();
    // The code rows' own marks are untouched by the pass.
    expect(marks("H0_0:span:4-7")).toEqual([[4, "span-bar-top"], [5, "span-bar"], [6, "span-bar"], [7, "span-bar-bottom"]]);
  });

  describe("the gutter fold", () => {
    const key = (k: string): void => { document.dispatchEvent(new KeyboardEvent("keydown", { key: k })); };
    const collapsed = (): boolean => document.documentElement.classList.contains("gutter-collapsed");
    /** A tall block, so the expanded gutter stretches the hunk's last row. */
    const TALL = [span("H0_0:span:4-5", 4, 5, "tall", [{ tag: "dead-code", note: "" }])];

    test("g folds the gutter: blocks hide, no row is stretched, marks stay; g again restores both", async () => {
      await bootViewer(makeData({ pending: false, files: [gutterFile(TALL)], symbols: [] }));
      fold("code");
      await layoutGeometry({ "H0_0:span:4-5": 130 });
      expect(collapsed()).toBe(false);
      expect(minHeights(newRows())).toEqual(["", "", "", "", "50px"]);

      key("g");
      expect(collapsed()).toBe(true);
      expect(textOf("H0_0:span:4-5")!.style.display).toBe("none");
      expect(minHeights(newRows())).toEqual(["", "", "", "", ""]);
      expect(minHeights(oldRows())).toEqual(["", "", "", "", ""]);
      expect(marks("H0_0:span:4-5")).toEqual([[4, "span-bar-top"], [5, "span-bar-bottom"]]);
      expect(localStorage.getItem("scr-gutter-fold")).toBe("collapsed");
      // A pass the rows trigger while folded places nothing either.
      await layoutGeometry({ "H0_0:span:4-5": 130 });
      expect(minHeights(newRows())).toEqual(["", "", "", "", ""]);

      key("g");
      expect(collapsed()).toBe(false);
      expect(textOf("H0_0:span:4-5")!.style.display).toBe("");
      expect(minHeights(newRows())).toEqual(["", "", "", "", "50px"]);
      expect(minHeights(oldRows())).toEqual(["", "", "", "", "50px"]);
      expect(localStorage.getItem("scr-gutter-fold")).toBe("expanded");
    });

    test("the fold is remembered across a reload, and applies to every half", async () => {
      localStorage.setItem("scr-gutter-fold", "collapsed");
      const file = gutterFile(TALL);
      const hunks = file.hunks as Record<string, unknown>[];
      hunks.push(makeHunkBlock("H0_1", "adds more", {
        old_start: 8, old_count: 0, new_start: 12, new_count: 2,
        rows: [12, 13].map((n) => ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text: `l${n}` })),
        spans: [span("H0_1:span:12-13", 12, 13, "later")],
      }));
      await bootViewer(makeData({ pending: false, files: [file], symbols: [] }));
      fold("code");
      expect(collapsed()).toBe(true);
      const blocks = Array.from(document.querySelectorAll<HTMLElement>(".half-new > .span-text"));
      expect(blocks.length).toBe(2);
      expect(blocks.map((b) => b.style.display)).toEqual(["none", "none"]);
      key("g");
      expect(blocks.map((b) => b.style.display)).toEqual(["", ""]);
    });

    test("the strip's empty area toggles the fold; a mark expands it and brings its text into view", async () => {
      await bootViewer(makeData({ pending: false, files: [gutterFile(TALL)], symbols: [] }));
      fold("code");
      const cell = rowOfLine(6).querySelector<HTMLElement>(".cell-gutter-text")!;
      expect(cell.title).toContain("(g)");
      cell.click();
      expect(collapsed()).toBe(true);
      (rowOfLine(6).querySelector(".cell-gutter-bars") as HTMLElement).click();
      expect(collapsed()).toBe(false);
      cell.click();
      expect(collapsed()).toBe(true);

      // Folded, a mark's tooltip is the span's rationale: its smells, then
      // its intent.
      const mark = rowOfLine(5).querySelector<HTMLElement>('.span-mark[data-span-id="H0_0:span:4-5"]')!;
      expect(mark.title).toBe("dead-code\ntall");
      const scrolled: Element[] = [];
      const orig = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function scrollIntoView(this: Element): void { scrolled.push(this); };
      try {
        mark.click();
      } finally {
        Element.prototype.scrollIntoView = orig;
      }
      expect(collapsed()).toBe(false);
      expect(scrolled).toEqual([textOf("H0_0:span:4-5")!.querySelector(".span-text-body")]);
      // A click on a block's own text is not a fold gesture.
      (textOf("H0_0:span:4-5")!.querySelector(".span-text-intent") as HTMLElement).click();
      expect(collapsed()).toBe(false);
    });
  });
});


describe("LLM observation → comment promotion", () => {
  test("smell pill click saves a comment immediately and detaches the pill", async () => {
    window.location.hash = "#fold=code";
    const data = makeData({
      pending: false,
      smells_catalogue: {
        perf: { label: "perf concern", severity: "minor", color: "#888" },
      },
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_line_count: null,
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

  test("a single-line span already promoted on initial load is hidden", async () => {
    window.location.hash = "#fold=code";
    const data = makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 0, summary: "", head_line_count: null,
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "", {
          spans: [{ id: "H0_0:span:1-1", start: 1, end: 1, intent: "old observation", smells: [], context: "", refs: [] }],
        })],
      }],
    });
    await bootViewer(data, {
      comments: [{
        id: "local-1", file: "a.py", side: "new", line: 1,
        body: "promoted version", created_at: 1, updated_at: 1,
        source: "local",
        derived_from: "H0_0:span:1-1",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    // The span is gone from the gutter — block and dot — because a local
    // comment derived from it exists; the comment stands in its place.
    expect(document.querySelector('[data-span-id="H0_0:span:1-1"]')).toBeNull();
    expect(document.querySelector(
      '.comment-thread-entry[data-comment-id="local-1"]',
    )).not.toBeNull();
    // The gutter's pass, re-run by the removal, has nothing left to place
    // and holds no stretch.
    for (const row of document.querySelectorAll<HTMLElement>(".half-new .row")) expect(row.style.minHeight).toBe("");
  });

  /** A nine-line file whose one hunk inserts lines 4..8, with `spans`. */
  function spanFile(spans: Array<Record<string, unknown>>): Record<string, unknown> {
    const rows = [4, 5, 6, 7, 8].map((n) =>
      ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text: `l${n}` }));
    return {
      id: "F0", path: "a.py", status: "modified", language: "python",
      adds: 5, dels: 0, summary: "", head_line_count: 9,
      symbols: { added: [], modified: [], removed: [] }, fold_regions: [],
      hunks: [makeHunkBlock("H0_0", "adds five lines", {
        old_start: 3, old_count: 0, new_start: 4, new_count: 5, rows, spans,
      })],
    };
  }
  const span = (id: string, start: number, end: number, intent: string, smells: unknown[] = []): Record<string, unknown> =>
    ({ id, start, end, intent, smells, context: "", refs: [] });
  /** Capture the next POST /comments body. */
  function capturePost(): () => Record<string, unknown> | null {
    let posted: Record<string, unknown> | null = null;
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        posted = JSON.parse(init!.body as string);
        return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(posted) } as Response);
      }) as typeof fetch);
    return () => posted;
  }
  /** The kinds of the marks on code rows for `spanId` (a thread row's
   *  segment, added by the placement pass, is checked on its own). */
  const markKinds = (spanId: string): string[] =>
    Array.from(document.querySelectorAll<HTMLElement>(`.row:not(.row-annotation) > .cell-gutter-bars > .span-mark[data-span-id="${spanId}"]`))
      .map((m) => Array.from(m.classList).find((c) => c.startsWith("span-") && c !== "span-mark")!);

  test("every block's pill row carries a promote affordance for its intent; a span with no intent has none", async () => {
    window.location.hash = "#fold=code";
    await bootViewer(makeData({ pending: false, files: [spanFile([
      span("H0_0:span:4-7", 4, 7, "a multi-line span", [{ tag: "dead-code", note: "" }]),
      span("H0_0:span:5-6", 5, 6, "a nested one, no smells"),
      span("H0_0:span:8-8", 8, 8, "a callout"),
      span("H0_0:span:7-7", 7, 7, "", []),
    ])] }));
    await new Promise<void>((r) => setTimeout(r, 0));
    const btn = (id: string): HTMLButtonElement | null =>
      document.querySelector<HTMLButtonElement>(`.span-text[data-span-id="${id}"] .span-text-pills > .span-promote`);
    for (const id of ["H0_0:span:4-7", "H0_0:span:5-6", "H0_0:span:8-8"]) {
      const b = btn(id)!;
      expect(b, id).not.toBeNull();
      expect(b.type).toBe("button");
      expect(b.title).toContain(`line ${id.split(":")[2].split("-")[0]}`);
    }
    // Nothing to promote: the block shows "(no intent)" and no button.
    expect(document.querySelector('.span-text[data-span-id="H0_0:span:7-7"] .span-text-intent.empty')).not.toBeNull();
    expect(btn("H0_0:span:7-7")).toBeNull();
  });

  test("promoting a multi-line span's intent saves a comment on its first line, hides its block and keeps its bar", async () => {
    window.location.hash = "#fold=code";
    await bootViewer(makeData({ pending: false, files: [spanFile([
      span("H0_0:span:4-7", 4, 7, "the outer edit", [{ tag: "dead-code", note: "" }]),
      span("H0_0:span:6-7", 6, 7, "the inner edit"),
    ])] }));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(markKinds("H0_0:span:4-7")).toEqual(["span-bar-top", "span-bar", "span-bar", "span-bar-bottom"]);
    const posted = capturePost();

    document.querySelector<HTMLElement>('.span-text[data-span-id="H0_0:span:4-7"] .span-promote')!.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    await new Promise<void>((r) => setTimeout(r, 0));

    const c = posted()!;
    expect(c).not.toBeNull();
    expect(c.body).toBe("the outer edit");
    expect(c.derived_from).toBe("H0_0:span:4-7");
    expect(c.file).toBe("a.py");
    expect(c.side).toBe("new");
    expect(c.line).toBe(4);
    // The block (pills and intent) is gone; the bar still marks the range
    // the comment, on one line, does not; the thread sits under line 4.
    expect(document.querySelector('.span-text[data-span-id="H0_0:span:4-7"]')).toBeNull();
    expect(markKinds("H0_0:span:4-7")).toEqual(["span-bar-top", "span-bar", "span-bar", "span-bar-bottom"]);
    const row4 = Array.from(document.querySelectorAll<HTMLElement>(".half-new .row"))
      .find((r) => r.querySelector(".cell-lineno")!.textContent === "4")!;
    const thread = row4.nextElementSibling as HTMLElement;
    expect(thread.classList.contains("annot-comment")).toBe(true);
    expect(thread.querySelector(':scope > .cell-gutter-bars > .span-mark.span-bar[data-span-id="H0_0:span:4-7"]')).not.toBeNull();
    // The nested span is untouched.
    expect(document.querySelector('.span-text[data-span-id="H0_0:span:6-7"]')).not.toBeNull();
    expect(markKinds("H0_0:span:6-7")).toEqual(["span-bar-top", "span-bar-bottom"]);
  });

  test("promoting a single-line span's intent removes its dot with its block", async () => {
    window.location.hash = "#fold=code";
    await bootViewer(makeData({ pending: false, files: [spanFile([span("H0_0:span:5-5", 5, 5, "a callout")])] }));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(markKinds("H0_0:span:5-5")).toEqual(["span-dot"]);
    const posted = capturePost();
    document.querySelector<HTMLElement>('.span-text[data-span-id="H0_0:span:5-5"] .span-promote')!.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(posted()!.line).toBe(5);
    expect(posted()!.derived_from).toBe("H0_0:span:5-5");
    expect(document.querySelector('[data-span-id="H0_0:span:5-5"]')).toBeNull();
  });

  test("a multi-line span promoted in an earlier session draws its bar and no block, on load and after a re-augment", async () => {
    window.location.hash = "#fold=code";
    const spans = [
      span("H0_0:span:4-7", 4, 7, "the outer edit", [{ tag: "dead-code", note: "" }]),
      span("H0_0:span:6-7", 6, 7, "the inner edit"),
    ];
    await bootViewer(makeData({ pending: false, files: [spanFile(spans)] }), {
      comments: [{
        id: "local-1", file: "a.py", side: "new", line: 4,
        body: "the outer edit", created_at: 1, updated_at: 1,
        source: "local", derived_from: "H0_0:span:4-7",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    const check = (): void => {
      expect(document.querySelector('.span-text[data-span-id="H0_0:span:4-7"]')).toBeNull();
      expect(markKinds("H0_0:span:4-7")).toEqual(["span-bar-top", "span-bar", "span-bar", "span-bar-bottom"]);
      expect(document.querySelector('.span-text[data-span-id="H0_0:span:6-7"]')).not.toBeNull();
    };
    check();
    expect(document.querySelector('.comment-thread-entry[data-comment-id="local-1"]')).not.toBeNull();
    // The bar runs through the thread row under line 4, as any bar does.
    const row4 = Array.from(document.querySelectorAll<HTMLElement>(".half-new .row"))
      .find((r) => r.querySelector(".cell-lineno")!.textContent === "4")!;
    const thread = row4.nextElementSibling as HTMLElement;
    expect(thread.classList.contains("annot-comment")).toBe(true);
    expect(thread.querySelector(':scope > .cell-gutter-bars > .span-mark[data-span-id="H0_0:span:4-7"]')).not.toBeNull();
    // A re-augment rebuilds the hunk with the comments known: the
    // renderer itself leaves the block out and draws the bar.
    lastEventSource().dispatch("hunk", {
      file_idx: 0, hunk_idx: 0, ok: true,
      block: makeHunkBlock("H0_0", "re-run", {
        old_start: 3, old_count: 0, new_start: 4, new_count: 5, spans,
        rows: [4, 5, 6, 7, 8].map((n) => ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text: `l${n}` })),
      }),
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    check();
  });

  test("a comment store written before spans hides the span under its line_note id", async () => {
    window.location.hash = "#fold=code";
    const spans = [
      { id: "H0_0:span:1-1", start: 1, end: 1, intent: "old observation", smells: [], context: "", refs: [] },
      { id: "H0_0:span:1-2", start: 1, end: 2, intent: "a span over the same start", smells: [], context: "", refs: [] },
    ];
    await bootViewer(makeData({ files: [{
      id: "F0", path: "a.py", status: "modified", language: "python",
      adds: 0, dels: 0, summary: "", head_line_count: null,
      symbols: { added: [], modified: [], removed: [] },
      hunks: [makeHunkBlock("H0_0", "", { spans })],
    }] }), {
      comments: [{
        id: "local-1", file: "a.py", side: "new", line: 1,
        body: "promoted version", created_at: 1, updated_at: 1,
        source: "local",
        derived_from: "H0_0:line_note:1",
      }],
    });
    await new Promise<void>((r) => setTimeout(r, 0));
    // A re-augment rebuilds the hunk with the comments known: the legacy
    // id keeps the one-line span from resurrecting, and never touches the
    // multi-line span starting on the same line.
    lastEventSource().dispatch("hunk", {
      file_idx: 0, hunk_idx: 0, ok: true, block: makeHunkBlock("H0_0", "re-run", { spans }),
    });
    expect(document.querySelector('[data-span-id="H0_0:span:1-1"]')).toBeNull();
    expect(document.querySelector('.span-text[data-span-id="H0_0:span:1-2"]')).not.toBeNull();
  });
});


describe("sidebar comment counts", () => {
  test("Files-axis pill shows a comment dot once comments load; directories carry none", async () => {
    window.location.hash = "#fold=code";
    await bootViewer(makeData({
      pending: false,
      files: [
        {
          id: "F0", path: "a.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_line_count: null,
          symbols: { added: [], modified: [], removed: [] },
          hunks: [makeHunkBlock("H0_0")],
        },
        {
          id: "F1", path: "lib/b.py", status: "modified", language: "python",
          adds: 0, dels: 0, summary: "", head_line_count: null,
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
          id: "gh-3", file: "lib/b.py", side: "new", line: 1,
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
    // a.py, lib/, lib/b.py
    expect(pills).toHaveLength(3);

    // a.py: 1 unresolved of 2 threads (the reply doesn't add to the count)
    // — an orange dot. The numbers are in the title, not the badge.
    const aPyBadge = pills[0].querySelector(".group-btn-comments") as HTMLElement;
    expect(aPyBadge).not.toBeNull();
    expect(aPyBadge.textContent).toBe("");
    expect(aPyBadge.classList.contains("has-unresolved")).toBe(true);
    expect(aPyBadge.title).toBe("1 unresolved of 2 threads");

    // lib/: a directory carries no dot — the rollup said "something in
    // here" without saying where, and the file row beneath it does.
    expect(pills[1].querySelector(".group-btn-comments")).toBeNull();

    // lib/b.py: 0 unresolved of 1 — a green dot.
    const bPyBadge = pills[2].querySelector(".group-btn-comments") as HTMLElement;
    expect(bPyBadge).not.toBeNull();
    expect(bPyBadge.classList.contains("all-resolved")).toBe(true);
    expect(bPyBadge.classList.contains("has-unresolved")).toBe(false);
    // With a badge somewhere, the section reserves the badge column on
    // every row so the count pills stay in one column.
    expect(filesSection.classList.contains("has-comment-badges")).toBe(true);
  });

  test("pills with no comments get no comment badge, and the section reserves no badge column", async () => {
    window.location.hash = "#fold=code";
    await bootViewer(makeData({ pending: false }));
    await new Promise<void>((r) => setTimeout(r, 0));
    const filesSection = document.querySelector('[data-axis="files"]')!;
    const pill = filesSection.querySelector(".group-btn") as HTMLElement;
    expect(pill).not.toBeNull();
    expect(pill.querySelector(".group-btn-comments")).toBeNull();
    expect(filesSection.classList.contains("has-comment-badges")).toBe(false);
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
    // Boot with the fold mode set to "code" so all hunk rows render —
    // default fold is "hunks" which collapses the diff body.
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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
    window.location.hash = "#fold=code";
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


describe("fold regions (server-computed) and lazy fold summaries", () => {
  // A region as the server ships it on the FileBlock: line ranges per
  // side, no row indices. The viewer places it on whatever rows it has.
  function region(over: Record<string, unknown>): Record<string, unknown> {
    return {
      context: "right", right_start: null, right_end: null, left_start: null, left_end: null,
      has_changes: false, qualified_name: null, kind: null, summary: "", ...over,
    };
  }

  function dataWithFold(): ViewerData {
    return makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok", head_line_count: null,
        symbols: { added: [], modified: [], removed: [] },
        fold_regions: [
          region({ context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, has_changes: true }),
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
    // and its body isn't in the DOM. Click "code" so the diff body
    // (and its fold-chev) materialises. This matches the user flow:
    // expand the fold-slider before reaching for a fold.
    (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
  }

  function clickEl(el: Element): void {
    // jsdom's SVGElement doesn't expose .click(); the addEventListener
    // path needs a dispatched event. Bubbling so the .hunk-header's
    // own click handler doesn't fire from us (stopPropagation in the
    // fold-chev handler covers that).
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  }

  const chevrons = (root: ParentNode = document): SVGElement[] =>
    Array.from(root.querySelectorAll<SVGElement>(".fold-chev"));
  const newRows = (root: ParentNode = document): HTMLElement[] =>
    Array.from(root.querySelectorAll<HTMLElement>(".half-new .row:not(.row-annotation)"));

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
    // The region straddles changed content, so the model gets a diff body.
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

  test("a deletion-only region posts context=left with pre-image coordinates", async () => {
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 0, dels: 3, summary: "ok", head_line_count: null,
        symbols: { added: [], modified: [], removed: [] },
        fold_regions: [
          region({ context: "left", left_start: 10, left_end: 12, has_changes: true, qualified_name: "removed", kind: "function" }),
        ],
        hunks: [makeHunkBlock("H0_0", "real intent", {
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
    // The chevron sits on the old side: the header row has no new content.
    expect(marker!.closest(".half-old")).not.toBeNull();
    clickEl(marker!);

    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    expect(JSON.parse(foldCalls[0].init!.body as string)).toEqual({
      file_idx: 0, context: "left", left_start: 10, left_end: 12,
    });

    await new Promise((r) => setTimeout(r, 0));
    expect(document.querySelector(".annot-box")?.textContent).toBe("function removed — drops the removed() helper");
  });

  test("a region the diff never carried folds once a chip discloses it", async () => {
    // The server folds the whole file: `foo` spans lines 1-3, of which
    // the hunk shows only line 3. With one row of the region on screen
    // there is nothing to fold; expanding the gap above brings its
    // opener and body in, and the chevron on line 1 folds rows from the
    // expansion and the hunk alike. Nothing here reads the rows' text.
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok", head_line_count: 3,
        symbols: { added: [], modified: [], removed: [] },
        fold_regions: [
          region({ context: "both", right_start: 1, right_end: 3, left_start: 1, left_end: 3, has_changes: true, qualified_name: "foo", kind: "function" }),
        ],
        hunks: [makeHunkBlock("H0_0", "ok", {
          old_start: 3, old_count: 1, new_start: 3, new_count: 1,
          rows: [{
            kind: "pair", old_line: 3, new_line: 3,
            old_text: "    return old()", new_text: "    return new()",
          }],
        })],
      }],
    }));
    expandHunk();
    expect(chevrons()).toHaveLength(0);   // one row of `foo` on screen: nothing to fold

    const chip = document.querySelector(".gap-chip") as HTMLElement;
    queueFileText(0, "a.py", null, "def foo():\n    x = 1\n    return new()\n");
    chip.click();
    await tick();

    expect(chevrons()).toHaveLength(1);
    const expansionRows = newRows(document.querySelector(".gap-expansion")!);
    const hunkRows = newRows(document.querySelector(".hunk")!);
    expect(expansionRows).toHaveLength(2);
    expect(hunkRows).toHaveLength(1);
    expect(chevrons()[0].closest(".row")).toBe(expansionRows[0]);   // on the opener

    clickEl(chevrons()[0]);
    expect(expansionRows[0].style.display).not.toBe("none");
    expect(expansionRows[1].style.display).toBe("none");
    expect(hunkRows[0].style.display).toBe("none");

    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    expect(JSON.parse(foldCalls[0].init!.body as string)).toEqual({
      file_idx: 0, context: "both", right_start: 1, right_end: 3, left_start: 1, left_end: 3,
    });
  });

  test("a region whose opener is off screen folds from its first rendered row", async () => {
    // `Foo` spans 1-9 and `Foo.bar` 5-9; the hunk shows 6-8 with neither
    // opener. Both regions land on row 6; the tighter one — the method —
    // labels the fold, and one chevron folds the two rows under it.
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok", head_line_count: 9,
        symbols: { added: [], modified: [], removed: [] },
        fold_regions: [
          region({ context: "both", right_start: 1, right_end: 9, left_start: 1, left_end: 9, has_changes: true, qualified_name: "Foo", kind: "class" }),
          region({ context: "both", right_start: 5, right_end: 9, left_start: 5, left_end: 9, has_changes: true, qualified_name: "Foo.bar", kind: "function" }),
        ],
        hunks: [makeHunkBlock("H0_0", "ok", {
          old_start: 6, old_count: 3, new_start: 6, new_count: 3,
          rows: [
            { kind: "ctx", old_line: 6, new_line: 6, old_text: "        a()", new_text: "        a()" },
            { kind: "pair", old_line: 7, new_line: 7, old_text: "        b()", new_text: "        c()" },
            { kind: "ctx", old_line: 8, new_line: 8, old_text: "        d()", new_text: "        d()" },
          ],
        })],
      }],
    }));
    expandHunk();

    expect(chevrons()).toHaveLength(1);
    const rows = newRows();
    expect(chevrons()[0].closest(".row")).toBe(rows[0]);
    clickEl(chevrons()[0]);
    expect(rows.map((r) => r.style.display)).toEqual(["", "none", "none"]);
    expect(document.querySelector(".annot-box")?.textContent).toBe("function Foo.bar — summarising…");
  });

  // --- A collapsed definition shows its labels (ADR 0008 slice 5b) --------

  /** A class over lines 1..11 with two methods, the hunk inserting 5..12
   *  with one context row (line 4). Spans: exactly alpha, a guard inside
   *  beta with a callout inside that, and a module constant after the
   *  class. Regions enclosing first, as the server lists them. */
  function labelledFile(regionOverrides: Record<string, unknown>[] = []): Record<string, unknown> {
    const rows = [4, 5, 6, 7, 8, 9, 10, 11, 12].map((n) =>
      ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text:
        n === 5 ? "    def alpha(self):" : n === 8 ? "    def beta(self):" : n === 12 ? "X = 1" : `        l${n}` }));
    rows[0] = { kind: "ctx", old_line: 3, new_line: 4, old_text: "    pass", new_text: "    pass" };
    const span = (id: string, start: number, end: number, intent: string, smells: unknown[] = []): Record<string, unknown> =>
      ({ id, start, end, intent, smells, context: "", refs: [] });
    return {
      id: "F0", path: "a.py", status: "modified", language: "python",
      adds: 8, dels: 0, summary: "", head_line_count: 20,
      symbols: { added: [], modified: [], removed: [] },
      fold_regions: [
        region({ right_start: 1, right_end: 11, has_changes: true, qualified_name: "Foo", kind: "class", ...(regionOverrides[0] || {}) }),
        region({ right_start: 5, right_end: 7, has_changes: true, qualified_name: "Foo.alpha", kind: "method", ...(regionOverrides[1] || {}) }),
        region({ right_start: 8, right_end: 11, has_changes: true, qualified_name: "Foo.beta", kind: "method", ...(regionOverrides[2] || {}) }),
      ],
      hunks: [makeHunkBlock("H0_0", "adds two methods", {
        old_start: 3, old_count: 1, new_start: 4, new_count: 9, rows,
        spans: [
          span("H0_0:span:5-7", 5, 7, "alpha does A"),
          span("H0_0:span:9-10", 9, 10, "beta's guard"),
          span("H0_0:span:10-10", 10, 10, "callout", [{ tag: "dead-code", note: "" }]),
          span("H0_0:span:12-12", 12, 12, "module constant"),
        ],
      })],
    };
  }
  const chevronOnLine = (line: number): SVGElement =>
    chevrons().find((c) => c.closest(".row")!.querySelector(".cell-lineno")!.textContent === String(line))!;
  const rowOfLine = (line: number): HTMLElement =>
    newRows().find((r) => r.querySelector(".cell-lineno")!.textContent === String(line))!;
  const labelRows = (root: ParentNode): string[] =>
    Array.from(root.querySelectorAll<HTMLElement>(".label-row")).map((el) =>
      `${el.dataset.def ?? el.dataset.id}: ${el.querySelector(".label-text")!.textContent}`);
  const foldBoxOf = (line: number): HTMLElement =>
    rowOfLine(line).nextElementSibling!.querySelector<HTMLElement>(".annot-box")!;

  test("collapsing a definition shows the labels it hid, beneath its summary line", async () => {
    await bootViewer(makeData({ pending: false, files: [labelledFile()], symbols: [] }));
    expandHunk();
    queueFetchResponse({ status: 200, body: { file_idx: 0, context: "right", right_start: 8, right_end: 11, summary: "beta guards then acts" } });

    // Collapse `beta` (chevron on its opener, line 8): lines 9-11 hide.
    clickEl(chevronOnLine(8));
    expect([9, 10, 11].map((n) => rowOfLine(n).style.display)).toEqual(["none", "none", "none"]);
    const box = foldBoxOf(8);
    expect(box.querySelector(".fold-summary")!.textContent).toBe("method Foo.beta — summarising…");
    // The tree holds the hidden labels: the guard span with its callout
    // nested inside. Not `Foo` or `beta` (they cover the visible opener),
    // not alpha's span or the module constant (outside the fold).
    const tree = box.querySelector(".label-tree")!;
    expect(labelRows(tree)).toEqual(["H0_0:span:9-10: beta's guard", "H0_0:span:10-10: callout"]);
    expect(tree.querySelector('.label-node > .label-row[data-id="H0_0:span:9-10"]')).not.toBeNull();
    expect(tree.querySelector('.label-children > .label-row[data-id="H0_0:span:10-10"] .smell')!.textContent).toBe("dead-code");
    // The guard's and the callout's text blocks (their first rows are
    // hidden) fold with their rows.
    await tick();
    const guardText = document.querySelector<HTMLElement>('.span-text[data-span-id="H0_0:span:9-10"]')!;
    const calloutText = document.querySelector<HTMLElement>('.span-text[data-span-id="H0_0:span:10-10"]')!;
    expect(guardText.style.display).toBe("none");
    expect(calloutText.style.display).toBe("none");

    // The summary lands: the line changes, the tree stays.
    await tick();
    expect(box.querySelector(".fold-summary")!.textContent).toBe("method Foo.beta — beta guards then acts");
    expect(labelRows(box.querySelector(".label-tree")!)).toEqual(["H0_0:span:9-10: beta's guard", "H0_0:span:10-10: callout"]);

    // Clicking a span's label opens the fold; its bar and text are back.
    clickEl(tree.querySelector('.label-row[data-id="H0_0:span:9-10"]')!);
    await tick();
    expect(chevronOnLine(8).classList.contains("open")).toBe(true);
    expect([9, 10, 11].map((n) => rowOfLine(n).style.display)).toEqual(["", "", ""]);
    expect(guardText.style.display).toBe("");
    expect(calloutText.style.display).toBe("");
    expect(rowOfLine(9).querySelector('.span-mark[data-span-id="H0_0:span:9-10"]')).not.toBeNull();
    expect(box.closest(".row-annotation")!.style.display).toBe("none");
  });

  test("a collapsed class lists the definitions inside it, each with its spans", async () => {
    await bootViewer(makeData({ pending: false, files: [labelledFile([{}, { summary: "alpha, summarised" }, {}])], symbols: [] }));
    expandHunk();
    queueFetchResponse({ status: 500, body: {} });

    // `Foo` folds from its first rendered row (line 4, context) through 11.
    clickEl(chevronOnLine(4));
    const tree = foldBoxOf(4).querySelector(".label-tree")!;
    // alpha shows its summary; beta, with none, its opener line. Spans
    // nest under the method that holds them. The module constant (line
    // 12) is outside the class and not listed.
    expect(labelRows(tree)).toEqual([
      "Foo.alpha: alpha, summarised",
      "H0_0:span:5-7: alpha does A",
      "Foo.beta: def beta(self):",
      "H0_0:span:9-10: beta's guard",
      "H0_0:span:10-10: callout",
    ]);
    const betaNode = tree.querySelector('.label-row[data-def="Foo.beta"]')!.parentElement!;
    expect(labelRows(betaNode.querySelector(":scope > .label-children")!))
      .toEqual(["H0_0:span:9-10: beta's guard", "H0_0:span:10-10: callout"]);
    // The module constant's text block, outside the fold, stays visible.
    expect(document.querySelector<HTMLElement>('.span-text[data-span-id="H0_0:span:12-12"]')!.style.display).toBe("");

    // Clicking a definition opens the fold and lands on its opener.
    clickEl(tree.querySelector('.label-row[data-def="Foo.beta"]')!);
    expect(chevronOnLine(4).classList.contains("open")).toBe(true);
    expect(rowOfLine(8).style.display).toBe("");
  });

  test("a span still showing on the chevron row is not repeated as a hidden label", async () => {
    await bootViewer(makeData({ pending: false, files: [labelledFile()], symbols: [] }));
    expandHunk();
    queueFetchResponse({ status: 500, body: {} });

    // `alpha` is exactly its span (5..7): collapsing it hides 6-7, but the
    // span's bar top and text stay on line 5, so the tree has nothing to
    // add and the box is the summary line alone.
    clickEl(chevronOnLine(5));
    await tick();
    expect([6, 7].map((n) => rowOfLine(n).style.display)).toEqual(["none", "none"]);
    const text = document.querySelector<HTMLElement>('.span-text[data-span-id="H0_0:span:5-7"]')!;
    expect(text.style.display).toBe("");
    expect(text.style.gridRow).toBe("2");
    expect(foldBoxOf(5).querySelector(".label-tree")).toBeNull();
    expect(foldBoxOf(5).querySelector(".fold-summary")!.textContent).toContain("method Foo.alpha — ");
  });

  test("a region with nothing labelled inside it shows its summary line alone", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    queueFetchResponse({ status: 200, body: { summary: "renames the column" } });
    clickEl(chevrons()[0]);
    await tick();
    const box = document.querySelector(".annot-box")!;
    expect(box.querySelector(".fold-summary")!.textContent).toBe("renames the column");
    expect(box.querySelector(".label-tree")).toBeNull();
  });

  test("regions the rendered rows never reach attach nothing", async () => {
    // Two whole-file regions elsewhere in the file: neither has a row on
    // screen, so no chevron, and nothing is derived from the hunk's own
    // indentation either.
    await bootViewer(makeData({
      pending: false,
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok", head_line_count: 40,
        symbols: { added: [], modified: [], removed: [] },
        fold_regions: [
          region({ right_start: 1, right_end: 5, qualified_name: "a", kind: "function" }),
          region({ right_start: 30, right_end: 40, qualified_name: "z", kind: "function" }),
        ],
        hunks: [makeHunkBlock("H0_0", "ok", {
          old_start: 10, old_count: 2, new_start: 10, new_count: 2,
          rows: [
            { kind: "ctx", old_line: 10, new_line: 10, old_text: "def mid():", new_text: "def mid():" },
            { kind: "pair", old_line: 11, new_line: 11, old_text: "    return 1", new_text: "    return 2" },
          ],
        })],
      }],
    }));
    expandHunk();
    expect(chevrons()).toHaveLength(0);
  });

  test("server's broadcast back to the requesting tab does not pop the fold open", async () => {
    // The server publishes a `fold-summary` SSE event to every
    // subscriber after handling the POST — including the tab that
    // issued it. The fold must stay closed with the summary in place.
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
    // The SSE handler re-attaches the fold chrome over the same rows;
    // the rebuilt fold box carries the streamed value and the fold is
    // still open, as the reviewer left it.
    const box = document.querySelector(".annot-box");
    expect(box?.textContent).toBe("remote summary");
    expect(document.querySelectorAll(".fold-chev")).toHaveLength(1);
    expect((document.querySelector(".fold-chev") as SVGElement).classList.contains("open")).toBe(true);
  });

  test("a fold-summary SSE event for a collapsed fold keeps it collapsed", async () => {
    await bootViewer(dataWithFold());
    expandHunk();
    // Collapse without a summary request racing: the POST fails fast.
    queueFetchResponse({ status: 500, body: { error: "boom" } });
    clickEl(document.querySelector(".fold-chev") as SVGElement);
    await tick();
    lastEventSource().dispatch("fold-summary", {
      file_idx: 0, context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2, summary: "from another tab",
    });
    const marker = document.querySelector(".fold-chev") as SVGElement;
    expect(marker.classList.contains("open")).toBe(false);
    expect(newRows()[1].style.display).toBe("none");
    expect(document.querySelector(".annot-box")?.textContent).toBe("from another tab");
  });

  test("re-attaching over cached rows replaces the chevrons rather than doubling them", async () => {
    // A repaint rebuilds the `.file` around the diff pane's cached
    // `.diff`; the fold pass runs again over the same rows.
    await bootViewer(dataWithFold());
    expandHunk();
    expect(chevrons()).toHaveLength(1);
    (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
    (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
    expect(chevrons()).toHaveLength(1);
    expect(document.querySelectorAll(".annot-box")).toHaveLength(1);
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

  // --- Collapse-only dismissal ------------------------------------------
  // The × and page-level Esc hide the drawer and keep everything: the
  // transcript nodes, and the server-side conversation the prompt's own
  // Esc drops. Returning to the prompt is the way back in.

  /** A finished turn in the drawer — where every dismissal case starts. */
  async function askAndAnswer(question = "why pagination?"): Promise<void> {
    const id = await ask(question);
    lastEventSource().dispatch("console-done", { console_id: id, answer: "because pages" });
  }

  const drawer = (): HTMLElement =>
    document.querySelector(".console-drawer") as HTMLElement;
  const collapsed = (): boolean => drawer().classList.contains("hidden");
  const closeBtn = (): HTMLButtonElement =>
    document.querySelector(".console-close") as HTMLButtonElement;
  const resetPosted = (): boolean =>
    fetchCalls.some((c) => c.url.includes("/console/reset"));

  test("the × hides the drawer and keeps the conversation", async () => {
    await bootViewer(makeData({ pending: false }));
    installStylesheet();
    await askAndAnswer();
    expect(collapsed()).toBe(false);
    // In the drawer's own scroller, sticky: a long transcript never
    // scrolls the way out of reach.
    expect(closeBtn().parentElement).toBe(drawer());
    expect(getComputedStyle(closeBtn()).position).toBe("sticky");
    expect(getComputedStyle(drawer()).overflowY).toBe("auto");

    closeBtn().dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(collapsed()).toBe(true);
    expect(document.querySelector(".console-q")?.textContent).toBe("why pagination?");
    expect(document.querySelector(".console-a .console-text")?.textContent)
      .toContain("because pages");
    expect(resetPosted()).toBe(false);
  });

  test("returning to the prompt brings the hidden transcript back", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;
    await askAndAnswer();
    closeBtn().click();
    expect(collapsed()).toBe(true);

    input.focus();
    expect(collapsed()).toBe(false);
    expect(document.querySelector(".console-q")).not.toBeNull();

    // And typing does, for a browser that leaves focus in the prompt
    // when the × is clicked — no focus event in between.
    closeBtn().click();
    input.value = "and the cursor?";
    input.dispatchEvent(new Event("input"));
    expect(collapsed()).toBe(false);
  });

  test("Ctrl-P opens a drawer that has a transcript, not an empty one", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;
    const ctrlP = (): void => window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "p", ctrlKey: true, bubbles: true }),
    );

    // Nothing asked yet: the prompt takes focus, and no bare strip
    // opens above the footer.
    ctrlP();
    expect(document.activeElement).toBe(input);
    expect(collapsed()).toBe(true);

    await askAndAnswer();
    closeBtn().click();
    ctrlP();
    expect(collapsed()).toBe(false);
  });

  test("page-level Esc hides the drawer without dropping the conversation", async () => {
    await bootViewer(makeData({ pending: false }));
    await askAndAnswer();

    // Focus anywhere but the prompt: the press render.ts routes.
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect(collapsed()).toBe(true);
    expect(document.querySelector(".console-q")).not.toBeNull();
    expect(resetPosted()).toBe(false);
  });

  test("the help overlay is dismissed before the drawer", async () => {
    await bootViewer(makeData({ pending: false }));
    await askAndAnswer();
    const overlay = document.getElementById("help-overlay")!;
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "?", bubbles: true }));
    expect(overlay.classList.contains("hidden")).toBe(false);

    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(overlay.classList.contains("hidden")).toBe(true);
    expect(collapsed()).toBe(false);

    // The next press is the drawer's.
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(collapsed()).toBe(true);
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
    window.location.hash = "#fold=code";      // the code, not the summaries
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
    window.location.hash = "#fold=code";
    await bootViewer(oneSidedData("deleted"));
    installStylesheet();

    const diff = document.querySelector("#app .file .diff") as HTMLElement;
    expect(diff.classList.contains("diff-only-old")).toBe(true);
    expect(getComputedStyle(diff.querySelector(".half-new") as HTMLElement).display).toBe("none");
    expect(getComputedStyle(diff.querySelector(".half-old") as HTMLElement).display).toBe("grid");
  });

  test("a modified file keeps both halves", async () => {
    window.location.hash = "#fold=code";
    await bootViewer(makeData({ pending: false }));   // a.py, pair rows
    installStylesheet();

    const diff = document.querySelector("#app .file .diff") as HTMLElement;
    expect(diff.className).toBe("diff");
    expect(getComputedStyle(diff.querySelector(".half-old") as HTMLElement).display).toBe("grid");
    // Two tracks, each half of the width less/plus its share of the span
    // gutter (zero here — the file has no span).
    const tracks = getComputedStyle(diff).gridTemplateColumns.split(/\)\s+minmax/);
    expect(tracks).toHaveLength(2);
    expect(tracks[0]).toContain("50% - ");
    expect(tracks[1]).toContain("50% + ");
  });

  test("a comment on an added file's line anchors on the new side", async () => {
    window.location.hash = "#fold=code";
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
        adds: 1, dels: 1, summary: "", head_line_count: null,
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
    window.location.hash = "#fold=code";  // expand the diff body so the round-trip row renders
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
    (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
    expect(divider()).toBe(el);
  });

  test("dragging writes the sidebar's basis and stores the width", async () => {
    await bootViewer(makeData({ pending: false }));
    dragDivider(divider(), 0, 300);
    expect(basis()).toBe("300px");
    expect(localStorage.getItem("scr-sidebar-width")).toBe("300");
  });

  test("the page stops selecting while the boundary is moving", async () => {
    // A captured pointer spends the drag over prose it is not aiming at.
    // The mark is on the page rather than a cancelled pointerdown: that
    // cancel would take the double-click reset with it.
    await bootViewer(makeData({ pending: false }));
    installStylesheet();
    const el = divider();
    const page = document.documentElement;
    el.dispatchEvent(new MouseEvent("pointerdown", { clientX: 0, button: 0, bubbles: true }));
    expect(el.classList.contains("dragging")).toBe(true);
    expect(getComputedStyle(page).userSelect).toBe("none");

    el.dispatchEvent(new MouseEvent("pointermove", { clientX: 300, bubbles: true }));
    el.dispatchEvent(new MouseEvent("pointerup", { clientX: 300, bubbles: true }));
    expect(el.classList.contains("dragging")).toBe(false);
    expect(page.classList.contains("dragging-divider")).toBe(false);
    expect(basis()).toBe("300px");
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
    (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
    const before = window.location.hash;
    expect(before).toContain("fold=code");

    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    // The level is untouched while in the mode; only `mode=` flips.
    expect(window.location.hash).toContain("fold=code");
    expect(window.location.hash).toContain("mode=overview");

    btn.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(window.location.hash).toBe(before);
    expect(document.querySelector('.fold-slider button[data-fold="code"]')!.classList.contains("active"))
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
    window.history.replaceState(null, "", "#fold=code&mode=diff");
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    expect(document.querySelector("#app .file")).not.toBeNull();
    expect(document.querySelector("#app .explainer")).toBeNull();
    expect(window.location.hash).toContain("fold=code");
    expect(window.location.hash).toContain("mode=diff");
  });

  test("mode=overview in the URL restores the mode and the level under it", async () => {
    window.history.replaceState(null, "", "#fold=code&mode=overview");
    await bootWithExplainer({ status: 200, body: DOC }, { pending: false });
    expect(document.querySelectorAll("#app .explainer-map-row")).toHaveLength(1);
    expect(window.location.hash).toContain("fold=code");
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector('.fold-slider button[data-fold="code"]')!.classList)
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
      (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expectDiffAt("code");
      expect(document.querySelector("#app .explainer-detail")).toBeNull();
      expect(document.querySelector('.fold-slider button[data-fold="code"]')!.classList)
        .toContain("active");
      expect(Array.from((document.getElementById("overview-btn") as HTMLButtonElement).classList))
        .not.toContain("active");
    });

    test("keys 1-3 do the same", async () => {
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
      (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));

      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect(document.querySelectorAll("#app .explainer-map-row")).toHaveLength(1);
      expect(window.location.hash).toContain("mode=overview");
      // And the level the press picked is what the diff is waiting at.
      expect(window.location.hash).toContain("fold=code");
    });

    test("the slider says where a press lands while the pane is the document", async () => {
      await bootIntoDocument();
      const code = document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement;
      expect(code.title).toBe("Leave the document and read the diff at this level");
      // The level highlight is the level a press lands on, so it stays.
      expect(document.querySelector('.fold-slider button[data-fold="hunks"]')!.classList)
        .toContain("active");

      // Leaving restores the markup's own title — empty in this
      // harness's header, which is what says the sentence came off.
      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect((document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).title)
        .toBe("");
    });
  });

  // --- the detail panel ------------------------------------------------

  describe("a reference opens beside the document", () => {
    const FILES = [
      ...["a.py", "b.py"].map((path, i) => ({
        id: `F${i}`, path, status: "modified", language: "python",
        adds: 1, dels: 1, summary: "", head_line_count: null,
        symbols: { added: [], modified: [], removed: [] },
        // The hunk's two rows are one server-computed fold region.
        fold_regions: [{
          context: "both", right_start: 1, right_end: 2, left_start: 1, left_end: 2,
          has_changes: true, qualified_name: "guard", kind: "function", summary: "",
        }],
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
      // Code, not the label tree an open hunk shows below `off`.
      expect(hunk.querySelector(".diff")).not.toBeNull();

      // The panel's overrides are its own: the diff is where the
      // reviewer left it, which is what makes the return trip free.
      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      expect(document.querySelector('.hunk[data-id="H0_0"]')!.classList.contains("folded"))
        .toBe(true);
    });

    test("fold regions attach inside the panel's copy of a file", async () => {
      const panel = await bootWithPanel();
      hunkRef().click();

      const chevron = panel.querySelector(".fold-chev") as SVGElement | null;
      expect(chevron).not.toBeNull();
      const rows = Array.from(panel.querySelectorAll<HTMLElement>(".half-new .row:not(.row-annotation)"));
      expect(rows).toHaveLength(2);
      chevron!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      expect(rows.map((r) => r.style.display)).toEqual(["", "none"]);
      expect(panel.querySelector(".annot-box")!.textContent).toBe("function guard — summarising…");

      // Back in the diff, the file's own copy carries its own chevron,
      // open: the panel's fold is the panel's.
      (document.getElementById("overview-btn") as HTMLButtonElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));
      (document.querySelector('.fold-slider button[data-fold="code"]') as HTMLElement).click();
      const diffFile = document.querySelector('#app .file[data-id="F0"]') as HTMLElement;
      const diffChevrons = diffFile.querySelectorAll(".fold-chev");
      expect(diffChevrons).toHaveLength(1);
      expect(diffChevrons[0].classList.contains("open")).toBe(true);
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

    test("Esc hides the console drawer before it closes the panel", async () => {
      const panel = await bootWithPanel();
      mapRow(0).click();
      const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;
      input.value = "what am I looking at?";
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await new Promise<void>((r) => setTimeout(r, 0));
      const drawer = document.querySelector(".console-drawer") as HTMLElement;
      expect(drawer.classList.contains("hidden")).toBe(false);

      // One surface per press: the drawer goes, the panel stays.
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      expect(drawer.classList.contains("hidden")).toBe(true);
      expect(panel.hidden).toBe(false);

      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      expect(panel.hidden).toBe(true);
    });

    test("Open in diff on a file reference is a focus on the file's hunks", async () => {
      const panel = await bootWithPanel();
      mapRow(1).click();
      (panel.querySelector(".explainer-detail-open") as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));

      expect(window.location.hash).toContain("mode=diff");
      expect(document.querySelector("#app .explainer")).toBeNull();
      expect(document.querySelector("#app .explainer-detail")).toBeNull();
      expect(document.querySelector('#app .file[data-id="F1"]')).not.toBeNull();
      // The file's hunk is open to its code; the others sit at the level
      // (`hunks`), and nothing was stored: the hash carries no override.
      expect(document.querySelector('#app .hunk[data-id="H1_0"] .diff')).not.toBeNull();
      expect(document.querySelector('#app .hunk[data-id="H0_0"] .diff')).toBeNull();
      expect(document.querySelector('#app .hunk[data-id="H0_0"]')!.classList.contains("folded")).toBe(true);
      expect(window.location.hash).toBe("#fold=hunks&mode=diff");

      // The slider folds it back to level.
      (document.querySelector('.fold-slider button[data-fold="hunks"]') as HTMLElement).click();
      expect(document.querySelector('#app .hunk[data-id="H1_0"] .diff')).toBeNull();
    });

    test("Open in diff on a hunk reference focuses that hunk alone", async () => {
      const panel = await bootWithPanel();
      hunkRef().click();
      (panel.querySelector(".explainer-detail-open") as HTMLElement).click();
      await new Promise<void>((r) => setTimeout(r, 0));

      expect(window.location.hash).toBe("#fold=hunks&mode=diff");
      expect(document.querySelector('#app .hunk[data-id="H0_0"] .diff')).not.toBeNull();
      expect(document.querySelector('#app .hunk[data-id="H1_0"] .diff')).toBeNull();
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

    // --- rendered mode beside the document -------------------------------
    // A `.md` reference opens in the panel with rendered mode's state its
    // own: the flip, the fold ladder, the chevrons and the outline act on
    // the panel and leave the diff pane where the reviewer left it.

    describe("a markdown reference", () => {
      const MD = "# Head\n\n" + [1, 2, 3, 4, 5, 6].map((n) => `para ${n}`).join("\n\n");
      const MD_FILES = [{
        id: "F0", path: "docs/x.md", status: "modified", language: "markdown",
        adds: 1, dels: 1, summary: "", head_line_count: null,
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0")],
      }];

      /** Open the document with a single `.md` file, click its Map row to
       *  put it in the panel, and flip the panel's copy to rendered mode. */
      async function panelRendered(): Promise<HTMLElement> {
        await bootWithExplainer({ status: 200, body: DOC }, { pending: false, files: MD_FILES });
        const panel = document.querySelector("#app .explainer-detail") as HTMLElement;
        mapRow(0).click();
        queueFetchResponse({
          status: 200,
          body: { file_idx: 0, path: "docs/x.md", base: MD, head: MD },
        });
        (panel.querySelector(".md-toggle") as HTMLElement).click();
        await new Promise<void>((r) => setTimeout(r, 0));
        return panel;
      }

      /** Leave the mode and land on the diff pane's copy of the file. */
      async function toDiffPane(): Promise<HTMLElement> {
        (document.getElementById("overview-btn") as HTMLButtonElement).click();
        await new Promise<void>((r) => setTimeout(r, 0));
        return document.querySelector('#app .file[data-id="F0"]') as HTMLElement;
      }

      test("flipping it in the panel leaves the diff pane on the text diff", async () => {
        const panel = await panelRendered();
        expect(panel.querySelector(".rmd-grid")).not.toBeNull();
        expect(panel.querySelector(".md-toggle")!.textContent).toBe("Diff");

        const inDiff = await toDiffPane();
        expect(inDiff.querySelector(".rmd-grid")).toBeNull();
        expect(inDiff.querySelector(".md-toggle")!.textContent).toBe("Rendered");
      });

      test("the fold ladder repaints the panel", async () => {
        const panel = await panelRendered();
        expect(panel.querySelector(".rmd-fold")).not.toBeNull();
        expect(panel.querySelector(".rmd-ladder-btn.active")!.textContent).toBe("Runs");

        Array.from(panel.querySelectorAll<HTMLElement>(".rmd-ladder-btn"))
          .find((b) => b.textContent === "Open")!.click();

        expect(panel.querySelector(".rmd-fold")).toBeNull();
        expect(panel.querySelector(".rmd-ladder-btn.active")!.textContent).toBe("Open");
        // The document beside it is untouched — the repaint was the
        // panel's, not the pane's.
        expect(document.querySelector("#app .explainer")!.textContent).toContain("Ground.");
      });

      test("a chevron reveal repaints the panel", async () => {
        const panel = await panelRendered();
        // "⋯ N unchanged blocks ⋯"
        const hidden = (): number => Number(
          /(\d+) unchanged/.exec(panel.querySelector(".rmd-fold-label")!.textContent || "")![1]);
        const before = hidden();
        expect(before).toBeGreaterThan(1);

        (panel.querySelector(".rmd-fold-chev-top") as HTMLElement).click();

        expect(hidden()).toBe(before - 1);
      });

      test("an outline entry repaints the panel", async () => {
        const panel = await panelRendered();
        expect(panel.querySelector(".rmd-fold")).not.toBeNull();

        (panel.querySelector(".rmd-outline-entry") as HTMLElement).click();

        expect(panel.querySelector(".rmd-fold")).toBeNull();
      });

      test("the panel's fold level does not follow the reviewer into the diff", async () => {
        const panel = await panelRendered();
        Array.from(panel.querySelectorAll<HTMLElement>(".rmd-ladder-btn"))
          .find((b) => b.textContent === "Open")!.click();

        // The diff pane's copy flips on its own — one fetch served both,
        // so the source cache is shared even though the view state is not.
        const inDiff = await toDiffPane();
        const hits = fetchCalls.filter((c) => c.url.includes("/file-text")).length;
        (inDiff.querySelector(".md-toggle") as HTMLElement).click();
        await new Promise<void>((r) => setTimeout(r, 0));
        expect(fetchCalls.filter((c) => c.url.includes("/file-text")).length).toBe(hits);

        const file = document.querySelector('#app .file[data-id="F0"]') as HTMLElement;
        expect(file.querySelector(".rmd-ladder-btn.active")!.textContent).toBe("Runs");
        expect(file.querySelector(".rmd-fold")).not.toBeNull();
      });
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

    // --- the document scrolls in its own column --------------------------
    // The shared far-right scrollbar was the window's, past the panel and
    // its own. Each column now scrolls inside itself.

    test("the document cell is a scroller bound to the viewport", async () => {
      const panel = await bootWithPanel();
      installStylesheet();
      const cs = getComputedStyle(docCell());
      expect(cs.overflowY).toBe("auto");
      // Vertical only: a figure's box breaks the measure by a few px, and
      // as `auto` that is a horizontal bar scrolling empty margin.
      expect(cs.overflowX).toBe("hidden");
      // The ceiling the panel and the sidebar already take, so the three
      // columns end together and each scrollbar is its own column's.
      expect(cs.maxHeight).toBe("calc(100vh - 56px)");
      expect(getComputedStyle(panel).maxHeight).toBe(cs.maxHeight);
      expect(getComputedStyle(document.getElementById("group-sidebar")!).maxHeight)
        .toBe(cs.maxHeight);
    });

    test("entering the mode hands the keyboard the column", async () => {
      await bootWithPanel();
      // Nothing else scrolls the prose: the window is spent, and the cell
      // is not a tab stop a reader could reach.
      expect(document.activeElement).toBe(docCell());
      expect(docCell().tabIndex).toBe(-1);

      // And the keys render.ts owns still arrive — it binds on `document`,
      // and the cell is neither an input nor a textarea. 1 in the mode is
      // the exit into the diff at that level.
      docCell().dispatchEvent(new KeyboardEvent("keydown", { key: "1", bubbles: true }));
      await new Promise<void>((r) => setTimeout(r, 0));
      expect(window.location.hash).toContain("fold=files");
      expect(window.location.hash).toContain("mode=diff");
    });

    test("a repaint leaves focus where the reader put it", async () => {
      await bootWithPanel();
      const divider = docDivider();
      divider.focus();
      await repaintProse();
      expect(document.activeElement).toBe(divider);
    });

    test("the panel opening, swapping and closing leaves the reader's place", async () => {
      const panel = await bootWithPanel();
      docCell().scrollTop = 400;

      mapRow(0).click();
      expect(docCell().scrollTop).toBe(400);
      mapRow(1).click();
      expect(docCell().scrollTop).toBe(400);
      (panel.querySelector(".explainer-detail-close") as HTMLElement).click();
      expect(docCell().scrollTop).toBe(400);
    });

    test("a repaint of the prose keeps the reader's place", async () => {
      // The repaint replaces the cell's whole child; the offset is the
      // cell's, and carrying it over is what stops a section write the
      // reader did not ask for throwing them back to the top.
      await bootWithPanel();
      const cell = docCell();
      cell.scrollTop = 400;
      await repaintProse();
      expect(document.querySelector("#app .explainer")!.textContent)
        .toContain("Ground, rewritten.");
      // The same cell, holding the same offset: the prose under it was
      // replaced, the scroller was not rebuilt.
      expect(docCell()).toBe(cell);
      expect(docCell().scrollTop).toBe(400);
    });

    /** A section write off the SSE bus: the repaint nobody asked for. */
    async function repaintProse(): Promise<void> {
      lastEventSource().dispatch("explainer", {
        ...PANEL_DOC,
        sections: PANEL_DOC.sections.map((s) =>
          s.kind === "background" ? { ...s, body: "Ground, rewritten. [H0_0]" } : s),
      });
      await new Promise<void>((r) => setTimeout(r, 0));
    }
  });
});

// --- Lazy disclosure (ADR 0008 slice 1) --------------------------------------
// Everything in a file is disclosable: a region between, above or below
// the hunks is laid out from the hunks' coordinates and reads its lines
// from /file-text when its chip is clicked, so nothing about a file's
// size or side puts a line out of reach. The fetched text is the cache
// rendered mode reads.

describe("lazy disclosure", () => {
  type Row = Record<string, unknown>;

  /** Line n of `path`'s text, as the fixtures spell it. */
  const lineText = (path: string, n: number): string => `${path}:${n}`;
  const textOf = (path: string, n: number, changed: Record<number, string> = {}): string =>
    Array.from({ length: n }, (_, i) => changed[i + 1] ?? lineText(path, i + 1)).join("\n") + "\n";

  /** A one-line replacement at `line` with three lines of context each
   *  side — the shape `git diff -U3` gives a one-line edit. */
  function editAt(id: string, path: string, line: number): Row {
    const ctx = (n: number): Row =>
      ({ kind: "ctx", old_line: n, new_line: n, old_text: lineText(path, n), new_text: lineText(path, n) });
    return makeHunkBlock(id, "", {
      old_start: line - 3, old_count: 7, new_start: line - 3, new_count: 7,
      rows: [
        ctx(line - 3), ctx(line - 2), ctx(line - 1),
        { kind: "pair", old_line: line, new_line: line, old_text: `old ${line}`, new_text: `new ${line}` },
        ctx(line + 1), ctx(line + 2), ctx(line + 3),
      ],
    });
  }

  function file(
    idx: number, path: string, status: string, headLines: number | null, hunks: Row[], extra: Row = {},
  ): Row {
    return {
      id: `F${idx}`, path, old_path: null, status, language: "python",
      adds: 0, dels: 0, summary: "", head_line_count: headLines,
      symbols: { added: [], modified: [], removed: [] },
      hunks, ...extra,
    };
  }

  const fold = (level: string): void =>
    (document.querySelector(`.fold-slider button[data-fold="${level}"]`) as HTMLElement).click();

  /** Every rendered line number on one side of a file element, with the
   *  content beside it. Empty cells (the other side of a one-sided row)
   *  are skipped. */
  function shown(el: Element, side: "old" | "new"): Map<number, string> {
    const out = new Map<number, string>();
    for (const row of el.querySelectorAll(`.half-${side} .row:not(.row-annotation)`)) {
      const ln = row.querySelector(`.cell-lineno-${side}`)!;
      if (ln.classList.contains("empty")) continue;
      out.set(Number(ln.textContent), row.querySelector(".cell-content")!.textContent || "");
    }
    return out;
  }

  const chipsOf = (el: Element): HTMLElement[] => Array.from(el.querySelectorAll<HTMLElement>(".gap-chip"));
  const fileEl = (id: string): HTMLElement => document.querySelector(`#app .file[data-id="${id}"]`) as HTMLElement;
  const fileTextHits = (): string[] =>
    fetchCalls.filter((c) => c.url.includes("/file-text")).map((c) => c.url);
  const contents = (el: Element, sel: string): (string | null)[] =>
    Array.from(el.querySelectorAll(sel)).map((c) => c.textContent);

  test("every line of both sides of every file is reachable through a chip", async () => {
    // Four shapes: a modified file past the old 5,000-line bundle cap
    // with two edits far apart; a deleted file (base side only); an
    // added file (head only); a rename with one edit.
    const BIG = 6000;
    const big = file(0, "big.py", "modified", BIG, [editAt("H0_0", "big.py", 100), editAt("H0_1", "big.py", 5900)]);
    const gone = file(1, "gone.py", "deleted", null, [makeHunkBlock("H1_0", "", {
      old_start: 1, old_count: 4, new_start: 0, new_count: 0,
      rows: [1, 2, 3, 4].map((n) => ({ kind: "del", old_line: n, new_line: null, old_text: lineText("gone.py", n), new_text: "" })),
    })]);
    const fresh = file(2, "new.py", "added", 3, [makeHunkBlock("H2_0", "", {
      old_start: 0, old_count: 0, new_start: 1, new_count: 3,
      rows: [1, 2, 3].map((n) => ({ kind: "ins", old_line: null, new_line: n, old_text: "", new_text: lineText("new.py", n) })),
    })]);
    const moved = file(3, "moved.py", "renamed", 10, [editAt("H3_0", "moved.py", 5)], { old_path: "orig.py" });
    await bootViewer(makeData({ pending: false, files: [big, gone, fresh, moved] }));
    fold("code");   // the hunks' own rows too, so the whole file can be counted

    // The chips are laid out and counted from the hunks alone; no text
    // has been fetched.
    expect(fileTextHits()).toHaveLength(0);
    expect(chipsOf(fileEl("F0")).map((c) => c.textContent)).toEqual([
      "⬆expand 96 lines above", "⋯expand 5793 hidden lines", "⬇expand 97 lines below",
    ]);
    expect(chipsOf(fileEl("F1"))).toHaveLength(0);   // the diff carries every base line
    expect(chipsOf(fileEl("F2"))).toHaveLength(0);   // and every head line
    expect(chipsOf(fileEl("F3")).map((c) => c.textContent)).toEqual([
      "⬆expand 1 line above", "⬇expand 2 lines below",
    ]);

    // Expand everything. One /file-text round trip per file, on its first
    // chip; the rest of that file's chips read the cache.
    const bigHead = textOf("big.py", BIG, { 100: "new 100", 5900: "new 5900" });
    const movedHead = textOf("moved.py", 10, { 5: "new 5" });
    for (const [idx, path, head] of [[0, "big.py", bigHead], [3, "moved.py", movedHead]] as const) {
      queueFileText(idx, path, null, head);
      for (const chip of chipsOf(fileEl(`F${idx}`))) { chip.click(); await tick(); }
      expect(chipsOf(fileEl(`F${idx}`))).toHaveLength(0);
    }
    expect(fileTextHits()).toEqual(["/file-text?file_idx=0", "/file-text?file_idx=3"]);

    // The whole of each side, line for line, in every file.
    const expectSide = (id: string, side: "old" | "new", text: string | null, n: number): void => {
      const got = shown(fileEl(id), side);
      expect(Array.from(got.keys()).sort((a, b) => a - b)).toEqual(Array.from({ length: n }, (_, i) => i + 1));
      if (text !== null) {
        const lines = text.split("\n");
        for (const [ln, content] of got) expect(content, `${id} ${side} line ${ln}`).toBe(lines[ln - 1]);
      }
    };
    expectSide("F0", "new", bigHead, BIG);
    expectSide("F0", "old", textOf("big.py", BIG, { 100: "old 100", 5900: "old 5900" }), BIG);
    expectSide("F1", "old", textOf("gone.py", 4), 4);
    expectSide("F1", "new", null, 0);
    expectSide("F2", "new", textOf("new.py", 3), 3);
    expectSide("F2", "old", null, 0);
    expectSide("F3", "new", movedHead, 10);
    expectSide("F3", "old", textOf("moved.py", 10, { 5: "old 5" }), 10);
  });

  test("a region's line numbers follow the hunk's end on both sides", async () => {
    // An edit that grows the file: below it old and new numbering differ
    // by the insertion, and the text arrives after the layout.
    const grow = makeHunkBlock("H0_0", "", {
      old_start: 2, old_count: 3, new_start: 2, new_count: 5,
      rows: [
        { kind: "ctx", old_line: 2, new_line: 2, old_text: "a.py:2", new_text: "a.py:2" },
        { kind: "ins", old_line: null, new_line: 3, old_text: "", new_text: "added 3" },
        { kind: "ins", old_line: null, new_line: 4, old_text: "", new_text: "added 4" },
        { kind: "ctx", old_line: 3, new_line: 5, old_text: "a.py:5", new_text: "a.py:5" },
        { kind: "ctx", old_line: 4, new_line: 6, old_text: "a.py:6", new_text: "a.py:6" },
      ],
    });
    await bootViewer(makeData({ pending: false, files: [file(0, "a.py", "modified", 8, [grow])] }));
    fold("code");
    queueFileText(0, "a.py", null, textOf("a.py", 8, { 3: "added 3", 4: "added 4" }));
    for (const chip of chipsOf(fileEl("F0"))) { chip.click(); await tick(); }

    expect(contents(fileEl("F0"), ".gap-expansion .half-new .row .cell-content"))
      .toEqual(["a.py:1", "a.py:7", "a.py:8"]);
    const oldOf = shown(fileEl("F0"), "old");
    const newOf = shown(fileEl("F0"), "new");
    // Below the hunk, new 7 is old 5 and new 8 is old 6.
    expect(newOf.get(7)).toBe("a.py:7");
    expect(oldOf.get(5)).toBe("a.py:7");
    expect(oldOf.get(6)).toBe("a.py:8");
    expect(Array.from(oldOf.keys()).sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  test("a demoted hunk's region interleaves its rows with fetched context", async () => {
    const f = file(0, "a.py", "modified", 20, [
      editAt("H0_0", "a.py", 4), editAt("H0_1", "a.py", 11), editAt("H0_2", "a.py", 17),
    ]);
    await bootViewer(makeData({
      pending: false, files: [f],
      symbols: [{ id: "SY0", title: "ends", rationale: "", hunk_ids: ["H0_0", "H0_2"] }],
    }));
    (document.querySelector('[data-axis="symbols"] .group-btn[data-pill-id="SY0"]') as HTMLElement).click();
    const between = chipsOf(fileEl("F0")).find((c) => c.textContent!.includes("hidden"))!;
    // H0_0 covers 1..7 and H0_2 covers 14..20, so the region is 8..13:
    // the demoted H0_1's seven rows (8..14) minus the one H0_2 owns.
    expect(between.textContent).toBe("⋯expand 7 hidden lines");
    queueFileText(0, "a.py", null, textOf("a.py", 20, { 4: "new 4", 11: "new 11", 17: "new 17" }));
    between.click();
    await tick();
    const exp = fileEl("F0").querySelector(".gap-expansion")!;
    expect(exp.querySelector(".hunk-header")).toBeNull();
    expect(contents(exp, ".half-new .row .cell-content"))
      .toEqual(["a.py:8", "a.py:9", "a.py:10", "new 11", "a.py:12", "a.py:13", "a.py:14"]);
  });

  test("the chip shows the wait, and a second click during it fetches nothing more", async () => {
    await bootViewer(makeData({ pending: false, files: [file(0, "a.py", "modified", 9, [editAt("H0_0", "a.py", 5)])] }));
    let release: (r: Response) => void = () => {};
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        return new Promise<Response>((r) => { release = r; });
      }) as typeof fetch);
    const chip = chipsOf(fileEl("F0"))[0];
    chip.click();
    expect(chip.classList.contains("loading")).toBe(true);
    expect(chip.textContent).toContain("expand 1 line above — loading");
    chip.click();
    await tick();
    expect(fileTextHits()).toHaveLength(1);
    expect(fileEl("F0").querySelector(".gap-expansion")).toBeNull();

    release({
      ok: true, status: 200,
      json: () => Promise.resolve({ file_idx: 0, path: "a.py", base: null, head: textOf("a.py", 9, { 5: "new 5" }) }),
    } as Response);
    await tick();
    expect(fileEl("F0").querySelector(".gap-expansion .cell-content")!.textContent).toBe("a.py:1");
  });

  test("a failed fetch is said on the chip, and the next click retries", async () => {
    await bootViewer(makeData({ pending: false, files: [file(0, "a.py", "modified", 9, [editAt("H0_0", "a.py", 5)])] }));
    queueFetchResponse({ status: 500, body: { error: "boom" } });
    const chip = chipsOf(fileEl("F0"))[0];
    chip.click();
    await tick();
    expect(chip.isConnected).toBe(true);
    expect(chip.classList.contains("failed")).toBe(true);
    expect(chip.classList.contains("loading")).toBe(false);
    expect(chip.textContent).toContain("could not load: GET /file-text -> 500 (click to retry)");
    expect(fileEl("F0").querySelector(".gap-expansion")).toBeNull();

    queueFileText(0, "a.py", null, textOf("a.py", 9, { 5: "new 5" }));
    chip.click();
    await tick();
    expect(chip.isConnected).toBe(false);
    expect(fileEl("F0").querySelector(".gap-expansion .cell-content")!.textContent).toBe("a.py:1");
    expect(fileTextHits()).toHaveLength(2);
  });

  test("text that cannot cover the region fails the chip rather than padding it", async () => {
    // A modified file whose head came back null, and one whose head is
    // shorter than the diff recorded (a dirty-tree review whose file was
    // edited under it): neither expands to blank rows.
    await bootViewer(makeData({ pending: false, files: [
      file(0, "a.py", "modified", 9, [editAt("H0_0", "a.py", 5)]),
      file(1, "b.py", "modified", 9, [editAt("H1_0", "b.py", 5)]),
    ] }));
    queueFileText(0, "a.py", "base only\n", null);
    const a = chipsOf(fileEl("F0"))[1];   // below: line 9
    a.click();
    await tick();
    expect(a.classList.contains("failed")).toBe(true);
    expect(a.textContent).toContain("a.py: no post-image text to expand");

    queueFileText(1, "b.py", null, textOf("b.py", 8, { 5: "new 5" }));
    const b = chipsOf(fileEl("F1"))[1];
    b.click();
    await tick();
    expect(b.classList.contains("failed")).toBe(true);
    expect(b.textContent).toContain("b.py: line 9 is past the end of the file (8 lines)");
    expect(document.querySelector(".gap-expansion")).toBeNull();
  });

  test("a chip's fetch serves rendered mode, and rendered mode's serves the chips", async () => {
    const MD = "# T\n\npara\n\nend\n";
    const md = file(0, "docs/x.md", "modified", 5, [makeHunkBlock("H0_0", "", {
      old_start: 2, old_count: 3, new_start: 2, new_count: 3,
      rows: [
        { kind: "ctx", old_line: 2, new_line: 2, old_text: "", new_text: "" },
        { kind: "pair", old_line: 3, new_line: 3, old_text: "old", new_text: "para" },
        { kind: "ctx", old_line: 4, new_line: 4, old_text: "", new_text: "" },
      ],
    })], { language: "markdown" });
    await bootViewer(makeData({ pending: false, files: [md] }));

    queueFileText(0, "docs/x.md", MD.replace("para", "old"), MD);
    chipsOf(fileEl("F0"))[0].click();   // above: line 1
    await tick();
    expect(fileEl("F0").querySelector(".gap-expansion .cell-content")!.textContent).toBe("# T");
    expect(fileTextHits()).toHaveLength(1);

    (fileEl("F0").querySelector(".md-toggle") as HTMLElement).click();
    await tick();
    expect(fileEl("F0").querySelector(".rmd-grid")).not.toBeNull();
    expect(fileTextHits()).toHaveLength(1);

    (fileEl("F0").querySelector(".md-toggle") as HTMLElement).click();
    await tick();
    chipsOf(fileEl("F0"))[1].click();   // below: line 5
    await tick();
    expect(contents(fileEl("F0"), ".gap-expansion .half-new .cell-content")).toEqual(["end"]);
    expect(fileTextHits()).toHaveLength(1);
  });

  test("a region expanded in the explainer's panel reads the diff pane's fetch, and stays in the panel", async () => {
    const DOC = {
      version: 1, base_sha: "b", head_sha: "h", verdict: "narrate", verdict_note: "",
      figure_family: "", cast: [], toy_data: false, dropped_refs: 0,
      sections: [{
        id: "map", kind: "map", title: "Map", state: "ready", body: "",
        refs: [{ kind: "file", id: "F0" }],
        map_rows: [{ ref: { kind: "file", id: "F0" }, why: "the contract" }],
        subsections: [],
      }],
    };
    const f = file(0, "a.py", "modified", 9, [editAt("H0_0", "a.py", 5)]);
    // A document exists, so the viewer opens on it; step into the diff.
    await bootViewer(
      makeData({ pending: false, explainer: true, files: [f] }),
      { explainer: { status: 200, body: DOC } },
    );
    await tick();
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await tick();
    queueFileText(0, "a.py", null, textOf("a.py", 9, { 5: "new 5" }));
    chipsOf(fileEl("F0"))[0].click();
    await tick();
    expect(fileEl("F0").querySelector(".gap-expansion")).not.toBeNull();
    expect(fileTextHits()).toHaveLength(1);

    // Into the document; the panel renders its own copy of the file with
    // its regions collapsed, and expands one off the cache.
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await tick();
    (document.querySelector(".explainer-map-row .explainer-ref") as HTMLElement).click();
    const panel = document.querySelector("#app .explainer-detail") as HTMLElement;
    const inPanel = panel.querySelector('.file[data-id="F0"]') as HTMLElement;
    expect(chipsOf(inPanel)).toHaveLength(2);
    chipsOf(inPanel)[1].click();   // below: line 9
    await tick();
    expect(inPanel.querySelectorAll(".gap-expansion")).toHaveLength(1);
    expect(inPanel.querySelector(".gap-expansion .cell-content")!.textContent).toBe("a.py:9");
    expect(fileTextHits()).toHaveLength(1);

    // Back to the diff: its regions are as the pane draws them, not as
    // the panel left its own.
    (document.getElementById("overview-btn") as HTMLButtonElement).click();
    await tick();
    expect(chipsOf(fileEl("F0"))).toHaveLength(2);
    expect(fileEl("F0").querySelector(".gap-expansion")).toBeNull();
  });
});
