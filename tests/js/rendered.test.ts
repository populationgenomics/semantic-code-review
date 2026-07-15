// Rendered-mode orchestration: markdown detection, the lazy /file-text
// fetch + cache, the toggle's flip-then-repaint contract, and the
// head-only body render (ADR 0004 slice 1).

import { describe, test, expect, vi, beforeEach } from "vitest";
import {
  Rendered, _plan, _outline, _reveal, _sectionOpen, _diffLines, _classify, _align,
  type BlockPair, type PlanItem,
} from "../../semantic_code_review/viewer/assets/rendered";

function file(id: string, path: string): FileBlock {
  return { id, path } as FileBlock;
}

function mockFileText(body: { base: string | null; head: string | null }): void {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ file_idx: 0, path: "x.md", ...body }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

beforeEach(() => {
  Rendered.init("");
});

describe("Rendered.isMarkdown", () => {
  test("matches .md / .markdown, case-insensitive", () => {
    expect(Rendered.isMarkdown(file("F0", "docs/x.md"))).toBe(true);
    expect(Rendered.isMarkdown(file("F0", "x.markdown"))).toBe(true);
    expect(Rendered.isMarkdown(file("F0", "README.MD"))).toBe(true);
  });

  test("rejects non-markdown paths", () => {
    expect(Rendered.isMarkdown(file("F0", "a.py"))).toBe(false);
    expect(Rendered.isMarkdown(file("F0", "notes.txt"))).toBe(false);
  });
});

describe("Rendered.toggle", () => {
  test("enabling fetches, caches, flips on, and repaints", async () => {
    mockFileText({ base: null, head: "# Hi" });
    const f = file("F1", "a.md");
    const rerender = vi.fn();

    await Rendered.toggle(f, rerender);

    expect(globalThis.fetch).toHaveBeenCalledWith("/file-text?file_idx=1", { cache: "no-store" });
    expect(Rendered.isOn("F1")).toBe(true);
    expect(rerender).toHaveBeenCalledTimes(1);
  });

  test("disabling flips off without a second fetch", async () => {
    mockFileText({ base: null, head: "# Hi" });
    const f = file("F2", "a.md");
    const rerender = vi.fn();

    await Rendered.toggle(f, rerender);       // on (1 fetch)
    await Rendered.toggle(f, rerender);       // off (no fetch)

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(Rendered.isOn("F2")).toBe(false);
    expect(rerender).toHaveBeenCalledTimes(2);
  });

  test("a failed fetch stays in text mode and does not repaint", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("nope", { status: 500 }));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = file("F3", "a.md");
    const rerender = vi.fn();

    await Rendered.toggle(f, rerender);

    expect(Rendered.isOn("F3")).toBe(false);
    expect(rerender).not.toHaveBeenCalled();
  });
});

/** A markdown FileBlock whose hunk rows drive block classification.
 *  `rows` default to none (everything unchanged). */
function mdFile(id: string, rows: Array<Partial<RowBlock>> = []): FileBlock {
  return {
    id, path: "a.md",
    hunks: [{ id: `${id.replace("F", "H")}_0`, rows } as HunkBlock],
  } as FileBlock;
}

async function flip(f: FileBlock, body: { base: string | null; head: string | null }): Promise<void> {
  mockFileText(body);
  await Rendered.toggle(f, () => {});
}

