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
// viewer.js is an IIFE — it has no exports and grabs everything off
// `document` / `window` at module load. We mount the same DOM template
// the Jinja template emits, install a scr-data <script>, stub
// EventSource + fetch on the global, then read viewer.js as a string
// and eval() it. The eval (rather than `import`) is what gives us a
// clean re-execution per test without fighting Vitest's module cache.

import fs from "node:fs";
import path from "node:path";
import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";

import "../../semantic_code_review/viewer/assets/annotations";

const VIEWER_SRC = fs.readFileSync(
  path.resolve(process.cwd(), "semantic_code_review/viewer/assets/viewer.js"),
  "utf-8",
);

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

function queueFetchResponse(r: FetchResponse): void {
  fetchResponses.push(r);
}

// --- Boot helper ----------------------------------------------------------

interface ViewerData {
  version?: string;
  pending?: boolean;
  pr?: Record<string, unknown>;
  smells_catalogue?: Record<string, unknown>;
  files?: Array<Record<string, unknown>>;
  groups?: Array<Record<string, unknown>>;
}

function bootViewer(data: ViewerData): void {
  // Build the same skeleton the Jinja template emits, minus the
  // bits viewer.js doesn't touch (highlight.js asset, help overlay
  // body, etc — we keep the IDs so qS lookups succeed).
  document.head.innerHTML = `
    <meta name="scr-session-endpoint" content="http://test">
  `;
  document.body.innerHTML = `
    <header class="pr-bar">
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
    <script type="application/json" id="scr-data">${JSON.stringify(data)}</script>
  `;
  // The fixture-loaded annotations module registers window.ScrAnnotations.
  // viewer.js depends on it.
  // Execute viewer.js as a fresh IIFE in the current realm so it picks
  // up our stubs. `new Function` ensures strict-mode and a clean scope.
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  new Function(VIEWER_SRC)();
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
    ...overrides,
  };
}

// --- Global hooks ----------------------------------------------------------

beforeEach(() => {
  eventSourceInstances.length = 0;
  fetchResponses.length = 0;
  fetchCalls.length = 0;
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    EventSourceStub as unknown as typeof EventSource;
  vi.spyOn(globalThis, "fetch").mockImplementation(((url: string, init?: RequestInit) => {
    fetchCalls.push({ url, init });
    const next = fetchResponses.shift() ?? { status: 200, body: {} };
    return Promise.resolve({
      status: next.status,
      json: () => Promise.resolve(next.body),
    } as Response);
  }) as typeof fetch);
});

afterEach(() => {
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});


