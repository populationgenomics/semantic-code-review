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

//: Which call writes each section kind — the server's `PROSE_PASSES`
//: as the viewer sees it. Intuition and Code share one, which is what
//: the queue has to fold.
const PASS_OF: Record<ExplainerSectionKind, string> = {
  map: "skeleton",
  background: "background",
  intuition: "walkthrough",
  code: "walkthrough",
};

function section(overrides: Partial<ExplainerSection> = {}): ExplainerSection {
  const kind = overrides.kind ?? "map";
  return {
    id: "map",
    kind: "map",
    pass_id: PASS_OF[kind],
    title: "Map",
    state: "ready",
    body: "",
    refs: [],
    map_rows: [],
    terms: [],
    skip_box: null,
    sources: [],
    figures: [],
    subsections: [],
    ...overrides,
  };
}

function doc(overrides: Partial<ExplainerDocument> = {}): ExplainerDocument {
  return {
    version: 2,
    turns_used: 0,
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

  test("a document in hand is proof its own skeleton ran", async () => {
    // A run dir is reused for the same head SHA, so a tab that boots
    // mid-pass can hold an earlier run's document. Waiting for the
    // `overview` frame there leaves it holding one the button will not
    // open — and, since the viewer opens into the document, one the
    // button will not leave either.
    boot({ pending: true });
    mockFetch([{ status: 200, body: doc() }]);
    await Explainer.load();
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
    // Loading, the result, then one per section the skeleton left
    // pending as the auto-queue takes them.
    expect(repaints).toBeGreaterThanOrEqual(2);
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
    expect(Explainer.renderPane().querySelector(".explainer-toy-notice")).toBeNull();
    Explainer.onEvent(doc({ toy_data: true }) as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-toy-notice")!.textContent)
      .toContain("illustrative");
  });

  test("a document with nothing to footnote but toy data still gets the notice", () => {
    boot();
    Explainer.onEvent(doc({ toy_data: true, sections: [] }) as SseExplainerEvent);
    const pane = Explainer.renderPane();
    expect(pane.querySelector(".explainer-footnote")).toBeNull();
    expect(pane.querySelector(".explainer-toy-notice")).not.toBeNull();
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

  function countingFetch(seen: string[]): { maxInFlight: () => number } {
    let inFlight = 0;
    let peak = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((async (url: string) => {
      seen.push(url);
      inFlight++;
      peak = Math.max(peak, inFlight);
      await Promise.resolve();
      inFlight--;
      return { status: 200, ok: true, json: () => Promise.resolve(doc()) } as Response;
    }) as typeof fetch);
    return { maxInFlight: () => peak };
  }

  test("two calls opened at once are written one at a time", async () => {
    boot();
    Explainer.onEvent(threeSectionDoc() as SseExplainerEvent);
    const seen: string[] = [];
    const f = countingFetch(seen);
    Explainer.generateSection("background");
    Explainer.generateSection("code");
    await vi.waitFor(() => expect(seen).toHaveLength(2));
    expect(f.maxInFlight()).toBe(1);
  });

  test("both halves of a merged pass are one call, not two", async () => {
    // Intuition and Code share a `pass_id`: pressing each would pay for
    // the same walkthrough twice.
    boot();
    Explainer.onEvent(threeSectionDoc() as SseExplainerEvent);
    const seen: string[] = [];
    countingFetch(seen);
    Explainer.generateSection("intuition");
    Explainer.generateSection("code");
    await vi.waitFor(() => expect(seen).toHaveLength(1));
    await Promise.resolve();
    expect(seen).toEqual([`/explainer/section/intuition`]);
  });

  test("entering the mode queues one call per pass, not one per section", async () => {
    boot();
    const seen: string[] = [];
    countingFetch(seen);
    Explainer.onEvent(threeSectionDoc() as SseExplainerEvent);
    Explainer.generateAllPending();
    await vi.waitFor(() => expect(seen).toHaveLength(2));
    expect(seen).toEqual([
      "/explainer/section/background",
      "/explainer/section/intuition",
    ]);
  });

  test("a section left pending by its own call is not silently re-queued", async () => {
    // The server marks a section its call did not return `failed`, not
    // `pending`, so the auto-queue cannot buy the same call again.
    boot();
    Explainer.onEvent(doc({
      sections: [
        section(),
        section({ id: "intuition", kind: "intuition", title: "Intuition", state: "ready", body: "x" }),
        section({ id: "code", kind: "code", title: "Code", state: "failed" }),
      ],
    }) as SseExplainerEvent);
    const seen: string[] = [];
    countingFetch(seen);
    Explainer.generateAllPending();
    await Promise.resolve();
    expect(seen).toHaveLength(0);
  });

  test("a merged call's failure is shown on both of its sections", async () => {
    boot();
    Explainer.onEvent(threeSectionDoc() as SseExplainerEvent);
    mockFetch([{ status: 500, body: { error: "the pass raised" } }]);
    Explainer.generateSection("code");
    await vi.waitFor(() => {
      const errors = Explainer.renderPane().querySelectorAll(".explainer-error");
      expect(errors).toHaveLength(2);
    });
  });
});

function threeSectionDoc(): ExplainerDocument {
  return doc({
    sections: [
      section(),
      section({ id: "background", kind: "background", title: "Background", state: "pending" }),
      section({ id: "intuition", kind: "intuition", title: "Intuition", state: "pending" }),
      section({ id: "code", kind: "code", title: "Code", state: "pending" }),
    ],
  });
}

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

  test("each term renders as a callout, keeping the dl pairing", () => {
    // A definition is what the `concept` kind was written for, so the two
    // share a visual rather than inventing a second one.
    boot();
    Explainer.onEvent(proseDoc({
      state: "ready",
      body: "Prose.",
      terms: [{ term: "`prompt` field", definition: "The free-text kickoff string." }],
    }) as SseExplainerEvent);
    const box = Explainer.renderPane().querySelector(".explainer-terms .explainer-callout")!;
    expect(box).not.toBeNull();
    // The name is inline markdown, not text: identifiers carry backticks.
    expect(box.querySelector("dt")!.querySelector("code")!.textContent).toBe("prompt");
    expect(box.querySelector("dd")!.textContent).toContain("free-text kickoff");
    expect(box.parentElement!.tagName).toBe("DL");
  });

  test("a section deferred by a busy server stays visibly queued", async () => {
    // The drain loop sets `_writing` before every attempt, so a retry
    // that cleared its phase rendered "Writing…" for the length of the
    // attempt and "Queued…" between them — a 4-second flash, per
    // section. The phase now outranks the flag.
    boot();
    Explainer.onEvent(proseDoc({ state: "pending", body: "" }) as SseExplainerEvent);
    mockFetch([{ status: 409, body: { error: "an explainer pass is already running", retry: true } }]);
    Explainer.generateSection("code");
    await new Promise<void>((r) => setTimeout(r, 0));
    const text = Explainer.renderPane().textContent || "";
    expect(text).toContain("Queued behind another section");
    expect(text).not.toContain("Writing this section");
  });

  test("an arriving document wakes a deferred section", async () => {
    // The finishing pass frees the server's single slot, so the SSE
    // frame is the signal to retry — not a timer.
    boot();
    Explainer.onEvent(proseDoc({ state: "pending", body: "" }) as SseExplainerEvent);
    const calls = mockFetch([
      { status: 409, body: { error: "an explainer pass is already running", retry: true } },
      { status: 200, body: proseDoc({ state: "ready", body: "Written." }) },
    ]);
    Explainer.generateSection("code");
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(calls).toHaveLength(1);

    Explainer.onEvent(proseDoc({ state: "pending", body: "" }) as SseExplainerEvent);
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(calls).toHaveLength(2);
  });

  test("a [!NOTE] blockquote becomes a concept callout, in place", () => {
    // Markdown has no callout of its own; GitHub's alert convention is the
    // spelling a model already knows, and a blockquote keeps the callout
    // where the prose put it rather than in a detached list.
    withBody("Before.\n\n> [!NOTE]\n> A `oneof` is a tagged union.\n\nAfter.");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    const box = body.querySelector(".explainer-callout")!;
    expect(box).not.toBeNull();
    expect(box.querySelector(".explainer-callout-title")!.textContent).toBe("Concept");
    expect(box.querySelector(".explainer-callout-body")!.textContent).toContain("tagged union");
    // The marker itself is consumed, not left in the prose.
    expect(box.textContent).not.toContain("[!NOTE]");
    expect(body.querySelector("blockquote")).toBeNull();
    // And it sits between the paragraphs it was written between.
    const kids = Array.from(body.children).map((e) => e.className || e.tagName);
    expect(kids[1]).toContain("explainer-callout");
  });

  test("the warning and tip kinds carry their own edge colour", () => {
    withBody("> [!WARNING]\n> This drops a column.\n\n> [!TIP]\n> Skippable.");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    expect(body.querySelector(".explainer-callout-edge")!.textContent).toContain("Edge case");
    expect(body.querySelector(".explainer-callout-aside")!.textContent).toContain("Aside");
  });

  test("an unrecognised marker stays an ordinary blockquote", () => {
    // Restyling a kind the prompt never offered would be inventing a
    // meaning the model did not ask for.
    withBody("> [!GOTCHA]\n> Invented by the model.");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    expect(body.querySelector(".explainer-callout")).toBeNull();
    expect(body.querySelector("blockquote")!.textContent).toContain("[!GOTCHA]");
  });

  test("a reference the prose has not named carries the basename", () => {
    // Bare, this sentence reads "The contract lives in ↗." — the arrow is
    // doing the work of a noun and cannot.
    withBody("The contract lives in [F0].");
    const ref = Explainer.renderPane().querySelector(".explainer-arrow") as HTMLElement;
    expect(ref.textContent).toBe("api.proto \u2197");
    expect(ref.title).toContain("schema/api.proto");
    expect(ref.dataset.refId).toBe("F0");
  });

  test("a reference the prose has just named goes bare", () => {
    withBody("The file api.proto [F0] holds the message shapes.");
    const ref = Explainer.renderPane().querySelector(".explainer-arrow") as HTMLElement;
    expect(ref.textContent).toBe("\u2197");
    expect(ref.title).toContain("schema/api.proto");
  });

  test("a name inside a code span still counts as having been said", () => {
    // markdown-it puts it in its own element, so the search has to walk
    // back past the boundary.
    withBody("See `api.proto` [F0] for the shapes.");
    const ref = Explainer.renderPane().querySelector(".explainer-arrow") as HTMLElement;
    expect(ref.textContent).toBe("\u2197");
  });

  test("a hunk reference is labelled by its file, not by its raw id", () => {
    // `_fileLabel` on a hunk id finds nothing and hands back "H1_1",
    // which is not something a reviewer can act on.
    withBody("The rename happens in [H1_1].");
    const ref = Explainer.renderPane().querySelector(".explainer-arrow") as HTMLElement;
    expect(ref.textContent).toBe("api_pb.ts:2 \u2197");
  });

  test("a run of references each keep their label", () => {
    // "Everything under ↗, ↗, ↗ is generated" was the failure: a row of
    // anonymous glyphs naming nothing.
    withBody("Everything under [F0], [F1] is generated.");
    const refs = Array.from(Explainer.renderPane().querySelectorAll(".explainer-arrow"));
    expect(refs.map((r) => r.textContent)).toEqual(["api.proto \u2197", "api_pb.ts \u2197"]);
  });

  test("a hunk arrow names the file and the hunk's place in it, in its tooltip", () => {
    const opened: string[] = [];
    boot({}, { onOpenHunk: (id: string) => opened.push(id) });
    Explainer.onEvent(proseDoc({ state: "ready", body: "See [H1_1] for the rename." }) as SseExplainerEvent);
    const ref = Explainer.renderPane().querySelector(".explainer-arrow") as HTMLElement;
    expect(ref.title).toContain("gen/api_pb.ts:2");
    ref.click();
    expect(opened).toEqual(["H1_1"]);
  });

  test("a reference inside a code span is the snippet's, not an arrow", () => {
    withBody("Grep for `[F0]` in the fixture, and read [F1].");
    const refs = Explainer.renderPane().querySelectorAll(".explainer-arrow");
    expect(refs).toHaveLength(1);
    expect((refs[0] as HTMLElement).title).toContain("gen/api_pb.ts");
  });

  test("surrounding prose survives the arrow swap", () => {
    withBody("Read [F0] first, then [F1].");
    const body = Explainer.renderPane().querySelector(".explainer-body")!;
    expect(body.textContent!.trim())
      .toBe("Read api.proto \u2197 first, then api_pb.ts \u2197.");
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

// --- Figures ---------------------------------------------------------------

describe("figures", () => {
  const FIG = {
    svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
      + '<rect class="d-box" x="0" y="0" width="10" height="10"/></svg>',
    alt: "the request path",
    caption: "",
    stripped: 0,
  };

  test("a section's figures render after its prose", () => {
    boot();
    Explainer.onEvent(proseDoc({ state: "ready", body: "The walkthrough.", figures: [FIG] }) as SseExplainerEvent);
    const fig = Explainer.renderPane().querySelector(".explainer-section .explainer-figure")!;
    expect(fig.querySelector("svg")!.getAttribute("aria-label")).toBe("the request path");
  });

  test("a subsection's figures render too", () => {
    boot();
    Explainer.onEvent(proseDoc({
      state: "ready",
      body: "The walkthrough.",
      subsections: [
        section({ id: "code-1", kind: "code", title: "The contract", body: "one", map_rows: [], figures: [FIG] }),
      ],
    }) as SseExplainerEvent);
    const fig = Explainer.renderPane().querySelector(".explainer-subsection .explainer-figure");
    expect(fig).not.toBeNull();
  });
});

// --- Provenance and Background's affordances -------------------------------

describe("provenance", () => {
  function backgroundDoc(overrides: Partial<ExplainerSection> = {}): ExplainerDocument {
    return doc({
      sections: [
        section({ id: "background", kind: "background", title: "Background", state: "ready", ...overrides }),
        section({ id: "code", kind: "code", title: "Code", state: "pending" }),
      ],
    });
  }

  test("the files the pass read are rendered as a citation line", () => {
    boot();
    Explainer.onEvent(backgroundDoc({
      body: "The RPC layer.",
      sources: ["schema/api.proto", "cmd/list.go"],
    }) as SseExplainerEvent);
    const line = Explainer.renderPane().querySelector(".explainer-sources")!;
    expect(line.textContent).toContain("schema/api.proto");
    expect(line.textContent).toContain("cmd/list.go");
  });

  test("a section that read nothing says so — that is the whole point", () => {
    boot();
    Explainer.onEvent(backgroundDoc({ body: "The RPC layer.", sources: [] }) as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-sources")!.textContent)
      .toBe("Written without reading any file.");
  });

  test("every prose section can carry one — not just Background", () => {
    boot();
    Explainer.onEvent(doc({
      sections: [
        section({ id: "code", kind: "code", title: "Code", state: "ready", body: "x", sources: ["api.py"] }),
      ],
    }) as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-sources")!.textContent)
      .toContain("api.py");
  });

  test("a merged pass cites once, under the last section it wrote", () => {
    // The read list belongs to the call. Repeating the identical
    // sentence under each of a merged pair says nothing the first did.
    boot();
    Explainer.onEvent(doc({
      sections: [
        section({
          id: "intuition", kind: "intuition", title: "Intuition",
          state: "ready", body: "the idea", sources: ["api.py"],
        }),
        section({
          id: "code", kind: "code", title: "Code",
          state: "ready", body: "the walkthrough", sources: ["api.py"],
        }),
      ],
    }) as SseExplainerEvent);
    const pane = Explainer.renderPane();
    expect(pane.querySelectorAll(".explainer-sources")).toHaveLength(1);
    const code = pane.querySelector('[data-section-id="code"]')!;
    expect(code.querySelector(".explainer-sources")).not.toBeNull();
  });

  test("the Map carries no citation line — the skeleton reads nothing", () => {
    boot();
    Explainer.onEvent(doc() as SseExplainerEvent);
    expect(Explainer.renderPane().querySelector(".explainer-sources")).toBeNull();
  });

  test("the skip box jumps to the section it names", () => {
    boot();
    Explainer.onEvent(backgroundDoc({
      body: "Ground.",
      skip_box: { body: "If you know the RPC layer,", target_section_id: "code" },
    }) as SseExplainerEvent);
    const box = Explainer.renderPane().querySelector(".explainer-skip")!;
    expect(box.textContent).toContain("If you know the RPC layer");
    (box.querySelector(".explainer-skip-link") as HTMLElement).click();
    expect(Explainer.activeSectionId()).toBe("code");
  });

  test("terms render as a definition list", () => {
    boot();
    Explainer.onEvent(backgroundDoc({
      body: "Ground.",
      terms: [
        { term: "ListRequest", definition: "the **paged** request" },
        { term: "cursor", definition: "an opaque position token" },
      ],
    }) as SseExplainerEvent);
    const dl = Explainer.renderPane().querySelector(".explainer-terms")!;
    expect(Array.from(dl.querySelectorAll("dt")).map((n) => n.textContent))
      .toEqual(["ListRequest", "cursor"]);
    // Definitions are markdown too, through the same sanitised path.
    expect(dl.querySelector("dd strong")!.textContent).toBe("paged");
  });

  test("the glossary follows the prose it consolidates; the skip box leads", () => {
    // Terms are a reference to come back to. Ahead of the prose they are
    // a wall of definitions leaning on concepts nothing has introduced.
    boot();
    Explainer.onEvent(backgroundDoc({
      body: "Ground.",
      skip_box: { body: "If you know the RPC layer,", target_section_id: "code" },
      figures: [{
        svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
          + '<rect class="d-box" x="0" y="0" width="10" height="10"/></svg>',
        alt: "the request path",
        caption: "",
        stripped: 0,
      }],
      terms: [{ term: "cursor", definition: "an opaque position token" }],
      sources: ["api.py"],
    }) as SseExplainerEvent);
    const section = Explainer.renderPane().querySelector('[data-section-id="background"]')!;
    expect(Array.from(section.children).map((e) => e.className)).toEqual([
      "explainer-section-title",
      "explainer-skip",
      "explainer-body",
      "explainer-figure",
      "explainer-terms",
      "explainer-sources",
    ]);
  });
});
