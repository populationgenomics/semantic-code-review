// The one visibility model, exercised without a rendered document.
//
// Every hide and reveal in the viewer is a `HiddenSpan` moving in or out
// of this store, so the rules the ADR fixes — reveal is reversible,
// nested state survives its container, a bulk action is not a reset — are
// properties of the store rather than of the DOM, and are asserted here.
// The renderer's half (a repaint reproduces the state) is pinned in
// viewer.test.ts, which needs a document by definition.

import { describe, test, expect, beforeEach } from "vitest";
import {
  Visibility, type CollapseLevel,
} from "../../semantic_code_review/viewer/assets/visibility";

function makeHunk(id: string, overrides: Partial<HunkBlock> = {}): HunkBlock {
  return {
    id, header: "@@ -10,4 +10,4 @@",
    old_start: 10, old_count: 4, new_start: 10, new_count: 4,
    adds: 1, dels: 1, intent: "", smells: [], confidence: null, context: "",
    refs: [], line_notes: [], segments: [], rows: [], fold_regions: [],
    ...overrides,
  };
}

function makeFile(id: string, path: string, hunks: HunkBlock[]): FileBlock {
  return {
    id, path, old_path: null, status: "modified", language: "python",
    adds: 1, dels: 1, summary: "",
    symbols: { added: [], modified: [], removed: [] },
    fold_symbols: { head: [], base: [] },
    head_lines: null,
    hunks,
  };
}

function makeData(files: FileBlock[]): ViewerData {
  return {
    version: "1", pr: {} as PRBlock, smells_catalogue: {},
    files, groups: [], symbols: [],
  };
}

const SRC = (): FileBlock => makeFile("F0", "src/a.py", [makeHunk("H0_0")]);
const LOCK = (): FileBlock => makeFile("F1", "uv.lock", [makeHunk("H1_0")]);

function row(newLine: number | null, oldLine: number | null): RowBlock {
  return {
    kind: "ctx", old_line: oldLine, new_line: newLine,
    old_text: "x", new_text: "x",
  };
}

beforeEach(() => {
  Visibility.reset();
});

describe("visibility is the complement of the union", () => {
  test("a line is visible until some span covers it, whatever the kind", () => {
    const f = SRC();
    expect(Visibility.lineHidden(f.id, "new", 11)).toBe(false);

    Visibility.hide(Visibility.hunkSpan(f, f.hunks[0], "user"));
    // The hunk covers 10..13 on both sides.
    expect(Visibility.lineHidden(f.id, "new", 11)).toBe(true);
    expect(Visibility.lineHidden(f.id, "old", 11)).toBe(true);
    expect(Visibility.lineHidden(f.id, "new", 14)).toBe(false);

    // A second, differently-owned span over the same line changes
    // nothing: the union is a union.
    Visibility.hide(Visibility.codeFoldSpan(f.id, {
      context: "right", right_start: 11, right_end: 12,
      left_start: null, left_end: null,
    }, "user"));
    expect(Visibility.covering(f.id, "new", 11)).toHaveLength(2);
    Visibility.reveal(f.id, Visibility.hunkSpanId("H0_0"));
    expect(Visibility.lineHidden(f.id, "new", 11)).toBe(true);
  });

  test("spans are side-tagged, so a right-only fold leaves the base side alone", () => {
    const f = SRC();
    Visibility.hide(Visibility.codeFoldSpan(f.id, {
      context: "right", right_start: 3, right_end: 9,
      left_start: null, left_end: null,
    }, "user"));
    expect(Visibility.lineHidden(f.id, "new", 5)).toBe(true);
    expect(Visibility.lineHidden(f.id, "old", 5)).toBe(false);
  });

  test("spans are per file", () => {
    const src = SRC();
    const lock = LOCK();
    Visibility.hide(Visibility.fileSpan(lock, "user"));
    expect(Visibility.lineHidden(lock.id, "new", 1)).toBe(true);
    expect(Visibility.lineHidden(src.id, "new", 1)).toBe(false);
  });

  test("a comment's line reports which span is hiding it", () => {
    // The shape slice 5's manifest is built on: it needs to know not just
    // that a note is out of sight but which hide it belongs under.
    const f = SRC();
    Visibility.hide(Visibility.hunkSpan(f, f.hunks[0], "user"));
    const hiding = Visibility.covering(f.id, "new", 12);
    expect(hiding.map((s) => [s.kind, s.id])).toEqual([["hunk", "hunk:H0_0"]]);
  });
});

