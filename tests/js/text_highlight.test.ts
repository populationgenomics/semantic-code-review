import { describe, test, expect } from "vitest";
import { blockDiff, matchRanges, wrapRanges, type CharRange } from "../../semantic_code_review/viewer/assets/text_highlight";

describe("blockDiff", () => {
  // Join each line's marked substrings — what wrapRanges would highlight.
  const markedLine = (line: string, ranges: CharRange[]) =>
    ranges.map(([s, e]) => line.slice(s, e)).join("");

  test("single line: near-adjacent changes coalesce into one block", () => {
    const a = "connect(host, 80, ssl)";
    const b = "connect(host, 443, tls)";
    const d = blockDiff([a], [b]);
    // 80 and ssl are separated only by ", " (<= the coalesce gap), so they
    // merge into one changed block; "connect(host, " and ")" stay clean.
    expect(markedLine(a, d.old[0])).toBe("80, ssl");
    expect(markedLine(b, d.new[0])).toBe("443, tls");
  });

  test("changes separated by a long unchanged stretch stay distinct", () => {
    const a = "alpha = 1; veryLongUnchangedMiddleSection; omega = 2";
    const b = "ALPHA = 1; veryLongUnchangedMiddleSection; OMEGA = 2";
    const d = blockDiff([a], [b]);
    // The wide matched middle keeps the two edits as separate blocks.
    expect(d.old[0].length).toBe(2);
    expect(markedLine(a, [d.old[0][0]])).toBe("alpha");
    expect(markedLine(a, [d.old[0][1]])).toBe("omega");
  });

  test("single line: pure insertion marks only the new side", () => {
    const a = "import { charDiff, wrapRanges }";
    const b = "import { charDiff, matchRanges, wrapRanges }";
    const d = blockDiff([a], [b]);
    expect(d.old[0]).toEqual([]);
    expect(markedLine(b, d.new[0])).toBe("matchRanges, ");
  });

  test("multi-line block: an inline type collapsed to a named type", () => {
    // The motivating case: 4 old lines -> 1 new line. The diff runs across
    // line boundaries, so the deleted object-type literal is marked as a
    // unit and SideRanges as the lone insertion — not decomposed per line.
    const oldLines = [
      "export function charDiff(a: string, b: string): {",
      "  oldRanges: CharRange[];",
      "  newRanges: CharRange[];",
      "} {",
    ];
    const newLines = ["export function charDiff(a: string, b: string): SideRanges {"];
    const d = blockDiff(oldLines, newLines);
    // Old side: just the opening brace on line 0, both body lines whole,
    // and the closing brace on the last line (the trailing " {" is kept).
    expect(markedLine(oldLines[0], d.old[0])).toBe("{");
    expect(markedLine(oldLines[1], d.old[1])).toBe(oldLines[1]);
    expect(markedLine(oldLines[2], d.old[2])).toBe(oldLines[2]);
    expect(markedLine(oldLines[3], d.old[3])).toBe("}");
    // New side: only the inserted type name.
    expect(markedLine(newLines[0], d.new[0])).toBe("SideRanges");
  });

  test("identical lines produce no ranges", () => {
    expect(blockDiff(["same()"], ["same()"])).toEqual({ old: [[]], new: [[]] });
  });

  test("returns row-tint-only (empty) ranges past the token-product guard", () => {
    // A pair of long, totally-distinct lines exceeds the product cap.
    const a = Array.from({ length: 600 }, (_, i) => `a${i};`).join("");
    const b = Array.from({ length: 600 }, (_, i) => `b${i};`).join("");
    expect(blockDiff([a], [b])).toEqual({ old: [[]], new: [[]] });
  });

  test("ranges feed wrapRanges to mark each changed token", () => {
    const a = "f(a, b)";
    const b = "f(c, b)";
    const el = document.createElement("code");
    el.textContent = b;
    wrapRanges(el, blockDiff([a], [b]).new[0], "char-chg");
    // Only "c" is marked; "b" (unchanged) and punctuation stay plain.
    expect([...el.querySelectorAll("span.char-chg")].map((m) => m.textContent)).toEqual(["c"]);
  });
});

