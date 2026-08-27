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

// --- Prose sections --------------------------------------------------------

function proseDoc(overrides: Partial<ExplainerSection> = {}): ExplainerDocument {
  return doc({
    sections: [
      section({
        map_rows: [
          { ref: { kind: "file", id: "F0" }, why: "the contract" },
          { ref: { kind: "file", id: "F1" }, why: "regenerated" },
        ],
      }),
      section({ id: "code", kind: "code", title: "Code", state: "pending", ...overrides }),
    ],
  });
}

describe("writing a section", () => {
  test("nothing is written on load — the pane stacks every section", async () => {
    boot();
    const calls = mockFetch([{ status: 200, body: proseDoc() }]);
    await Explainer.load();
    Explainer.renderPane();
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/explainer");
  });

  test("opening a pending section POSTs its route", async () => {
    boot();
    Explainer.onEvent(proseDoc() as SseExplainerEvent);
    const written = proseDoc({ state: "ready", body: "The proto is the contract." });
    const calls = mockFetch([{ status: 200, body: written }]);
    Explainer.setActiveSection("code");
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toBe("/explainer/section/code");
    expect(calls[0].init?.method).toBe("POST");
    await vi.waitFor(() =>
      expect(Explainer.renderPane().querySelector(".explainer-body")!.textContent)
        .toContain("The proto is the contract"),
    );
  });

  test("opening a section that is already written spends nothing", async () => {
    boot();
    Explainer.onEvent(proseDoc({ state: "ready", body: "written" }) as SseExplainerEvent);
    const calls = mockFetch([]);
    Explainer.setActiveSection("code");
    Explainer.generateSection("code");
    expect(calls).toHaveLength(0);
  });

  test("the pending section offers a button and shows progress while it runs", async () => {
    boot();
    Explainer.onEvent(proseDoc() as SseExplainerEvent);
    const pane = Explainer.renderPane();
    const btn = pane.querySelector('[data-section-id="code"] .explainer-generate') as HTMLElement;
    expect(btn.textContent).toBe("Write this section");

    let release: (v: unknown) => void = () => {};
    const pending = new Promise((r) => { release = r; });
    vi.spyOn(globalThis, "fetch").mockImplementation((() =>
      pending.then(() => ({ status: 200, ok: true, json: () => Promise.resolve(proseDoc({ state: "ready", body: "x" })) }))
    ) as unknown as typeof fetch);
    btn.click();
    await vi.waitFor(() =>
      expect(Explainer.renderPane().textContent).toContain("Writing this section"),
    );
    release(null);
  });

  test("a section whose hunks aren't annotated says so and offers to wait", async () => {
    boot();
    Explainer.onEvent(proseDoc() as SseExplainerEvent);
    mockFetch([{ status: 409, body: { error: "not ready", annotated: 12, total: 31 } }]);
    Explainer.generateSection("code");
    await vi.waitFor(() => {
      const text = Explainer.renderPane().textContent!;
      expect(text).toContain("12 of 31 hunks");
      expect(text).toContain("narrate over the gaps");
    });
    // Still pending, and the offer to wait is a retry rather than an error.
    expect(Explainer.renderPane().querySelector(".explainer-generate")!.textContent).toBe("Check again");
    expect(Explainer.renderPane().querySelector(".explainer-error")).toBeNull();
  });

  test("a failed section is retryable and leaves its neighbours alone", async () => {
    boot();
    Explainer.onEvent(proseDoc() as SseExplainerEvent);
    mockFetch([{ status: 500, body: { error: "RuntimeError: model said no" } }]);
    Explainer.generateSection("code");
    await vi.waitFor(() =>
      expect(Explainer.renderPane().querySelector(".explainer-error")!.textContent)
        .toContain("model said no"),
    );
    expect(Explainer.renderPane().querySelector(".explainer-generate")!.textContent).toBe("Try again");
    // The Map is untouched: one section's failure must not poison the document.
    expect(Explainer.renderPane().querySelectorAll(".explainer-map-row")).toHaveLength(2);
  });

  test("two sections opened at once are written one at a time", async () => {
    boot();
    Explainer.onEvent(doc({
      sections: [
        section(),
        section({ id: "intuition", kind: "intuition", title: "Intuition", state: "pending" }),
        section({ id: "code", kind: "code", title: "Code", state: "pending" }),
      ],
    }) as SseExplainerEvent);
    let inFlight = 0;
    let maxInFlight = 0;
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((async (url: string) => {
      seen.push(url);
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await Promise.resolve();
      inFlight--;
      return { status: 200, ok: true, json: () => Promise.resolve(doc()) } as Response;
    }) as typeof fetch);
    Explainer.generateSection("intuition");
    Explainer.generateSection("code");
    await vi.waitFor(() => expect(seen).toHaveLength(2));
    expect(maxInFlight).toBe(1);
  });
});

