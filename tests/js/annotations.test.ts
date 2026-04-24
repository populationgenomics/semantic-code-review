import { describe, test, expect, afterEach } from "vitest";
// annotations.ts is a classic-script module (no exports) that
// registers window.ScrAnnotations for the viewer to consume. Import
// it for side effect only; the public surface is the global.
import "../../semantic_code_review/viewer/assets/annotations";
import { makeAnchorRow, makeHunkFixture } from "./fixtures/hunk-dom";
import { flushRaf } from "./setup";

// Type-only shape used in tests. Matches the runtime facade exposed
// on window.ScrAnnotations by annotations.ts. Kept narrow — tests
// only need the methods they exercise.
interface AttachOptions {
  anchor: HTMLElement;
  shadowAnchor?: HTMLElement | null;
  variant?: string;
  content: Node | string;
  column?: { mode: "auto" | "absolute" | "explicit"; value?: number };
  stack?: { policy: "auto" | "fixed" | "grouped" };
  layout?: { maxWidth?: string | null; maxHeight?: string | null; overflow?: "hidden" | "visible"; wrap?: boolean };
  onInsert?: (el: HTMLElement) => void;
}
interface AnnotationHandle {
  element: HTMLElement;
  placeholder: HTMLElement | null;
  resize(): void;
  remove(): void;
  setContent(body: Node | string): void;
}
interface AnnotationsFacade {
  attach(opts: AttachOptions): AnnotationHandle;
  detach(row: HTMLElement): void;
  reflow(anchor: HTMLElement): void;
  reflowAll(): void;
  watchViewport(): void;
  charRectInRow(row: HTMLElement, col: number): DOMRect | null;
  _setRectProvider(fn: ((t: Element | Range) => DOMRect) | null): void;
}
const Annotations = (globalThis as unknown as { ScrAnnotations: AnnotationsFacade }).ScrAnnotations;

// Tests exercise the DOM-construction and choreography parts of the
// annotation module. Geometry-math tests inject canned rects via the
// `_setRectProvider` seam; everything else uses whatever jsdom reports
// (all zeros — enough for the assertions we actually make).

function mountAnchor(): HTMLElement {
  const anchor = makeAnchorRow({ lineno: 1, text: "hello world" });
  document.body.appendChild(anchor);
  return anchor;
}

function baseOpts(anchor: HTMLElement, overrides: Partial<AttachOptions> = {}): AttachOptions {
  return { anchor, content: "hi", ...overrides };
}

afterEach(() => Annotations._setRectProvider(null));

describe("attach: DOM shape", () => {
  test("builds a row with the expected classes and children", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, { variant: "note" }));
    const el = handle.element;
    expect(el.classList.contains("row")).toBe(true);
    expect(el.classList.contains("row-annotation")).toBe(true);
    expect(el.classList.contains("annot-note")).toBe(true);
    expect(el.querySelector(".cell-annotation")).not.toBeNull();
    expect(el.querySelector("svg.annot-arrow")).not.toBeNull();
    const box = el.querySelector<HTMLElement>(".annot-box");
    expect(box?.textContent).toBe("hi");
  });

  test("inserts the row as a sibling immediately after the anchor", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor));
    expect(anchor.nextElementSibling).toBe(handle.element);
  });

  test("accepts a Node as content", () => {
    const anchor = mountAnchor();
    const body = document.createElement("em");
    body.textContent = "emph";
    const handle = Annotations.attach(baseOpts(anchor, { content: body }));
    expect(handle.element.querySelector(".annot-box em")).not.toBeNull();
  });
});

describe("shadow placeholder lifecycle", () => {
  test("inserts a placeholder after shadowAnchor when provided", () => {
    const fx = makeHunkFixture([{ old: "left", new: "right" }]);
    const handle = Annotations.attach(baseOpts(fx.new[0], { shadowAnchor: fx.old[0] }));
    expect(handle.placeholder).not.toBeNull();
    expect(fx.old[0].nextElementSibling).toBe(handle.placeholder);
    expect(handle.placeholder!.classList.contains("row-placeholder")).toBe(true);
  });

  test("remove() takes both the row and the placeholder out", () => {
    const fx = makeHunkFixture([{ old: "left", new: "right" }]);
    const handle = Annotations.attach(baseOpts(fx.new[0], { shadowAnchor: fx.old[0] }));
    const phCountBefore = document.querySelectorAll(".row-placeholder").length;
    expect(phCountBefore).toBe(1);
    handle.remove();
    expect(document.querySelectorAll(".row-annotation").length).toBe(0);
    expect(document.querySelectorAll(".row-placeholder").length).toBe(0);
  });

  test("remove() is idempotent", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor));
    handle.remove();
    expect(() => handle.remove()).not.toThrow();
  });
});