describe("reveal is the removal of a record", () => {
  test("a reveal survives the renderer re-seeding the same span", () => {
    const f = SRC();
    const gap = Visibility.contextSpan(f.id, { start: 1, end: 9 }, { start: 1, end: 9 });
    Visibility.seed(gap);
    expect(Visibility.isHidden(f.id, gap.id)).toBe(true);

    Visibility.reveal(f.id, gap.id);
    // Every render seeds the gaps it lays out; the mark left behind is
    // what stops the re-seed putting this one back.
    Visibility.seed(gap);
    Visibility.seed(gap);
    expect(Visibility.isHidden(f.id, gap.id)).toBe(false);
  });

  test("toggling reports the state it moved to", () => {
    const f = SRC();
    const span = Visibility.fileSpan(f, "user");
    expect(Visibility.toggle(span)).toBe(true);
    expect(Visibility.toggle(span)).toBe(false);
  });
});

describe("nested state survives its container", () => {
  test("expanding a container leaves the collapse inside it standing", () => {
    const f = SRC();
    const fold = Visibility.codeFoldSpan(f.id, {
      context: "right", right_start: 11, right_end: 13,
      left_start: null, left_end: null,
    }, "user");
    Visibility.hide(fold);
    Visibility.hide(Visibility.fileSpan(f, "user"));

    // Both cover line 12 while the file is shut.
    expect(Visibility.covering(f.id, "new", 12)).toHaveLength(2);

    Visibility.reveal(f.id, Visibility.fileSpanId(f.id));
    expect(Visibility.isHidden(f.id, fold.id)).toBe(true);
    expect(Visibility.lineHidden(f.id, "new", 12)).toBe(true);
  });
});

describe("a bulk action retracts only what it asserted", () => {
  function levelled(level: CollapseLevel): { data: ViewerData; src: FileBlock; lock: FileBlock } {
    const src = SRC();
    const lock = LOCK();
    const data = makeData([src, lock]);
    Visibility.setLevel(data, level);
    return { data, src, lock };
  }

  test("the level seeds files, hunks and segments by depth", () => {
    const ids = (level: CollapseLevel): string[] =>
      Visibility.levelSpans(makeData([SRC()]), level).map((s) => s.id).sort();
    expect(ids("files")).toEqual(["file:F0", "hunk:H0_0", "seg:H0_0_whole"]);
    expect(ids("hunks")).toEqual(["hunk:H0_0", "seg:H0_0_whole"]);
    expect(ids("segments")).toEqual(["seg:H0_0_whole"]);
    expect(ids("off")).toEqual([]);
  });

  test("a lockfile folded away by hand does not blow open on 'off'", () => {
    const { data, src, lock } = levelled("hunks");
    Visibility.hide(Visibility.fileSpan(lock, "user"));

    Visibility.setLevel(data, "off");

    expect(Visibility.isHidden(lock.id, Visibility.fileSpanId(lock.id))).toBe(true);
    expect(Visibility.isHidden(src.id, Visibility.hunkSpanId("H0_0"))).toBe(false);
    expect(Visibility.isHidden(lock.id, Visibility.hunkSpanId("H1_0"))).toBe(false);
  });

  test("a revealed gap outlives a level change too", () => {
    const { data, src } = levelled("hunks");
    const gap = Visibility.contextSpan(src.id, { start: 1, end: 9 }, { start: 1, end: 9 });
    Visibility.seed(gap);
    Visibility.reveal(src.id, gap.id);

    Visibility.setLevel(data, "files");

    expect(Visibility.isHidden(src.id, gap.id)).toBe(false);
    // ... while a gap the reviewer never touched stays shut.
    const other = Visibility.contextSpan(src.id, { start: 20, end: 30 }, { start: 20, end: 30 });
    Visibility.seed(other);
    expect(Visibility.isHidden(src.id, other.id)).toBe(true);
  });

  test("picking a level re-folds what the reviewer expanded under it", () => {
    const { data, src } = levelled("hunks");
    Visibility.reveal(src.id, Visibility.hunkSpanId("H0_0"));
    expect(Visibility.isHidden(src.id, Visibility.hunkSpanId("H0_0"))).toBe(false);

    // Even re-picking the level the reviewer is already on: the slider is
    // authoritative over its own spans.
    Visibility.setLevel(data, "hunks");
    expect(Visibility.isHidden(src.id, Visibility.hunkSpanId("H0_0"))).toBe(true);
  });

  test("re-asserting a level hide by hand makes it the reviewer's", () => {
    const { data, src } = levelled("hunks");
    Visibility.reveal(src.id, Visibility.hunkSpanId("H0_0"));
    Visibility.hide(Visibility.hunkSpan(src, src.hunks[0], "user"));

    Visibility.setLevel(data, "off");
    expect(Visibility.isHidden(src.id, Visibility.hunkSpanId("H0_0"))).toBe(true);
  });

  test("reset is the one control that drops everything", () => {
    const { data, lock } = levelled("hunks");
    Visibility.hide(Visibility.fileSpan(lock, "user"));
    Visibility.reset();
    Visibility.setLevel(data, "hunks");
    expect(Visibility.isHidden(lock.id, Visibility.fileSpanId(lock.id))).toBe(false);
  });
});

