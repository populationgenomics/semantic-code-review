// What a hide's manifest lists, exercised without a rendered document.
//
// Two questions, both answerable from state: which of a file's notes can
// appear at all (resolved threads and replaced annotations cannot), and
// which of them a given hide is covering. The second is asked of the
// span store, so a manifest can only be built for a hide that is in
// place. The renderer's half — that a collapsed file actually shows the
// list, and that nothing skips the gutter silently — is in
// viewer.test.ts, which needs a document by definition.

import { describe, test, expect, beforeEach } from "vitest";
import { Manifest, type ManifestNote } from "../../semantic_code_review/viewer/assets/manifest";
import { Visibility } from "../../semantic_code_review/viewer/assets/visibility";

function makeHunk(id: string, overrides: Partial<HunkBlock> = {}): HunkBlock {
  return {
    id, header: "@@ -10,4 +10,4 @@",
    old_start: 10, old_count: 4, new_start: 10, new_count: 4,
    adds: 1, dels: 1, intent: "", smells: [], confidence: null, context: "",
    refs: [], line_notes: [], segments: [], rows: [],
    ...overrides,
  };
}

function makeFile(hunks: HunkBlock[] = [makeHunk("H0_0")]): FileBlock {
  return {
    id: "F0", path: "src/a.py", old_path: null, status: "modified",
    language: "python", adds: 1, dels: 1, summary: "",
    symbols: { added: [], modified: [], removed: [] },
    fold_symbols: { head: [], base: [] },
    hunks,
  };
}

function comment(
  id: string, line: number, body: string, extra: Partial<ReviewerComment> = {},
): ReviewerComment {
  return {
    id, file: "src/a.py", side: "new", line, body,
    created_at: 1, updated_at: 1, ...extra,
  };
}

/** `kind:side:line` plus the entry's text — the whole of what an entry
 *  says, in one comparable string. */
function shown(ns: ManifestNote[]): string[] {
  return ns.map((n) => `${n.kind}:${n.side}:${n.line} ${n.text}`);
}

beforeEach(() => {
  Visibility.reset();
});

describe("what can appear in a manifest", () => {
  test("an unresolved comment stays, wherever it came from", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("c1", 11, "local note"),
      comment("gh1", 12, "ingested note", { source: "github", author: "alice" }),
    ]);
    // Both are human-owned, which is the whole of the test the ADR sets.
    expect(shown(notes)).toEqual([
      "comment:new:11 local note",
      "comment:new:12 ingested note",
    ]);
  });

  test("a resolved thread drops out", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("gh1", 11, "settled", { source: "github", thread_resolved: true }),
      comment("gh2", 12, "still open", { source: "github" }),
    ]);
    expect(shown(notes)).toEqual(["comment:new:12 still open"]);
  });

  test("a reply is part of its thread, not an entry of its own", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("gh1", 11, "the question", { source: "github" }),
      comment("c2", 11, "the answer", { in_reply_to_id: "gh1" }),
    ]);
    expect(shown(notes)).toEqual(["comment:new:11 the question"]);
  });

  test("resolution is read from the root, so a reply cannot re-open it", () => {
    // `thread_resolved` is denormalised onto every member; a local reply
    // does not carry it, and honouring the reply would resurrect a
    // thread the reviewer has finished with.
    const notes = Manifest.notes(makeFile(), [
      comment("gh1", 11, "settled", { source: "github", thread_resolved: true }),
      comment("c2", 11, "one more thing", { in_reply_to_id: "gh1" }),
    ]);
    expect(notes).toEqual([]);
  });

  test("a comment promoted from an LLM annotation stays; the annotation goes", () => {
    // Where it came from does not decide anything — a human owns it now,
    // and the annotation it replaced would be the same note twice.
    const file = makeFile([makeHunk("H0_0", {
      line_notes: [{ line: 11, body: "shadows the outer name" }],
    })]);
    const notes = Manifest.notes(file, [
      comment("c1", 11, "shadows the outer name", {
        derived_from: "H0_0:line_note:11",
      }),
    ]);
    expect(shown(notes)).toEqual(["comment:new:11 shadows the outer name"]);
  });

  test("an LLM annotation is listed too, under its own kind", () => {
    const file = makeFile([makeHunk("H0_0", {
      line_notes: [{ line: 12, body: "unchecked index" }],
    })]);
    expect(shown(Manifest.notes(file, []))).toEqual([
      "annotation:new:12 unchecked index",
    ]);
  });

  test("a comment with no line at head has nothing to name and drops out", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("gh1", 11, "on a deleted file", {
        source: "github", anchor_status: "file_gone", head_line: null,
      }),
    ]);
    expect(notes).toEqual([]);
  });

  test("an ingested comment is listed at its propagated head line", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("gh1", 4, "was written against an older commit", {
        source: "github", anchor_status: "shifted", head_line: 11,
      }),
    ]);
    expect(notes[0].line).toBe(11);
  });

  test("an entry is one line: the note's first non-blank line", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("c1", 11, "\n  the point  \nand the elaboration\n"),
    ]);
    expect(notes[0].text).toBe("the point");
  });

  test("another file's comments are not this file's", () => {
    const notes = Manifest.notes(makeFile(), [
      comment("c1", 11, "elsewhere", { file: "src/b.py" }),
    ]);
    expect(notes).toEqual([]);
  });
});

