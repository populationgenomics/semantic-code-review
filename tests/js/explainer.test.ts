// Change-explainer document + overview-mode pane (ADR 0007 slice 1).
//
// The module owns the document and the pane; the mode bit lives in
// render.ts and is covered in viewer.test.ts. These cases drive the
// module directly rather than through the bundle, the way
// rendered.test.ts does.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { Explainer } from "../../semantic_code_review/viewer/assets/explainer";

function data(overrides: Partial<ViewerData> = {}): ViewerData {
  return {
    version: "1",
    pr: { head_sha: "head5678" } as PRBlock,
    smells_catalogue: {},
    files: [
      { id: "F0", path: "schema/api.proto" } as FileBlock,
      { id: "F1", path: "gen/api_pb.ts" } as FileBlock,
    ],
    groups: [],
    symbols: [],
    explainer: true,
    ...overrides,
  } as ViewerData;
}

function section(overrides: Partial<ExplainerSection> = {}): ExplainerSection {
  return {
    id: "map",
    kind: "map",
    title: "Map",
    state: "ready",
    body: "",
    refs: [],
    map_rows: [],
    subsections: [],
    ...overrides,
  };
}

function doc(overrides: Partial<ExplainerDocument> = {}): ExplainerDocument {
  return {
    version: 1,
    base_sha: "base1234",
    head_sha: "head5678",
    verdict: "narrate",
    verdict_note: "a cursor threaded from the proto to the client.",
    figure_family: "boxes are services",
    cast: ["ListRequest"],
    toy_data: false,
    dropped_refs: 0,
    sections: [
      section({ id: "background", kind: "background", title: "Background", state: "pending" }),
      section({
        refs: [
          { kind: "file", id: "F0" },
          { kind: "file", id: "F1" },
        ],
        map_rows: [
          { ref: { kind: "file", id: "F0" }, why: "the contract every field below follows from" },
          { ref: { kind: "file", id: "F1" }, why: "regenerated — confirm, do not read" },
        ],
      }),
    ],
    ...overrides,
  };
}

function mockFetch(responses: Array<{ status: number; body: unknown }>): Array<{ url: string; init?: RequestInit }> {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const next = responses.shift() ?? { status: 200, body: {} };
    return Promise.resolve({
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      json: () => Promise.resolve(next.body),
    } as Response);
  }) as typeof fetch);
  return calls;
}

function boot(overrides: Partial<ViewerData> = {}, opts = {}): void {
  const d = data(overrides);
  Explainer.setFilePaths(d);
  Explainer.init("", d, opts);
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

// --- Readiness -------------------------------------------------------------

describe("readiness", () => {
  test("a page that booted after augmentation can press straight away", () => {
    boot({ pending: false });
    expect(Explainer.isReady()).toBe(true);
  });

  test("a page that booted mid-pass waits — the route would 409", () => {
    boot({ pending: true });
    expect(Explainer.isReady()).toBe(false);
    Explainer.markReady();
    expect(Explainer.isReady()).toBe(true);
  });
});

// --- Loading and generating ------------------------------------------------

describe("document acquisition", () => {
  test("404 on load is the press-the-button state, not an error", async () => {
    boot();
    mockFetch([{ status: 404, body: { error: "no explainer document for this diff" } }]);
    await Explainer.load();
    expect(Explainer.hasDocument()).toBe(false);
    expect(Explainer.renderPane().querySelector(".explainer-generate")).not.toBeNull();
  });

  test("an existing document is adopted rather than regenerated", async () => {
    boot();
    const calls = mockFetch([{ status: 200, body: doc() }]);
    await Explainer.load();
    expect(Explainer.hasDocument()).toBe(true);
    await Explainer.generate();
    // Still one call: the pre-fetch. Generating over a document in hand
    // would be a second model call for the same answer.
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/explainer");
  });

  test("generate POSTs the skeleton route and repaints", async () => {
    let repaints = 0;
    boot({}, { onChange: () => { repaints++; } });
    const calls = mockFetch([{ status: 200, body: doc() }]);
    await Explainer.generate();
    expect(calls[0].url).toBe("/explainer/skeleton");
    expect(calls[0].init?.method).toBe("POST");
    expect(Explainer.hasDocument()).toBe(true);
    // Once for the loading state, once for the result.
    expect(repaints).toBe(2);
  });

  test("a failed generation shows the server's reason and offers a retry", async () => {
    boot();
    mockFetch([{ status: 500, body: { error: "ValueError: model said no" } }]);
    await Explainer.generate();
    const pane = Explainer.renderPane();
    expect(pane.querySelector(".explainer-error")!.textContent).toContain("model said no");
    expect(pane.querySelector(".explainer-generate")!.textContent).toBe("Try again");
  });

  test("an SSE frame adopts the document another tab paid for", () => {
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    expect(Explainer.hasDocument()).toBe(true);
    expect(Explainer.sections().map((s) => s.id)).toEqual(["background", "map"]);
  });
});

// --- The Map ---------------------------------------------------------------

describe("the Map", () => {
  test("renders one row per file, in the order given, labelled by path", () => {
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    const rows = Array.from(Explainer.renderPane().querySelectorAll(".explainer-map-row"));
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.querySelector(".explainer-ref")!.textContent))
      .toEqual(["schema/api.proto", "gen/api_pb.ts"]);
    expect(rows[0].querySelector(".explainer-map-why")!.textContent)
      .toBe("the contract every field below follows from");
  });

  test("a row falls back to the raw id when the file isn't in this build", () => {
    boot({ files: [] });
    Explainer.onEvent(doc() as SseExplainerEvent);
    const first = Explainer.renderPane().querySelector(".explainer-ref")!;
    expect(first.textContent).toBe("F0");
  });

  test("clicking a row hands the file id back — it does not fold anything", () => {
    const opened: string[] = [];
    boot({}, { onOpenFile: (id: string) => opened.push(id) });
    Explainer.onEvent(doc() as SseExplainerEvent);
    const btn = Explainer.renderPane().querySelectorAll(".explainer-ref")[1] as HTMLElement;
    btn.click();
    expect(opened).toEqual(["F1"]);
  });

  test("an empty Map says so rather than rendering a bare header", () => {
    boot();
    Explainer.onEvent(doc({ sections: [section()] }) as SseExplainerEvent);
    const pane = Explainer.renderPane();
    expect(pane.querySelector(".explainer-map")).toBeNull();
    expect(pane.textContent).toContain("the files stand on their own");
  });
});