// --- Markdown and reference chips -----------------------------------------

describe("prose rendering", () => {
  function withBody(body: string): void {
    boot();
    Explainer.onEvent(proseDoc({ state: "ready", body }) as SseExplainerEvent);
  }

  test("the body renders as markdown, not as text", () => {
    withBody("A **contract**, then its consumers.\n\n- one\n- two");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    expect(body.querySelector("strong")!.textContent).toBe("contract");
    expect(body.querySelectorAll("li")).toHaveLength(2);
  });

  test("model-authored HTML never reaches the DOM", () => {
    withBody("before <img src=x onerror=alert(1)> <script>alert(2)</script> after");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    expect(body.querySelector("script")).toBeNull();
    expect(body.querySelector("img")).toBeNull();
    expect(body.textContent).toContain("after");
  });

  test("an inline file reference becomes a chip labelled by path", () => {
    withBody("The contract lives in [F0].");
    const chip = Explainer.renderPane().querySelector(".explainer-chip")!;
    expect(chip.textContent).toBe("schema/api.proto");
    expect((chip as HTMLElement).dataset.refId).toBe("F0");
  });

  test("a hunk chip names the file and the hunk's place in it", () => {
    const opened: string[] = [];
    boot({}, { onOpenHunk: (id: string) => opened.push(id) });
    Explainer.onEvent(proseDoc({ state: "ready", body: "See [H1_1] for the rename." }) as SseExplainerEvent);
    const chip = Explainer.renderPane().querySelector(".explainer-chip") as HTMLElement;
    expect(chip.textContent).toBe("gen/api_pb.ts:2");
    chip.click();
    expect(opened).toEqual(["H1_1"]);
  });

  test("a reference inside a code span is the snippet's, not a chip", () => {
    withBody("Grep for `[F0]` in the fixture, and read [F1].");
    const chips = Explainer.renderPane().querySelectorAll(".explainer-chip");
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe("gen/api_pb.ts");
  });

  test("surrounding prose survives the chip swap", () => {
    withBody("Read [F0] first, then [F1].");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    expect(body.textContent!.trim()).toBe("Read schema/api.proto first, then gen/api_pb.ts.");
  });
});

// --- Subsections -----------------------------------------------------------

describe("subsections", () => {
  test("Code's model-chosen parts render under it with their own headings", () => {
    boot();
    Explainer.onEvent(proseDoc({
      state: "ready",
      body: "The walkthrough.",
      subsections: [
        section({ id: "code-1", kind: "code", title: "The contract", body: "one", map_rows: [] }),
        section({ id: "code-2", kind: "code", title: "Its consumers", body: "two", map_rows: [] }),
      ],
    }) as SseExplainerEvent);
    const titles = Array.from(
      Explainer.renderPane().querySelectorAll(".explainer-subsection-title"),
    ).map((n) => n.textContent);
    expect(titles).toEqual(["The contract", "Its consumers"]);
  });

  test("a subsection can be the persisted selection", () => {
    localStorage.setItem("scr-explainer-section:head5678", "explainer:code-1");
    boot();
    Explainer.onEvent(proseDoc({
      state: "ready",
      subsections: [section({ id: "code-1", kind: "code", title: "The contract", map_rows: [] })],
    }) as SseExplainerEvent);
    expect(Explainer.activeSectionId()).toBe("code-1");
  });

  test("a subsection's references count towards coverage", () => {
    boot();
    Explainer.onEvent(proseDoc({
      state: "ready",
      refs: [{ kind: "file", id: "F0" }],
      subsections: [
        section({ id: "code-1", kind: "code", title: "x", refs: [{ kind: "hunk", id: "H1_0" }], map_rows: [] }),
      ],
    }) as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-footnote")!.textContent)
      .toContain("2 references");
  });
});
