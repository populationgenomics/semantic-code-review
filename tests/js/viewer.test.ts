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

interface BootOptions {
  /** Body the /comments fetch fired by Comments.init should resolve to.
   *  Defaults to an empty array. */
  comments?: unknown[];
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
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    EventSourceStub as unknown as typeof EventSource;
  vi.spyOn(globalThis, "fetch").mockImplementation(((url: string, init?: RequestInit) => {
    fetchCalls.push({ url, init });
    const next = fetchResponses.shift() ?? { status: 200, body: {} };
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

    // Click the Foo.bar pill — only H0_0 stays visible.
    (pills[0] as HTMLElement).click();
    const h0 = document.querySelector('.hunk[data-id="H0_0"]') as HTMLElement;
    const h1 = document.querySelector('.hunk[data-id="H0_1"]') as HTMLElement;
    expect(h0.style.display).not.toBe("none");
    expect(h1.style.display).toBe("none");
    expect((pills[0] as HTMLElement).classList.contains("active")).toBe(true);

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

    const h0 = document.querySelector('.hunk[data-id="H0_0"]') as HTMLElement;
    const h1 = document.querySelector('.hunk[data-id="H0_1"]') as HTMLElement;
    const h2 = document.querySelector('.hunk[data-id="H0_2"]') as HTMLElement;

    // Click the class → both its methods' hunks stay visible, the
    // unrelated H0_2 is hidden.
    classPill.click();
    expect(h0.style.display).not.toBe("none");
    expect(h1.style.display).not.toBe("none");
    expect(h2.style.display).toBe("none");

    // Click the method → only its own hunk remains.
    section.querySelector<HTMLElement>('.group-btn[data-pill-id="SY1"]')!.click();
    expect(h0.style.display).not.toBe("none");
    expect(h1.style.display).toBe("none");
    expect(h2.style.display).toBe("none");

    // The collapse toggle hides the children without filtering.
    const toggle = section.querySelector<HTMLElement>(".group-tree-toggle")!;
    expect(toggle.classList.contains("group-tree-toggle-leaf")).toBe(false);
    toggle.click();
    const childWrap = section.querySelector(".group-tree-children") as HTMLElement;
    expect(childWrap.style.display).toBe("none");
    // Filter unaffected by collapse — still on SY1.
    expect(h1.style.display).toBe("none");
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
  test("submitting a question POSTs /console/ask and renders the answer", async () => {
    await bootViewer(makeData({ pending: false }));

    const input = document.querySelector<HTMLTextAreaElement>(".console-input");
    expect(input).not.toBeNull();

    // The /console/ask POST is the next fetch — capture its body and
    // return a canned answer.
    let postedBody: Record<string, unknown> | null = null;
    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        postedBody = JSON.parse(init!.body as string);
        return Promise.resolve({
          status: 200, ok: true,
          json: () => Promise.resolve({ answer: "pagination threads page/size through list_users" }),
        } as Response);
      }) as typeof fetch);

    input!.value = "why pagination?";
    input!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));

    const ask = fetchCalls.filter((c) => c.url.includes("/console/ask"));
    expect(ask.length).toBe(1);
    expect(postedBody!.question).toBe("why pagination?");

    // The drawer is revealed and shows the question + the answer text.
    const drawer = document.querySelector(".console-drawer");
    expect(drawer?.classList.contains("hidden")).toBe(false);
    expect(document.querySelector(".console-q")?.textContent).toBe("why pagination?");
    const answer = document.querySelector(".console-a");
    expect(answer?.textContent).toBe("pagination threads page/size through list_users");
    expect(answer?.classList.contains("console-pending")).toBe(false);
    // Input cleared, ready for the next turn.
    expect(input!.value).toBe("");
  });

  test("answer renders as plain text — script-laden output is inert", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;

    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        return Promise.resolve({
          status: 200, ok: true,
          json: () => Promise.resolve({ answer: "<script>alert(1)</script>" }),
        } as Response);
      }) as typeof fetch);

    input.value = "x";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));

    const answer = document.querySelector(".console-a")!;
    // textContent rendering means no <script> node is created.
    expect(answer.querySelector("script")).toBeNull();
    expect(answer.textContent).toBe("<script>alert(1)</script>");
  });

  test("a failed turn surfaces the error inline", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;

    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        return Promise.resolve({
          status: 409, ok: false,
          json: () => Promise.resolve({ error: "console unavailable" }),
        } as Response);
      }) as typeof fetch);

    input.value = "x";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));

    const answer = document.querySelector(".console-a")!;
    expect(answer.classList.contains("console-error")).toBe(true);
    expect(answer.textContent).toBe("console unavailable");
  });

  test("Esc collapses the drawer, clears the transcript, and resets the server", async () => {
    await bootViewer(makeData({ pending: false }));
    const input = document.querySelector<HTMLTextAreaElement>(".console-input")!;

    (globalThis.fetch as unknown as { mockImplementationOnce: (fn: typeof fetch) => void })
      .mockImplementationOnce(((url: string, init?: RequestInit) => {
        fetchCalls.push({ url, init });
        return Promise.resolve({
          status: 200, ok: true, json: () => Promise.resolve({ answer: "ok" }),
        } as Response);
      }) as typeof fetch);

    input.value = "q";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(document.querySelector(".console-q")).not.toBeNull();

    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(document.querySelector(".console-drawer")?.classList.contains("hidden")).toBe(true);
    expect(document.querySelector(".console-q")).toBeNull();
    expect(fetchCalls.some((c) => c.url.includes("/console/reset"))).toBe(true);
  });
});
