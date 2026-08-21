// Cross-language lockstep for the symbol-aware fold detector.
//
// The same (rows, spans) cases in tests/fixtures/fold_regions_cases.json
// drive this vitest case and the pytest case in tests/test_hunk_layout.py
// (test_fold_regions_lockstep_fixture). Detection itself runs only here
// now (ADR 0006 slice 6); `hunk_layout.compute_fold_regions` is the
// reference implementation this one is pinned against, so the algorithm
// has an executable specification outside the bundle.

import fs from "node:fs";
import path from "node:path";
import { describe, test, expect, beforeEach, vi } from "vitest";
import { Folds, _computeFoldRegions } from "../../semantic_code_review/viewer/assets/folds";
import { FileRows } from "../../semantic_code_review/viewer/assets/file_rows";
import { FileText } from "../../semantic_code_review/viewer/assets/file_text";
import { Visibility } from "../../semantic_code_review/viewer/assets/visibility";

interface FoldCase {
  name: string;
  rows: RowBlock[];
  head_spans: FoldSymbolSpan[];
  base_spans: FoldSymbolSpan[];
  expected: Array<Record<string, unknown>>;
}

const REGION_KEYS = [
  "header_idx", "body_start_idx", "body_end_idx", "context",
  "right_start", "right_end", "left_start", "left_end",
  "qualified_name", "kind",
] as const;

const CASES: FoldCase[] = JSON.parse(
  fs.readFileSync(
    path.resolve(process.cwd(), "tests/fixtures/fold_regions_cases.json"),
    "utf-8",
  ),
);

describe("fold detector lockstep fixture", () => {
  for (const c of CASES) {
    test(c.name, () => {
      // _computeFoldRegions only reads row line numbers / kind / text, so
      // the DOM-less fixture rows stand in for RowWithEls.
      const detected = _computeFoldRegions(
        c.rows as never[], c.head_spans, c.base_spans,
      );
      const got = detected.map((r) =>
        Object.fromEntries(REGION_KEYS.map((k) => [k, (r as Record<string, unknown>)[k]])),
      );
      expect(got).toEqual(c.expected);
    });
  }
});

// --- Detection is anchored to the file, not to what is rendered ------------
//
// `attachFileFolds` runs against the rows of the current render, which grow
// when the reviewer reveals context outside a hunk. Detection must not: a
// region's identity is the absolute line span its record is matched on, and
// the summary + collapse state hanging off that record are lost the moment
// the span moves (#10).

interface RenderedRow {
  row: RowBlock;
  oldEl: HTMLElement;
  newEl: HTMLElement;
}

// These cases mount the DOM themselves rather than going through the
// renderer, so a chevron click has no render to ask for; the state it
// moves is asserted directly.
const NO_REPAINT = (): void => undefined;

beforeEach(() => {
  Visibility.reset();
  FileText.init("", () => undefined);
  FileText.reset();
});

/** Put a file's source behind the `/file-text` route the detector reads
 *  it through. `sides` picks which sides the route serves — "base" is a
 *  file whose head side it has none of (a deletion, or a head over its
 *  2 MB cap), which is the case `head_lines` structurally could not
 *  cover. */
async function _serve(
  file: FileBlock, lines: string[], sides: "both" | "head" | "base" = "both",
): Promise<void> {
  const text = lines.join("\n");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
    JSON.stringify({
      file_idx: 0, path: file.path,
      base: sides === "head" ? null : text,
      head: sides === "base" ? null : text,
    }),
    { status: 200 },
  ));
  await FileText.load(file);
}

function _rowEl(text: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "row";
  el.appendChild(document.createElement("div"));       // line-number cell
  const content = document.createElement("div");
  content.className = text === "" ? "empty" : "content";
  content.textContent = text;
  el.appendChild(content);
  return el;
}

function _rendered(row: RowBlock): RenderedRow {
  return { row, oldEl: _rowEl(row.old_text), newEl: _rowEl(row.new_text) };
}

/** Mount one `.file` whose body holds the given containers, each a run of
 *  rows registered with `FileRows` exactly as render.ts does. */
