import { describe, test, expect } from "vitest";
import { charDiff, matchRanges, wrapRanges, type CharRange } from "../../semantic_code_review/viewer/assets/text_highlight";

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

describe("charDiff", () => {
  test("small insertion: only the new side is marked", () => {
    const d = charDiff("x = 1", "x = 12");
    expect(d.oldRanges).toEqual([]);
    expect(d.newRanges).toEqual([[5, 6]]);
    expect("x = 12".slice(5, 6)).toBe("2");
  });

  test("small deletion: only the old side is marked", () => {
    const d = charDiff("value = 100", "value = 1");
    expect(d.newRanges).toEqual([]);
    expect(d.oldRanges).toEqual([[9, 11]]);
    expect("value = 100".slice(9, 11)).toBe("00");
  });

  test("mid-line replacement marks the differing span on each side", () => {
    const d = charDiff("foo(a, b)", "foo(a, c, b)");
    const [os, oe] = d.oldRanges[0] ?? [0, 0];
    const [ns, ne] = d.newRanges[0] ?? [0, 0];
    // Common prefix "foo(a, " and common suffix "b)" are stripped.
    expect("foo(a, b)".slice(os, oe)).toBe("");
    expect("foo(a, c, b)".slice(ns, ne)).toBe("c, ");
  });

  test("identical strings produce no ranges", () => {
    expect(charDiff("same", "same")).toEqual({ oldRanges: [], newRanges: [] });
  });

  test("complete replacement marks the whole of both sides", () => {
    expect(charDiff("foo", "bar")).toEqual({ oldRanges: [[0, 3]], newRanges: [[0, 3]] });
  });

  test("prefix and suffix never overlap (repeated chars)", () => {
    // "aaa" -> "aa": prefix consumes 2, suffix must not double-count.
    const d = charDiff("aaa", "aa");
    expect(d.oldRanges).toEqual([[2, 3]]);
    expect(d.newRanges).toEqual([]);
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