describe("Rendered.renderBody — two-pane", () => {
  test("renders base left and head right into the block grid", async () => {
    const f = mdFile("F4");
    await flip(f, { base: "# Old", head: "# New\n\nbody text" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    const grid = body.querySelector(".rmd-grid");
    expect(grid).not.toBeNull();
    const oldCol = grid!.querySelector(".rmd-col-old .rmd-block");
    const newCols = grid!.querySelectorAll(".rmd-col-new .rmd-block");
    expect(oldCol!.innerHTML).toContain("<h1>Old</h1>");
    // Head parses to two top-level blocks (heading + paragraph).
    expect(Array.from(newCols).map((c) => c.innerHTML).join("")).toContain("<h1>New</h1>");
    expect(Array.from(newCols).map((c) => c.innerHTML).join("")).toContain("body text");
  });

  test("tints changed blocks: red base / green head, unchanged neutral", async () => {
    // Line 1 replaced (pair), line 3 unchanged context.
    const f = mdFile("F5", [
      { kind: "pair", old_line: 1, new_line: 1, old_text: "# Old", new_text: "# New" },
    ]);
    await flip(f, { base: "# Old\n\nkeep", head: "# New\n\nkeep" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    const removed = body.querySelectorAll(".rmd-col-old .rmd-removed");
    const added = body.querySelectorAll(".rmd-col-new .rmd-added");
    expect(removed).toHaveLength(1);
    expect(added).toHaveLength(1);
    expect(removed[0].querySelector("h1")?.textContent).toBe("Old");
    expect(added[0].querySelector("h1")?.textContent).toBe("New");
    // The unchanged "keep" paragraph carries no tint on either side.
    expect(body.querySelectorAll(".rmd-block").length).toBe(4);
    expect(body.querySelectorAll(".rmd-removed, .rmd-added").length).toBe(2);
  });

  test("a rewritten paragraph reads all-red-left / all-green-right, aligned", async () => {
    // An unchanged heading anchors; the paragraph below is replaced.
    const f = mdFile("F6", [
      { kind: "pair", old_line: 3, new_line: 3, old_text: "old body", new_text: "new body" },
    ]);
    await flip(f, { base: "# H\n\nold body", head: "# H\n\nnew body" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    // Two block-pairs → four columns: (H,H) then (old body, new body).
    const cols = body.querySelectorAll(".rmd-grid > .rmd-col");
    expect(cols).toHaveLength(4);
    // The heading pair is unchanged on both sides; the paragraph pair is
    // red-left beside green-right in the same grid row.
    expect(cols[2].querySelector(".rmd-removed")!.textContent).toContain("old body");
    expect(cols[3].querySelector(".rmd-added")!.textContent).toContain("new body");
  });

  test("added file (no base): head all-green beside empty left pads", async () => {
    const f = mdFile("F7", [
      { kind: "ins", old_line: null, new_line: 1, old_text: "", new_text: "# A" },
      { kind: "ins", old_line: null, new_line: 3, old_text: "", new_text: "body" },
    ]);
    await flip(f, { base: null, head: "# A\n\nbody" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    expect(body.querySelectorAll(".rmd-col-old.rmd-col-empty")).toHaveLength(2);
    expect(body.querySelectorAll(".rmd-col-new .rmd-added")).toHaveLength(2);
  });

  test("marks changed words within a replaced block: del left / ins right", async () => {
    const f = mdFile("F12", [
      { kind: "pair", old_line: 1, new_line: 1, old_text: "the quick brown fox", new_text: "the slow brown fox" },
    ]);
    await flip(f, { base: "the quick brown fox", head: "the slow brown fox" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    const oldMarks = body.querySelectorAll(".rmd-col-old .char-chg");
    const newMarks = body.querySelectorAll(".rmd-col-new .char-chg");
    expect(Array.from(oldMarks).map((m) => m.textContent).join("")).toBe("quick");
    expect(Array.from(newMarks).map((m) => m.textContent).join("")).toBe("slow");
  });

  test("sub-diff marks cross inline elements (bold), painting only the word", async () => {
    const f = mdFile("F13", [
      { kind: "pair", old_line: 1, new_line: 1, old_text: "a **quick** fox", new_text: "a **slow** fox" },
    ]);
    await flip(f, { base: "a **quick** fox", head: "a **slow** fox" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    // The changed word sits inside <strong>; the mark lands within it,
    // leaving the surrounding text unmarked.
    const oldMark = body.querySelector(".rmd-col-old .char-chg");
    expect(oldMark?.textContent).toBe("quick");
    expect(oldMark?.closest("strong")).not.toBeNull();
    const newMark = body.querySelector(".rmd-col-new .char-chg");
    expect(newMark?.textContent).toBe("slow");
  });

  test("no sub-diff marks on an unchanged or one-sided block", async () => {
    // Heading unchanged (anchor), paragraph purely inserted on the head.
    const f = mdFile("F14", [
      { kind: "ins", old_line: null, new_line: 3, old_text: "", new_text: "added para" },
    ]);
    await flip(f, { base: "# H", head: "# H\n\nadded para" });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    // The inserted paragraph is wholly green (block tint), no intra marks.
    expect(body.querySelectorAll(".char-chg")).toHaveLength(0);
    expect(body.querySelectorAll(".rmd-col-new .rmd-added")).toHaveLength(1);
  });

  test("shows a notice when both sides are null", async () => {
    const f = mdFile("F8");
    await flip(f, { base: null, head: null });

    const body = document.createElement("div");
    Rendered.renderBody(body, f);

    expect(body.querySelector(".rmd-grid")).toBeNull();
    expect(body.querySelector(".rendered-md-notice")).not.toBeNull();
  });
});

// --- Diff-driven block alignment ----------------------------------------

describe("Rendered._diffLines / _classify / _align", () => {
  function rowsFile(rows: Array<Partial<RowBlock>>): FileBlock {
    return { id: "F0", path: "a.md", hunks: [{ id: "H0", rows } as HunkBlock] } as FileBlock;
  }
  function rb(line: number, end: number = line): RenderedBlock {
    return { html: `<p>${line}</p>`, startLine: line, endLine: end, heading: null, listItem: null };
  }

  test("_diffLines separates aligned (pair) lines from one-sided (ins/del)", () => {
    const d = _diffLines(rowsFile([
      { kind: "ctx", old_line: 1, new_line: 1 },
      { kind: "del", old_line: 2, new_line: null },
      { kind: "pair", old_line: 3, new_line: 2 },
      { kind: "ins", old_line: null, new_line: 3 },
    ]));
    expect([...d.baseChanged].sort()).toEqual([2, 3]);
    expect([...d.baseDeleted]).toEqual([2]); // del only, not the pair
    expect([...d.headChanged].sort()).toEqual([2, 3]);
    expect([...d.headInserted]).toEqual([3]); // ins only, not the pair
  });

  test("_classify: an all-deleted block is unmatched, a replaced block matched", () => {
    // line 2 deleted, line 3 replaced (pair).
    const [deleted, replaced] = _classify([rb(2), rb(3)], new Set([2, 3]), new Set([2]));
    expect(deleted).toMatchObject({ changed: true, matched: false });
    expect(replaced).toMatchObject({ changed: true, matched: true });
  });

  test("a deleted block sits alone; the modified block aligns to its replacement", () => {
    // base paras A[1] B[3](deleted) C[5](replaced); head A[1] C[3](replacement).
    // The old positional zip mis-paired B with C's replacement; projecting
    // the diff keeps B on its own row and lines C up with C'.
    const d = _diffLines(rowsFile([
      { kind: "ctx", old_line: 1, new_line: 1 },
      { kind: "ctx", old_line: 2, new_line: 2 },
      { kind: "del", old_line: 3, new_line: null },
      { kind: "del", old_line: 4, new_line: null },
      { kind: "pair", old_line: 5, new_line: 3 },
    ]));
    const base = _classify([rb(1), rb(3), rb(5)], d.baseChanged, d.baseDeleted);
    const head = _classify([rb(1), rb(3)], d.headChanged, d.headInserted);
    const pairs = _align(base, head);
    expect(pairs.map((p) => [p.base?.startLine ?? null, p.head?.startLine ?? null])).toEqual([
      [1, 1],
      [3, null],
      [5, 3],
    ]);
  });

  test("an inserted block sits alone; surrounding blocks stay aligned", () => {
    // base A[1] B[3]; head A[1] X[3](inserted) B[5].
    const d = _diffLines(rowsFile([
      { kind: "ctx", old_line: 1, new_line: 1 },
      { kind: "ins", old_line: null, new_line: 3 },
      { kind: "ctx", old_line: 3, new_line: 5 },
    ]));
    const base = _classify([rb(1), rb(3)], d.baseChanged, d.baseDeleted);
    const head = _classify([rb(1), rb(3), rb(5)], d.headChanged, d.headInserted);
    const pairs = _align(base, head);
    expect(pairs.map((p) => [p.base?.startLine ?? null, p.head?.startLine ?? null])).toEqual([
      [1, 1],
      [null, 3],
      [3, 5],
    ]);
  });
});

// --- Fold planning (slice 3) --------------------------------------------

/** A block-pair whose sides carry only the fields the planner reads:
 *  changed status, the source line the reveal key derives from, and an
 *  optional heading landmark. `undefined` on a side means an alignment
 *  pad (null block). */
function p(
  line: number,
  opts: { changed?: boolean; heading?: number } = {},
): BlockPair {
  const b = {
    html: `<p>L${line}</p>`, startLine: line, endLine: line,
    changed: !!opts.changed,
    heading: opts.heading ? { level: opts.heading, text: `H${line}` } : null,
  };
  return { base: b, head: b } as BlockPair;
}

function folds(plan: PlanItem[]): Extract<PlanItem, { kind: "fold" }>[] {
  return plan.filter((i): i is Extract<PlanItem, { kind: "fold" }> => i.kind === "fold");
}

describe("Rendered._plan — run folding", () => {
  beforeEach(() => {
    for (const k of Object.keys(_reveal)) delete _reveal[k];
    for (const k of Object.keys(_sectionOpen)) delete _sectionOpen[k];
  });

  test("collapses a long unchanged run, bleeding one block toward a change", () => {
    // heading landmark, 6 unchanged body blocks, then a change.
    const pairs = [
      p(1, { heading: 1 }),
      p(3), p(4), p(5), p(6), p(7), p(8),
      p(10, { changed: true }),
    ];
    const plan = _plan(pairs, "runs", "F0");
    const fs = folds(plan);
    expect(fs).toHaveLength(1);
    expect(fs[0].scope).toBe("run");
    // 6 unchanged − 1 bled toward the trailing change = 5 collapsed.
    expect(fs[0].count).toBe(5);
    // Heading + bled block + the change stay as visible pairs.
    const pairsKept = plan.filter((i) => i.kind === "pair");
    expect(pairsKept).toHaveLength(3);
  });

  test("leaves a short gap (min-run threshold) fully visible", () => {
    const pairs = [
      p(1, { changed: true }),
      p(2), p(3), p(4),        // 2 collapsible after bleeding both ends
      p(5, { changed: true }),
    ];
    const plan = _plan(pairs, "runs", "F0");
    expect(folds(plan)).toHaveLength(0);
    expect(plan.every((i) => i.kind === "pair")).toBe(true);
  });

  test("an unchanged heading breaks a run and stays visible as a landmark", () => {
    // Two 4-block unchanged runs split by a heading; without the break
    // they would be one 8-block run.
    const pairs = [
      p(1), p(2), p(3), p(4),
      p(5, { heading: 2 }),
      p(6), p(7), p(8), p(9),
    ];
    const plan = _plan(pairs, "runs", "F0");
    expect(folds(plan)).toHaveLength(2);
    // The heading pair survives between the two chips.
    const headingKept = plan.some(
      (i) => i.kind === "pair" && i.pair.head?.heading?.level === 2,
    );
    expect(headingKept).toBe(true);
  });

  test("open level collapses nothing", () => {
    const pairs = [p(1), p(2), p(3), p(4), p(5), p(6)];
    const plan = _plan(pairs, "open", "F0");
    expect(folds(plan)).toHaveLength(0);
    expect(plan).toHaveLength(6);
  });

  test("sections level collapses a whole unchanged section, heading included", () => {
    const pairs = [
      p(1, { heading: 1 }), p(2), p(3),           // unchanged section
      p(5, { heading: 1 }), p(6, { changed: true }), // changed section
    ];
    const plan = _plan(pairs, "sections", "F0");
    const fs = folds(plan);
    expect(fs).toHaveLength(1);
    expect(fs[0].scope).toBe("section");
    expect(fs[0].count).toBe(3);   // heading + 2 body blocks
    // The changed section keeps its heading and changed block visible.
    expect(plan.filter((i) => i.kind === "pair")).toHaveLength(2);
  });

  test("reveal state peels blocks off a run's end and shrinks the chip", () => {
    const pairs = [
      p(1, { heading: 1 }),
      p(3), p(4), p(5), p(6), p(7), p(8),
      p(10, { changed: true }),
    ];
    // The run keys off its topmost collapsible block (line 3, head side).
    _reveal["F0"] = { h3: { top: 2, bottom: 0 } };
    const plan = _plan(pairs, "runs", "F0");
    const fs = folds(plan);
    expect(fs).toHaveLength(1);
    expect(fs[0].count).toBe(3);   // 5 − 2 revealed from the top
  });
});

describe("Rendered._outline", () => {
  test("one entry per heading section, badged changed/unchanged", () => {
    const pairs = [
      p(1, { heading: 1 }), p(2),               // unchanged section
      p(3, { heading: 2, changed: true }),      // changed section (heading itself)
      p(4), p(5, { changed: true }),            // changed body
    ];
    const entries = _outline(pairs);
    expect(entries.map((e) => [e.level, e.changed])).toEqual([
      [1, false],
      [2, true],
    ]);
    expect(entries[0].text).toBe("H1");
  });

  test("no headings → empty outline", () => {
    expect(_outline([p(1), p(2)])).toHaveLength(0);
  });
});

describe("Rendered.renderBody — fold chip DOM", () => {
  beforeEach(() => {
    for (const k of Object.keys(_reveal)) delete _reveal[k];
    for (const k of Object.keys(_sectionOpen)) delete _sectionOpen[k];
  });

  test("an unchanged run renders one chip with a chevron at each end; a click reveals into it", async () => {
    // Heading landmark + 6 identical paragraphs, no diff → one long run.
    const doc = "# Head\n\n" + [1, 2, 3, 4, 5, 6].map((n) => `para ${n}`).join("\n\n");
    const f = mdFile("F9");
    await flip(f, { base: doc, head: doc });

    const body = document.createElement("div");
    // Repaint into the same element so the chevron click re-renders.
    Rendered.init("", () => { body.innerHTML = ""; Rendered.renderBody(body, f); });
    Rendered.renderBody(body, f);

    const chip = body.querySelector(".rmd-fold");
    expect(chip).not.toBeNull();
    expect(chip!.querySelectorAll(".rmd-fold-chev")).toHaveLength(2);
    // 6 unchanged body blocks, no change to bleed against → all 6 collapse.
    expect(body.querySelector(".rmd-fold-label")!.textContent).toContain("6 unchanged blocks");
    // The heading landmark stays rendered beside the chip.
    expect(body.querySelector(".rmd-block")!.innerHTML).toContain("<h1>Head</h1>");

    (body.querySelector(".rmd-fold-chev-top") as HTMLElement).click();
    expect(body.querySelector(".rmd-fold-label")!.textContent).toContain("5 unchanged blocks");
  });

  test("in-body ladder switches fold level; Open reveals the whole run", async () => {
    const doc = "# Head\n\n" + [1, 2, 3, 4, 5, 6].map((n) => `para ${n}`).join("\n\n");
    const f = mdFile("F10");
    await flip(f, { base: doc, head: doc });

    const body = document.createElement("div");
    Rendered.init("", () => { body.innerHTML = ""; Rendered.renderBody(body, f); });
    Rendered.renderBody(body, f);

    const ladder = body.querySelector(".rmd-ladder");
    expect(ladder!.querySelectorAll(".rmd-ladder-btn")).toHaveLength(3);
    // Runs is the default active level, and the run is collapsed.
    expect(body.querySelector(".rmd-ladder-btn.active")!.textContent).toBe("Runs");
    expect(body.querySelector(".rmd-fold")).not.toBeNull();

    const openBtn = Array.from(body.querySelectorAll<HTMLElement>(".rmd-ladder-btn"))
      .find((b) => b.textContent === "Open")!;
    openBtn.click();
    expect(body.querySelector(".rmd-fold")).toBeNull();
    expect(body.querySelector(".rmd-ladder-btn.active")!.textContent).toBe("Open");
  });

  test("outline entry expands its whole section", async () => {
    const doc = "# Head\n\n" + [1, 2, 3, 4, 5, 6].map((n) => `para ${n}`).join("\n\n");
    const f = mdFile("F11");
    await flip(f, { base: doc, head: doc });

    const body = document.createElement("div");
    Rendered.init("", () => { body.innerHTML = ""; Rendered.renderBody(body, f); });
    Rendered.renderBody(body, f);

    // One outline entry for the single heading; the run is collapsed.
    const entry = body.querySelector(".rmd-outline-entry") as HTMLElement;
    expect(entry.textContent).toContain("Head");
    expect(body.querySelector(".rmd-fold")).not.toBeNull();

    entry.click();
    expect(body.querySelector(".rmd-fold")).toBeNull();
  });
});