function _mountFile(
  fileId: string, containers: Array<{ kind: "hunk" | "gap-expansion"; rows: RowBlock[] }>,
): HTMLElement {
  const fileEl = document.createElement("div");
  fileEl.className = "file";
  fileEl.dataset.id = fileId;
  const body = document.createElement("div");
  body.className = "file-body";
  fileEl.appendChild(body);
  for (const c of containers) {
    const rendered = c.rows.map(_rendered);
    const entry = {
      rows: c.rows,
      oldEls: rendered.map((r) => r.oldEl),
      newEls: rendered.map((r) => r.newEl),
      sourceRows: c.rows,
    };
    const outer = document.createElement("div");
    outer.className = c.kind;
    let source = outer;
    if (c.kind === "hunk") {
      source = document.createElement("div");
      source.className = "diff";
      outer.appendChild(source);
    }
    for (const r of rendered) { source.appendChild(r.oldEl); source.appendChild(r.newEl); }
    FileRows.record(source, entry);
    body.appendChild(outer);
  }
  document.body.appendChild(fileEl);
  return fileEl;
}

// A 12-line file whose single hunk touches lines 6..8 — well inside
// `Foo` / `Foo.bar`, and nowhere near `other`.
const HEAD_LINES = [
  "import os",
  "",
  "class Foo:",
  "    def bar(self):",
  "        a = 1",
  "        b = 2",
  "        c = 3",
  "        d = 4",
  "        return a",
  "",
  "def other():",
  "    return 1",
];

const HUNK_ROWS: RowBlock[] = [
  { kind: "ctx", old_line: 6, new_line: 6, old_text: "        b = 2", new_text: "        b = 2" },
  { kind: "pair", old_line: 7, new_line: 7, old_text: "        c = 2", new_text: "        c = 3" },
  { kind: "ctx", old_line: 8, new_line: 8, old_text: "        d = 4", new_text: "        d = 4" },
];

const SPANS: FoldSymbolSpan[] = [
  { start_line: 3, end_line: 9, kind: "class", qualified_name: "Foo", depth: 0 },
  { start_line: 4, end_line: 9, kind: "function", qualified_name: "Foo.bar", depth: 1 },
  { start_line: 11, end_line: 12, kind: "function", qualified_name: "other", depth: 0 },
];

/** Rows for a run of unchanged head lines, as a gap expansion renders them. */
function _contextRows(from: number, to: number): RowBlock[] {
  const rows: RowBlock[] = [];
  for (let n = from; n <= to; n++) {
    rows.push({
      kind: "ctx", old_line: n, new_line: n,
      old_text: HEAD_LINES[n - 1], new_text: HEAD_LINES[n - 1],
    });
  }
  return rows;
}

function _makeFile(overrides: Partial<FileBlock> = {}): FileBlock {
  return {
    id: "F0", path: "a.py", old_path: null, status: "modified", language: "python",
    adds: 1, dels: 1, summary: "",
    symbols: { added: [], modified: [], removed: [] },
    fold_symbols: { head: SPANS, base: SPANS },
    fold_regions: [],
    hunks: [{
      id: "H0_0", header: "@@ -6,3 +6,3 @@",
      old_start: 6, old_count: 3, new_start: 6, new_count: 3,
      adds: 1, dels: 1, intent: "", smells: [], confidence: null, context: "",
      refs: [], line_notes: [], segments: [], rows: HUNK_ROWS,
    }],
    ...overrides,
  };
}

/** The text of every collapsed-region placeholder in the document. */
function _placeholderTexts(): string[] {
  return Array.from(document.querySelectorAll(".annot-box")).map((e) => e.textContent ?? "");
}

/** Every region record the file carries, as its identity tuple. */
function _addresses(file: FileBlock): string[] {
  return file.fold_regions
    .map((r) => `${r.context}:${r.right_start}-${r.right_end}:${r.left_start}-${r.left_end}`);
}

/** Detect against the hunk alone, then against the hunk plus its
 *  surrounding context revealed — the two states #10's repro alternates
 *  between. */