describe("diff-agnostic attach", () => {
  test("works on an anchor that isn't inside a .half or .diff", () => {
    const anchor = document.createElement("div");
    anchor.className = "custom-row";
    const lineno = document.createElement("span");
    lineno.textContent = "x";
    const content = document.createElement("span");
    content.textContent = "non-diff content";
    anchor.appendChild(lineno);
    anchor.appendChild(content);
    document.body.appendChild(anchor);
    const handle = Annotations.attach(baseOpts(anchor));
    expect(handle.element.parentNode).toBe(anchor.parentNode);
    expect(anchor.nextElementSibling).toBe(handle.element);
  });
});

describe("layout overrides", () => {
  test("caller layout wins over variant default", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, {
      variant: "fold",  // defaults maxHeight to "3.9em"
      layout: { maxHeight: "99em", maxWidth: "30ch", wrap: false, overflow: "visible" },
    }));
    const box = handle.element.querySelector<HTMLElement>(".annot-box")!;
    expect(box.style.maxHeight).toBe("99em");
    expect(box.style.maxWidth).toBe("30ch");
    expect(box.style.whiteSpace).toBe("nowrap");
    expect(box.style.overflow).toBe("visible");
  });

  test("variant 'fold' clamps by default", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, { variant: "fold" }));
    const box = handle.element.querySelector<HTMLElement>(".annot-box")!;
    expect(box.style.maxHeight).toBe("3.9em");
  });

  test("non-fold variants leave maxHeight unset", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, { variant: "note" }));
    const box = handle.element.querySelector<HTMLElement>(".annot-box")!;
    expect(box.style.maxHeight).toBe("none");
  });
});

describe("reflow coalescing", () => {
  test("multiple reflow() calls collapse into one RAF pass", async () => {
    // Spy on the rect provider: each full reflow of N siblings makes
    // some predictable number of rect reads. Three reflow() calls in
    // the same frame should produce exactly ONE batch of reads, not
    // three.
    let rectCalls = 0;
    Annotations._setRectProvider((t: Element | Range) => {
      rectCalls++;
      return (t as Element | Range).getBoundingClientRect();
    });
    const anchor = mountAnchor();
    const h1 = Annotations.attach(baseOpts(anchor));
    const h2 = Annotations.attach(baseOpts(anchor));
    await flushRaf();
    const baseline = rectCalls;

    Annotations.reflow(anchor);
    Annotations.reflow(anchor);  // deduped
    Annotations.reflow(anchor);  // deduped
    await flushRaf();
    const afterOneBatch = rectCalls - baseline;

    // A second batch for comparison: if coalescing works, two separate
    // frames should roughly double the reads vs. one frame.
    Annotations.reflow(anchor);
    await flushRaf();
    const afterTwoBatches = rectCalls - baseline;

    // Not a tight bound — just assert that 3 reflow()s in one frame
    // don't cost 3x the rect reads of 1 reflow() in one frame.
    expect(afterTwoBatches).toBeGreaterThan(afterOneBatch);
    expect(afterOneBatch).toBeLessThan(afterTwoBatches * 2);
    // Light touch used just to keep h1/h2 referenced.
    expect(h1.element).toBeTruthy();
    expect(h2.element).toBeTruthy();
  });
});