describe("matchRanges", () => {
  test("matches whole-identifier occurrences only", () => {
    const text = "get(getName(widget), get);";
    const ranges = matchRanges(text, "get");
    // "get(" at 0 and " get)" at 21 — but NOT getName / widget substrings.
    expect(ranges).toEqual([[0, 3], [21, 24]]);
    for (const [s, e] of ranges) expect(text.slice(s, e)).toBe("get");
  });

  test("respects _ and $ as identifier characters", () => {
    expect(matchRanges("a_b ab a", "a")).toEqual([[7, 8]]);
    expect(matchRanges("$x x $xy", "x")).toEqual([[3, 4]]);
  });

  test("empty term yields no ranges", () => {
    expect(matchRanges("anything", "")).toEqual([]);
  });

  test("no occurrence yields no ranges", () => {
    expect(matchRanges("foo bar", "baz")).toEqual([]);
  });

  test("regex metacharacters in the term are matched literally", () => {
    expect(matchRanges("a.b a+b", "a.b")).toEqual([[0, 3]]);
  });

  test("feeds wrapRanges to highlight matches", () => {
    const el = document.createElement("code");
    el.textContent = "compute(x); compute(y)";
    wrapRanges(el, matchRanges(el.textContent, "compute"), "symbol-hit");
    expect([...el.querySelectorAll("span.symbol-hit")].map((m) => m.textContent))
      .toEqual(["compute", "compute"]);
  });
});


describe("wrapRanges", () => {
  function frag(html: string): HTMLElement {
    const el = document.createElement("code");
    el.innerHTML = html;
    return el;
  }

  test("wraps a range inside a single text node", () => {
    const el = frag("hello world");
    wrapRanges(el, [[6, 11]], "m");
    expect(el.querySelectorAll("span.m").length).toBe(1);
    expect(el.querySelector("span.m")!.textContent).toBe("world");
    expect(el.textContent).toBe("hello world");
  });

  test("a range spanning existing inline spans wraps within each", () => {
    // highlight.js-style markup: <span class=kw>def</span> foo
    const el = frag('<span class="hljs-keyword">def</span> foo');
    expect(el.textContent).toBe("def foo");
    wrapRanges(el, [[1, 6]], "m"); // "ef fo"
    const marks = [...el.querySelectorAll("span.m")];
    // One mark inside the keyword span ("ef"), one in the trailing text node
    // (" fo" — the space lives in the text node after the span).
    expect(marks.map((m) => m.textContent)).toEqual(["ef", " fo"]);
    expect(el.textContent).toBe("def foo");
    // The keyword span is preserved as an ancestor of its mark.
    expect(el.querySelector(".hljs-keyword")!.textContent).toBe("def");
  });

  test("multiple ranges in one node", () => {
    const el = frag("abcdef");
    wrapRanges(el, [[0, 1], [3, 4]], "m");
    expect([...el.querySelectorAll("span.m")].map((m) => m.textContent)).toEqual(["a", "d"]);
    expect(el.textContent).toBe("abcdef");
  });

  test("empty / no ranges is a no-op", () => {
    const el = frag("untouched");
    wrapRanges(el, [], "m");
    expect(el.querySelector("span.m")).toBeNull();
    expect(el.innerHTML).toBe("untouched");
  });

  test("overlapping and unsorted ranges are normalised", () => {
    const el = frag("abcdef");
    wrapRanges(el, [[3, 5], [0, 2], [1, 3]], "m");
    // [0,2]+[1,3] merge to [0,3]; plus [3,5] touches -> [0,5].
    expect([...el.querySelectorAll("span.m")].map((m) => m.textContent)).toEqual(["abcde"]);
  });
});