function _attachUnrevealed(file: FileBlock): void {
  Folds.attachFileFolds(_mountFile(file.id, [{ kind: "hunk", rows: HUNK_ROWS }]), file, [], NO_REPAINT);
}

function _attachRevealed(file: FileBlock): void {
  Folds.attachFileFolds(_mountFile(file.id, [
    { kind: "gap-expansion", rows: _contextRows(1, 5) },
    { kind: "hunk", rows: HUNK_ROWS },
    { kind: "gap-expansion", rows: _contextRows(9, 12) },
  ]), file, [], NO_REPAINT);
}

describe("CodeFold spans are absolute and reveal-invariant", () => {
  test("a region is addressed by its definition's span, not by the visible rows", async () => {
    const file = _makeFile();
    await _serve(file, HEAD_LINES);
    _attachUnrevealed(file);
    // `Foo` (3..9) and `Foo.bar` (4..9) are addressed whole even though
    // only lines 6..8 are on screen. `other` (11..12) has no visible row,
    // so no fold is offered for it.
    expect(_addresses(file)).toEqual(["both:3-9:3-9", "both:4-9:4-9"]);
  });

  test("revealing context leaves the existing addresses — and records — alone", async () => {
    const file = _makeFile();
    await _serve(file, HEAD_LINES);
    _attachUnrevealed(file);
    const records = [...file.fold_regions];
    // Whatever hangs off a record — the fold summary today, the collapse
    // flag once it is record-borne — must survive the reveal.
    records[0].summary = "sets up Foo";

    document.body.innerHTML = "";
    _attachRevealed(file);

    // The regions do not change — detection reads the file, not the
    // screen — and `other`, off screen before, keeps the address it had.
    // What matters is that the two existing ones keep their record
    // objects.
    expect(_addresses(file)).toEqual(["both:3-9:3-9", "both:4-9:4-9", "right:11-12:11-12"]);
    const after = [...file.fold_regions];
    expect(after[0]).toBe(records[0]);
    expect(after[1]).toBe(records[1]);
    expect(after[0].summary).toBe("sets up Foo");
  });

  test("#10's repro — reveal, fold, collapse the file, reopen it — keeps the record", async () => {
    const file = _makeFile();
    await _serve(file, HEAD_LINES);
    // Reveal the context around the hunk, then fold `Foo`.
    _attachRevealed(file);
    const folded = file.fold_regions[0];
    folded.summary = "collapsed by the reviewer";
    const chevrons = Array.from(document.querySelectorAll<SVGElement>(".fold-chev"));
    chevrons[0].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

    // Collapsing a file re-renders it from scratch, which drops the
    // revealed context along with the whole body; reopening it renders
    // the hunk alone again.
    document.body.innerHTML = "";
    _attachUnrevealed(file);

    expect(file.fold_regions[0]).toBe(folded);
    expect(file.fold_regions[0].summary).toBe("collapsed by the reviewer");
  });

  test("no content: no fold is offered rather than one that moves", () => {
    // The route serves neither side of a binary file, and neither side of
    // one over its 2 MB cap; nothing has answered yet at first paint. A
    // region detected off the rendered rows instead would be addressed
    // differently the moment the reviewer revealed context — #10 — so
    // none is offered at all.
    const file = _makeFile({ fold_symbols: { head: [], base: [] } });
    Folds.attachFileFolds(
      _mountFile(file.id, [{ kind: "hunk", rows: _contextRows(3, 9) }]), file,
      [], NO_REPAINT,
    );
    expect(_addresses(file)).toEqual([]);
    expect(document.querySelectorAll(".fold-chev")).toHaveLength(0);
  });

  test("a hide already in place is honoured before any content arrives", () => {
    // The ordering claim behind making detection asynchronous (ADR 0006):
    // a persisted `HiddenSpan` describes itself, so restoring one needs no
    // detection — only *offering* a new fold does. The renderer drops the
    // rows the span covers (visibility.ts's `planRows`); this is the
    // detector's half — it neither trips over the span nor re-derives it.
    const file = _makeFile();
    const address = {
      context: "both" as FoldContext,
      right_start: 3, right_end: 9, left_start: 3, left_end: 9,
    };
    Visibility.hide(Visibility.codeFoldSpan(file.id, address, "user"));

    Folds.attachFileFolds(
      _mountFile(file.id, [{ kind: "hunk", rows: HUNK_ROWS }]), file, [], NO_REPAINT,
    );

    expect(Visibility.isHidden(file.id, Visibility.codeFoldSpanId(file.id, address)))
      .toBe(true);
    expect(_addresses(file)).toEqual([]);
  });

  test("the base side alone carries detection when head is unserved", async () => {
    // The head-side-only limit `head_lines` imposed: a file the route
    // serves no head side for — one whose head is over the 2 MB cap, or
    // absent — had no content stream at all and fell back to the rendered
    // rows. Unchanged context is the same text on both sides, so the base
    // side answers the same question: `other` (11..12) is detected here
    // only because the *file* says it is there.
    const file = _makeFile();
    await _serve(file, HEAD_LINES, "base");
    _attachRevealed(file);

    expect(_addresses(file)).toEqual([
      "both:3-9:3-9", "both:4-9:4-9", "right:11-12:11-12",
    ]);
  });
});