describe("a manifest is what one hide covers", () => {
  const NOTES: ManifestNote[] = [
    { kind: "comment", side: "new", line: 11, text: "in the hunk" },
    { kind: "comment", side: "old", line: 11, text: "on the base side" },
    { kind: "annotation", side: "new", line: 40, text: "far below" },
  ];

  test("only the notes the named span is hiding, in line order", () => {
    const f = makeFile();
    Visibility.hide(Visibility.hunkSpan(f, f.hunks[0], "user"));
    // The hunk covers 10..13 on both sides; line 40 is outside it.
    expect(shown(Manifest.under(f.id, Visibility.hunkSpanId("H0_0"), NOTES)))
      .toEqual(["comment:new:11 in the hunk", "comment:old:11 on the base side"]);
  });

  test("a hide that is not in place lists nothing", () => {
    // The store is the authority, not the span's own ranges: a manifest
    // is a stand-in for something hidden, so an open hunk has none.
    const f = makeFile();
    expect(Manifest.under(f.id, Visibility.hunkSpanId("H0_0"), NOTES)).toEqual([]);
  });

  test("a right-only fold does not claim the note on the base side", () => {
    const f = makeFile();
    const addr = {
      context: "right" as const, right_start: 10, right_end: 13,
      left_start: null, left_end: null,
    };
    Visibility.hide(Visibility.codeFoldSpan(f.id, addr, "user"));
    expect(shown(Manifest.under(f.id, Visibility.codeFoldSpanId(f.id, addr), NOTES)))
      .toEqual(["comment:new:11 in the hunk"]);
  });

  test("an outer hide lists what the hide inside it also covers", () => {
    // Nesting is not partitioning: the file's manifest is what the
    // reviewer sees while the file is shut, whatever else is folded
    // underneath it.
    const f = makeFile();
    Visibility.hide(Visibility.hunkSpan(f, f.hunks[0], "user"));
    Visibility.hide(Visibility.fileSpan(f, "user"));
    expect(Manifest.under(f.id, Visibility.fileSpanId(f.id), NOTES)).toHaveLength(3);
  });

  test("a line range covers what no single span expresses", () => {
    // The seg-list case: while every segment is collapsed the hunk body
    // is gone, but a segment span covers neither the base side nor the
    // hunk's context rows.
    expect(shown(Manifest.inRange(NOTES, { start: 10, end: 13 }, { start: 10, end: 13 })))
      .toEqual(["comment:new:11 in the hunk", "comment:old:11 on the base side"]);
    // A pure-insertion hunk's base range is empty (end < start) and
    // covers nothing.
    expect(Manifest.inRange(NOTES, null, { start: 10, end: 9 })).toEqual([]);
  });

  test("the list is not bounded", () => {
    const f = makeFile();
    Visibility.hide(Visibility.fileSpan(f, "user"));
    const many: ManifestNote[] = Array.from({ length: 30 }, (_, i) => ({
      kind: "comment" as const, side: "new" as const, line: i + 1, text: `note ${i}`,
    }));
    expect(Manifest.under(f.id, Visibility.fileSpanId(f.id), many)).toHaveLength(30);
  });
});

describe("manifest layout", () => {
  const note = (side: "old" | "new", line: number, text: string): ManifestNote =>
    ({ kind: "comment", side, line, text });

  test("sides: two unlabelled columns in old/new order", () => {
    const el = Manifest.render([note("new", 12, "head"), note("old", 9, "base")])!;
    expect(el.className).toBe("manifest");
    expect(el.querySelectorAll(".manifest-col-label").length).toBe(0);
    expect(el.querySelector(".manifest-col-old")!.textContent).toContain("base");
    expect(el.querySelector(".manifest-col-new")!.textContent).toContain("head");
  });

  test("sides: a one-sided hide leaves its opposite column empty", () => {
    const el = Manifest.render([note("new", 12, "head-side only")])!;
    expect(el.querySelector(".manifest-col-old")!.children.length).toBe(0);
  });

  test("single: one column carrying both sides, in line order", () => {
    const el = Manifest.render(
      [note("old", 9, "base note"), note("new", 12, "head note")], "single",
    )!;
    expect(el.classList.contains("manifest-single")).toBe(true);
    const cols = el.querySelectorAll(".manifest-col");
    expect(cols.length).toBe(1);
    expect(Array.from(cols[0].querySelectorAll(".manifest-text")).map((n) => n.textContent))
      .toEqual(["base note", "head note"]);
  });

  test("single: entries keep their kind colour, which is what draws the bar", () => {
    const el = Manifest.render(
      [{ kind: "annotation", side: "new", line: 3, text: "llm" }], "single",
    )!;
    expect(el.querySelector(".manifest-entry")!.className)
      .toBe("manifest-entry manifest-annotation");
  });
});