describe("pending boot", () => {
  test("progress strip shows total + every square starts queued", () => {
    bootViewer(makeData({
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

  test("hunks with empty intent render the 'queued' placeholder", () => {
    bootViewer(makeData());
    const intent = document.querySelector(".hunk-intent")!;
    expect(intent.classList.contains("queued")).toBe(true);
    expect(intent.textContent).toBe("queued");
  });

  test("generated/binary files are excluded from the progress grid", () => {
    bootViewer(makeData({
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
  test("hunk-start flips the square + intent slot to 'running'", () => {
    bootViewer(makeData());
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

  test("hunk completion patches the intent and marks the square ok", () => {
    bootViewer(makeData());
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

  test("hunk failure marks the square failed and shows the re-run copy", () => {
    bootViewer(makeData());
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

  test("overview event populates the themes axis and the file summary", () => {
    bootViewer(makeData({
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

  test("by-file axis renders from boot with one pill per file and filters on click", () => {
    bootViewer(makeData({
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

    // Click the a.py pill — only its two hunks remain visible; b's
    // hunk is hidden and its file element collapses.
    (pills[0] as HTMLElement).click();
    const h0 = document.querySelector('.hunk[data-id="H0_0"]') as HTMLElement;
    const h1 = document.querySelector('.hunk[data-id="H1_0"]') as HTMLElement;
    expect(h0.style.display).not.toBe("none");
    expect(h1.style.display).toBe("none");
    expect((pills[0] as HTMLElement).classList.contains("active")).toBe(true);

    // Clicking it again clears the filter.
    (pills[0] as HTMLElement).click();
    expect(h1.style.display).not.toBe("none");
    expect(document.querySelector(".group-btn-all")!.classList.contains("active")).toBe(true);
  });

  test("done event hides the progress strip and clears pending", () => {
    bootViewer(makeData());
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


describe("lazy fold summaries", () => {
  function dataWithFold(): ViewerData {
    return makeData({
      pending: false,  // post-augment state
      files: [{
        id: "F0", path: "a.py", status: "modified", language: "python",
        adds: 1, dels: 1, summary: "ok", head_lines: null,
        symbols: { added: [], modified: [], removed: [] },
        hunks: [makeHunkBlock("H0_0", "real intent", {
          fold_regions: [
            { header_idx: 0, body_start_idx: 1, body_end_idx: 1, new_start: 1, new_end: 2, has_changes: true, summary: "" },
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
    bootViewer(dataWithFold());
    expandHunk();
    queueFetchResponse({
      status: 200,
      body: { hunk_id: "H0_0", new_start: 1, new_count: 2, summary: "renames the column" },
    });

    const marker = document.querySelector(".fold-chev") as SVGElement | null;
    expect(marker).not.toBeNull();
    // SVGElement has no .click() in jsdom; dispatch the event directly.
    // The default is OPEN; one click collapses → fires the request.
    clickEl(marker!);
    expect(marker!.classList.contains("open")).toBe(false);

    const foldCalls = fetchCalls.filter((c) => c.url.includes("/fold-summary"));
    expect(foldCalls).toHaveLength(1);
    const body = JSON.parse((foldCalls[0].init!.body as string));
    expect(body).toEqual({ hunk_id: "H0_0", new_start: 1, new_count: 2 });

    // Let the fetch promise resolve.
    await new Promise((r) => setTimeout(r, 0));
    const box = document.querySelector(".annot-box");
    expect(box?.textContent).toBe("renames the column");
    expect(box?.classList.contains("pending")).toBe(false);
  });

  test("repeated fold-close while a request is in flight does not re-fire", async () => {
    bootViewer(dataWithFold());
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

    resolveFetch({ status: 200, body: { hunk_id: "H0_0", new_start: 1, new_count: 2, summary: "done" } });
    await new Promise((r) => setTimeout(r, 0));
    expect(document.querySelector(".annot-box")?.textContent).toBe("done");
  });

  test("server's broadcast back to the requesting tab does not pop the fold open", async () => {
    // The server publishes a `fold-summary` SSE event to every
    // subscriber after handling the POST — including the tab that
    // issued it. Re-rendering the hunk on receipt would rebuild the
    // fold in its default-open state and clobber the user's collapse.
    bootViewer(dataWithFold());
    expandHunk();
    queueFetchResponse({
      status: 200,
      body: { hunk_id: "H0_0", new_start: 1, new_count: 2, summary: "wraps in try/except" },
    });

    const marker = document.querySelector(".fold-chev") as SVGElement;
    clickEl(marker);   // collapse → POST
    expect(marker.classList.contains("open")).toBe(false);

    // SSE arrives for the same region with the same payload.
    lastEventSource().dispatch("fold-summary", {
      hunk_id: "H0_0", new_start: 1, new_count: 2, summary: "wraps in try/except",
    });
    await new Promise((r) => setTimeout(r, 0));

    // Fold is still collapsed; the box carries the summary text from
    // the fetch handler.
    const markerAfter = document.querySelector(".fold-chev") as SVGElement;
    expect(markerAfter.classList.contains("open")).toBe(false);
    expect(document.querySelector(".annot-box")?.textContent).toBe("wraps in try/except");
  });

  test("failure response surfaces the retry copy", async () => {
    bootViewer(dataWithFold());
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
    bootViewer(dataWithFold());
    expandHunk();
    const es = lastEventSource();
    es.dispatch("fold-summary", {
      hunk_id: "H0_0", new_start: 1, new_count: 2, summary: "remote summary",
    });
    // The SSE handler drops the rendered cache and replaces the hunk
    // DOM, so the new fold box's content reflects the streamed value.
    const box = document.querySelector(".annot-box");
    expect(box?.textContent).toBe("remote summary");
  });
});