describe("indentation folds are detected from file content, not the rendered rows", () => {
  // No definition spans (unsupported language, or no worktree), so every
  // region is indentation-derived — the half that genuinely needs file
  // content. A hunk truncates `def bar`'s body at line 8; the file runs
  // it to line 9. Detecting over the rendered rows would address the
  // region 4..8 unrevealed and 4..9 revealed, orphaning its record.
  const rows: RowBlock[] = [
    { kind: "ctx", old_line: 4, new_line: 4, old_text: HEAD_LINES[3], new_text: HEAD_LINES[3] },
    { kind: "ctx", old_line: 5, new_line: 5, old_text: HEAD_LINES[4], new_text: HEAD_LINES[4] },
    { kind: "ctx", old_line: 6, new_line: 6, old_text: HEAD_LINES[5], new_text: HEAD_LINES[5] },
    { kind: "pair", old_line: 7, new_line: 7, old_text: "        c = 2", new_text: "        c = 3" },
    { kind: "ctx", old_line: 8, new_line: 8, old_text: HEAD_LINES[7], new_text: HEAD_LINES[7] },
  ];

  function _file(): FileBlock {
    const f = _makeFile({ fold_symbols: { head: [], base: [] } });
    f.hunks[0].old_start = 4; f.hunks[0].old_count = 5;
    f.hunks[0].new_start = 4; f.hunks[0].new_count = 5;
    f.hunks[0].rows = rows;
    return f;
  }

  test("the address covers the whole block even where the hunk stops short", async () => {
    const file = _file();
    await _serve(file, HEAD_LINES);
    Folds.attachFileFolds(_mountFile(file.id, [{ kind: "hunk", rows }]), file, [], NO_REPAINT);
    // Both blocks run to line 10 — the indent detector closes a block on
    // the row before the next line at or below its own indent, and line 10
    // is the blank ahead of `def other():`.
    expect(_addresses(file)).toEqual(["both:3-10:3-10", "both:4-10:4-10"]);
  });

  test("revealing context around the hunk does not move it", async () => {
    const file = _file();
    await _serve(file, HEAD_LINES);
    Folds.attachFileFolds(_mountFile(file.id, [{ kind: "hunk", rows }]), file, [], NO_REPAINT);
    const before = [...file.fold_regions];
    before[before.length - 1].summary = "bar's body";

    document.body.innerHTML = "";
    Folds.attachFileFolds(_mountFile(file.id, [
      { kind: "gap-expansion", rows: _contextRows(1, 3) },
      { kind: "hunk", rows },
      { kind: "gap-expansion", rows: _contextRows(9, 12) },
    ]), file, [], NO_REPAINT);

    // The record is re-matched rather than orphaned, so its summary is
    // still what the collapsed placeholder shows. A row-derived address
    // would have moved from 4..8 to 4..10 and left this behind.
    const after = [...file.fold_regions];
    expect(after[before.length - 1]).toBe(before[before.length - 1]);
    expect(_placeholderTexts()).toContain("bar's body");
  });
});