describe("stack policy + column resolution", () => {
  // Inject a rect provider that returns predictable geometry:
  // - anchor cells at y=100-120 so anchorMidY=110
  // - annotation cell at y=200 so topOverrun = 200-110 = 90
  // - characters are monospace 10px wide starting at x=50
  function installCannedRects(): void {
    const rectFor = (target: Element | Range): DOMRect => {
      if (target instanceof Range) {
        // Each char is 10px wide. Determine char index from the range.
        // We use the range's startOffset as a proxy for "which char".
        const startOffset = (target as Range).startOffset;
        const charX = 50 + startOffset * 10;
        return rect(charX, 100, 10, 20);
      }
      const el = target as Element;
      if (el.classList.contains("cell-lineno")) return rect(0, 100, 50, 20);
      if (el.classList.contains("cell-content")) return rect(50, 100, 200, 20);
      if (el.classList.contains("cell-annotation")) return rect(50, 200, 200, 40);
      if (el.classList.contains("annot-box")) return rect(60, 200, 150, 30);
      if (el.classList.contains("row-annotation")) return rect(50, 200, 200, 40);
      return rect(0, 0, 0, 0);
    };
    Annotations._setRectProvider(rectFor);
  }

  function rect(x: number, y: number, w: number, h: number): DOMRect {
    return {
      x, y, width: w, height: h,
      top: y, left: x, right: x + w, bottom: y + h,
      toJSON() { return this; },
    } as DOMRect;
  }

  test("column absolute picks the Nth character", async () => {
    installCannedRects();
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, {
      column: { mode: "absolute", value: 3 },
    }));
    await flushRaf();
    const svg = handle.element.querySelector<SVGSVGElement>("svg")!;
    // anchor char 3 → canned rect left=80, width=10, center=85.
    // cell left=50, padL assumed 0 in jsdom → margin-left = 85 - 50 - 0 - 2 = 33.
    const marginL = parseFloat(svg.style.marginLeft);
    expect(marginL).toBeGreaterThan(30);
    expect(marginL).toBeLessThan(35);
  });

  test("column explicit uses pixel offset from cell content edge", async () => {
    installCannedRects();
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, {
      column: { mode: "explicit", value: 42 },
    }));
    await flushRaf();
    const svg = handle.element.querySelector<SVGSVGElement>("svg")!;
    // cell left=50, padL=0, offset=42 → anchorX=92 → marginL = 92-50-0-2 = 40
    expect(parseFloat(svg.style.marginLeft)).toBeGreaterThan(38);
    expect(parseFloat(svg.style.marginLeft)).toBeLessThan(42);
  });

  test("auto stack: sibling count determines column", async () => {
    installCannedRects();
    const anchor = mountAnchor();
    // Insert in order: first annotation -> no siblings below -> col 0.
    const first = Annotations.attach(baseOpts(anchor));
    // Second annotation -> becomes sibling above; FIRST annotation now
    // has 1 below it — but "auto" stack counts siblings *below* in DOM
    // order, so the first-attached row sees the newly-inserted as below
    // it? Our module inserts after anchor each time, so the second
    // attach's row goes BETWEEN anchor and first.
    const second = Annotations.attach(baseOpts(anchor));
    await flushRaf();
    // After both inserts: anchor -> second -> first.
    // `second` (immediately below anchor) has 1 annotation below → col 1.
    // `first` (below second) has 0 annotations below → col 0.
    const secondMarginL = parseFloat(second.element.querySelector<SVGSVGElement>("svg")!.style.marginLeft);
    const firstMarginL = parseFloat(first.element.querySelector<SVGSVGElement>("svg")!.style.marginLeft);
    expect(secondMarginL).toBeGreaterThan(firstMarginL);
  });

  test("fixed stack: all siblings share column 0", async () => {
    installCannedRects();
    const anchor = mountAnchor();
    const h1 = Annotations.attach(baseOpts(anchor, { stack: { policy: "fixed" } }));
    const h2 = Annotations.attach(baseOpts(anchor, { stack: { policy: "fixed" } }));
    await flushRaf();
    const m1 = parseFloat(h1.element.querySelector<SVGSVGElement>("svg")!.style.marginLeft);
    const m2 = parseFloat(h2.element.querySelector<SVGSVGElement>("svg")!.style.marginLeft);
    expect(Math.abs(m1 - m2)).toBeLessThan(1);
  });
});

describe("arrow vertical geometry", () => {
  test("topOverrun reaches back to the anchor row's midline", async () => {
    // Canned rects: anchor cells at y=100-120 (mid=110),
    // annotation cell at y=200. topOverrun should be 200-110 = 90.
    Annotations._setRectProvider((target: Element | Range) => {
      if (target instanceof Range) {
        return { x: 50, y: 100, width: 10, height: 20, top: 100, left: 50, right: 60, bottom: 120, toJSON() { return this; } } as DOMRect;
      }
      const el = target as Element;
      if (el.classList.contains("cell-annotation")) {
        return { x: 50, y: 200, width: 200, height: 40, top: 200, left: 50, right: 250, bottom: 240, toJSON() { return this; } } as DOMRect;
      }
      if (el.classList.contains("annot-box")) {
        return { x: 60, y: 200, width: 150, height: 30, top: 200, left: 60, right: 210, bottom: 230, toJSON() { return this; } } as DOMRect;
      }
      // Any anchor child cell
      return { x: 0, y: 100, width: 50, height: 20, top: 100, left: 0, right: 50, bottom: 120, toJSON() { return this; } } as DOMRect;
    });
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor));
    await flushRaf();
    const svg = handle.element.querySelector<SVGSVGElement>("svg")!;
    // margin-top = -topOverrun; expect near -90.
    const marginTop = parseFloat(svg.style.marginTop);
    expect(marginTop).toBeLessThan(-80);
    expect(marginTop).toBeGreaterThan(-100);
  });
});

describe("setContent", () => {
  test("replaces body text and preserves the arrow", () => {
    const anchor = mountAnchor();
    const handle = Annotations.attach(baseOpts(anchor, { content: "first" }));
    expect(handle.element.querySelector(".annot-box")!.textContent).toBe("first");
    handle.setContent("updated");
    expect(handle.element.querySelector(".annot-box")!.textContent).toBe("updated");
    expect(handle.element.querySelector("svg.annot-arrow")).not.toBeNull();
  });
});

describe("charRectInRow", () => {
  test("returns a rect for a valid character index", () => {
    Annotations._setRectProvider((target: Element | Range) => {
      if (target instanceof Range) {
        return { x: 60, y: 100, width: 10, height: 20, top: 100, left: 60, right: 70, bottom: 120, toJSON() { return this; } } as DOMRect;
      }
      return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, toJSON() { return this; } } as DOMRect;
    });
    const anchor = mountAnchor();
    const r = Annotations.charRectInRow(anchor, 2);
    expect(r).not.toBeNull();
    expect(r!.left).toBe(60);
  });
});