describe("nodes that arrive later take the current level", () => {
  test("a segment from an SSE hunk patch is seeded, a reveal is not undone", () => {
    const src = SRC();
    const data = makeData([src]);
    Visibility.setLevel(data, "segments");
    Visibility.reveal(src.id, Visibility.segmentSpanId("H0_0_whole"));

    src.hunks[0].segments = [
      { id: "H0_0_S0", new_start: 10, new_count: 2, intent: "", smells: [], context: "", refs: [] },
    ];
    Visibility.syncLevel(data, "segments");

    expect(Visibility.isHidden(src.id, Visibility.segmentSpanId("H0_0_S0"))).toBe(true);
    expect(Visibility.isHidden(src.id, Visibility.segmentSpanId("H0_0_whole"))).toBe(false);
  });

  test("a segment-less hunk folds as one synthetic segment spanning it", () => {
    const h = makeHunk("H0_0");
    expect(Visibility.displaySegments(h).map((s) => [s.id, s.new_start, s.new_count]))
      .toEqual([["H0_0_whole", 10, 4]]);
  });
});

describe("planRows drops what a CodeFold is holding down", () => {
  const rows = [row(10, 10), row(11, 11), row(12, 12), row(13, 13)];

  function foldOver(fileId: string, start: number, end: number): void {
    Visibility.hide(Visibility.codeFoldSpan(fileId, {
      context: "right", right_start: start, right_end: end,
      left_start: null, left_end: null,
    }, "user"));
  }

  test("every row renders while nothing is folded", () => {
    expect(Visibility.planRows("F0", rows, new Set())).toEqual([0, 1, 2, 3]);
  });

  test("the run's first line stays as the header the chevron hangs off", () => {
    foldOver("F0", 11, 13);
    expect(Visibility.planRows("F0", rows, new Set())).toEqual([0, 1]);
  });

  test("a definition whose opening line is off screen still gets a header", () => {
    // The fold starts at line 3, which this container does not render.
    foldOver("F0", 3, 12);
    expect(Visibility.planRows("F0", rows, new Set())).toEqual([0, 3]);
  });

  test("a fold nested inside a folded one places no header of its own", () => {
    foldOver("F0", 10, 13);
    foldOver("F0", 11, 12);
    expect(Visibility.planRows("F0", rows, new Set())).toEqual([0]);
  });

  test("an inner fold heads itself while the outer is open", () => {
    foldOver("F0", 11, 12);
    expect(Visibility.planRows("F0", rows, new Set())).toEqual([0, 1, 3]);
  });

  test("a fold straddling two containers places one header, in the first", () => {
    foldOver("F0", 11, 13);
    const headed = new Set<string>();
    expect(Visibility.planRows("F0", [rows[0], rows[1]], headed)).toEqual([0, 1]);
    expect(Visibility.planRows("F0", [rows[2], rows[3]], headed)).toEqual([]);
  });

  test("only CodeFolds are consulted — a hidden hunk never reaches here", () => {
    const f = SRC();
    Visibility.hide(Visibility.hunkSpan(f, f.hunks[0], "user"));
    expect(Visibility.planRows(f.id, rows, new Set())).toEqual([0, 1, 2, 3]);
  });
});