// --- Verdict and footer ----------------------------------------------------

describe("verdict", () => {
  test("not_warranted renders as an answer, not an empty state", () => {
    boot();
    Explainer.onEvent(doc({
      verdict: "not_warranted",
      verdict_note: "Two files, one added field. Read the proto, then confirm the client.",
      sections: [section({ map_rows: [{ ref: { kind: "file", id: "F0" }, why: "the added field" }] })],
    }) as SseExplainerEvent);
    const pane = Explainer.renderPane();
    expect(pane.querySelector(".explainer-lede")!.textContent).toContain("Two files, one added field");
    expect(pane.textContent).toContain("reads faster directly");
    // No pending prose sections offering to generate something nobody needs.
    expect(pane.textContent).not.toContain("Not written yet");
    expect(pane.querySelectorAll(".explainer-map-row")).toHaveLength(1);
  });

  test("a pending prose section says so and shows no body", () => {
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    const bg = Explainer.renderPane().querySelector('[data-section-id="background"]')!;
    expect(bg.textContent).toContain("Not written yet");
    expect(bg.querySelector(".explainer-body")).toBeNull();
  });
});

describe("footer", () => {
  test("dropped references are surfaced, not swallowed", () => {
    boot();
    Explainer.onEvent(doc({ dropped_refs: 2 }) as SseExplainerEvent);
    const footnote = Explainer.renderPane().querySelector(".explainer-footnote")!;
    expect(footnote.textContent).toContain("2 references");
    expect(footnote.textContent).toContain("2 dropped");
  });

  test("the toy-data notice appears only when the document sets it", () => {
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-footnote")!.textContent)
      .not.toContain("illustrative");
    Explainer.onEvent(doc({ toy_data: true }) as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-footnote")!.textContent)
      .toContain("illustrative");
  });
});

// --- Section selection -----------------------------------------------------

describe("section selection", () => {
  test("persists under its own key, leaving the diff-mode pill alone", () => {
    localStorage.setItem("scr-active-group:head5678", "files:BF0");
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    Explainer.setActiveSection("background");
    expect(localStorage.getItem("scr-explainer-section:head5678")).toBe("explainer:background");
    expect(localStorage.getItem("scr-active-group:head5678")).toBe("files:BF0");
  });

  test("a persisted section is restored on the next boot", () => {
    localStorage.setItem("scr-explainer-section:head5678", "explainer:background");
    boot();
    expect(Explainer.activeSectionId()).toBe("background");
    Explainer.onEvent(doc() as SseExplainerEvent);
    expect(Explainer.activeSectionId()).toBe("background");
  });

  test("a persisted section the document doesn't have falls back to the Map", () => {
    localStorage.setItem("scr-explainer-section:head5678", "explainer:nonesuch");
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    expect(Explainer.activeSectionId()).toBe("map");
  });
});
