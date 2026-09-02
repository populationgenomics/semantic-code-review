// Render-time figure sanitisation (ADR 0007 slice 4).
//
// The server sanitises before it writes; this is the second pass, and it
// has to hold on its own — a document can arrive from a run directory
// this browser's server never wrote. Every case here feeds the renderer
// SVG the server would have rejected.

import { describe, test, expect } from "vitest";
import { ExplainerFigures } from "../../semantic_code_review/viewer/assets/explainer_figure";

const { sanitizeFigureSvg, renderFigure } = ExplainerFigures;

function svg(body: string, rootAttrs = 'viewBox="0 0 100 50"'): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" ${rootAttrs}>${body}</svg>`;
}

function figure(overrides: Partial<ExplainerFigure> = {}): ExplainerFigure {
  return {
    svg: svg('<rect class="d-box" x="0" y="0" width="10" height="10"/>'),
    alt: "the request path",
    caption: "",
    stripped: 0,
    ...overrides,
  };
}

describe("paint is not the model's to choose", () => {
  test("a hardcoded fill does not survive", () => {
    const out = sanitizeFigureSvg(
      svg('<rect class="d-box" x="1" y="2" width="10" height="8" fill="#ff00ff" stroke="red"/>'),
      "f0",
    );
    expect(out).not.toContain("ff00ff");
    expect(out).not.toContain("stroke");
    expect(out).toContain('class="d-box"');
    expect(out).toContain('x="1"');
  });

  test.each([
    'fill="#123456"',
    'stroke="#123456"',
    'stroke-width="9"',
    'stroke-dasharray="1 2"',
    'style="fill:red"',
    'font-family="Comic Sans"',
    'font-size="40"',
    'font-weight="900"',
    'color="red"',
    'opacity="0.2"',
  ])("%s is stripped", (attr) => {
    const out = sanitizeFigureSvg(svg(`<rect class="d-box" x="0" y="0" width="4" height="4" ${attr}/>`), "f0");
    expect(out).toBe(
      '<svg viewBox="0 0 100 50"><rect class="d-box" x="0" y="0" width="4" height="4"></rect></svg>',
    );
  });

  test("an unknown class is removed and the known ones stay", () => {
    const out = sanitizeFigureSvg(svg('<rect class="d-box brand-purple hl" x="0" y="0" width="4" height="4"/>'), "f0");
    expect(out).toContain('class="d-box hl"');
    expect(out).not.toContain("brand-purple");
  });

  test("a shape class on a text element is dropped", () => {
    const out = sanitizeFigureSvg(svg('<text class="chip" x="1" y="2">=1000</text>'), "f");
    expect(out).not.toContain("chip");
    expect(out).toContain("=1000");
  });

  test("a text class on a shape is dropped", () => {
    const out = sanitizeFigureSvg(svg('<rect class="t-b" x="0" y="0" width="4" height="4"/>'), "f");
    expect(out).not.toContain("t-b");
  });

  test("a class attribute with nothing known left is removed entirely", () => {
    const out = sanitizeFigureSvg(svg('<rect class="brand-purple" x="0" y="0" width="4" height="4"/>'), "f0");
    expect(out).not.toContain("class");
  });
});

describe("hostile input", () => {
  test("a script element does not survive", () => {
    const out = sanitizeFigureSvg(
      svg('<script>alert(1)</script><rect class="d-box" x="0" y="0" width="4" height="4"/>'),
      "f0",
    );
    expect(out.toLowerCase()).not.toContain("script");
    expect(out).not.toContain("alert");
    expect(out).toContain("<rect");
  });

  test("an event handler does not survive", () => {
    const out = sanitizeFigureSvg(svg('<rect class="d-box" x="0" y="0" width="4" height="4" onclick="alert(1)"/>'), "f0");
    expect(out).not.toContain("onclick");
    expect(out).not.toContain("alert");
  });

  test("a foreignObject does not survive", () => {
    const out = sanitizeFigureSvg(svg('<foreignObject width="9" height="9"><b>hi</b></foreignObject>'), "f0");
    expect(out.toLowerCase()).not.toContain("foreignobject");
  });

  test("an external reference does not survive", () => {
    const out = sanitizeFigureSvg(svg('<image href="https://evil.example/x.png" x="0" y="0"/>'), "f0");
    expect(out).not.toContain("evil.example");
  });

  test("a root that is not an svg yields nothing", () => {
    expect(sanitizeFigureSvg("<div>hello</div>", "f0")).toBe("");
  });

  test("a root without a viewBox yields nothing", () => {
    expect(sanitizeFigureSvg(svg('<rect x="0" y="0" width="1" height="1"/>', 'width="9"'), "f0")).toBe("");
  });

  test("root width and height are stripped; a rect keeps its own", () => {
    const out = sanitizeFigureSvg(
      svg('<rect class="d-box" x="0" y="0" width="4" height="4"/>', 'viewBox="0 0 100 50" width="800" height="400"'),
      "f0",
    );
    expect(out).toContain('width="4"');
    expect(out).not.toContain('width="800"');
    expect(out).not.toContain('height="400"');
  });
});

describe("marker ids", () => {
  const marker =
    '<defs><marker id="arrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="6" markerHeight="6" orient="auto">' +
    '<path class="head" d="M0,0 L6,3 L0,6 z"/></marker></defs>' +
    '<line class="ln" x1="0" y1="0" x2="50" y2="0" marker-end="url(#arrow)"/>';

  test("two figures with the same marker id cannot collide on one page", () => {
    const host = document.createElement("div");
    host.appendChild(renderFigure(figure({ svg: svg(marker) })));
    host.appendChild(renderFigure(figure({ svg: svg(marker) })));

    const ids = [...host.querySelectorAll("[id]")].map((n) => n.id);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);

    // Each line still points at the marker inside its own figure.
    const figures = [...host.querySelectorAll("figure")];
    for (const fig of figures) {
      const ref = fig.querySelector("line")!.getAttribute("marker-end")!;
      const target = ref.slice("url(#".length, -1);
      expect(fig.querySelector(`#${target}`)).not.toBeNull();
      expect(host.querySelectorAll(`[id="${target}"]`)).toHaveLength(1);
    }
  });

  test("a marker reference that is not a same-document url is dropped", () => {
    const out = sanitizeFigureSvg(
      svg('<line class="ln" x1="0" y1="0" x2="1" y2="1" marker-end="url(https://evil.example/m.svg#a)"/>'),
      "f0",
    );
    expect(out).not.toContain("marker-end");
    expect(out).not.toContain("evil.example");
  });
});

describe("the figure element", () => {
  test("alt becomes the svg's aria-label", () => {
    const fig = renderFigure(figure({ alt: "a proto field flowing to the client" }));
    const node = fig.querySelector("svg")!;
    expect(node.getAttribute("aria-label")).toBe("a proto field flowing to the client");
    expect(node.getAttribute("role")).toBe("img");
  });

  test("a caption renders as a figcaption", () => {
    const fig = renderFigure(figure({ caption: "how a read resolves" }));
    expect(fig.querySelector("figcaption")!.textContent).toBe("how a read resolves");
  });

  test("a caption is inline markdown, like every other short model string", () => {
    const fig = renderFigure(figure({ caption: "what `ListRequest` carries" }));
    const cap = fig.querySelector("figcaption")!;
    expect(cap.querySelector("code")!.textContent).toBe("ListRequest");
    expect(cap.querySelector("p")).toBeNull();
  });

  test("a caption's raw HTML is never interpreted", () => {
    const fig = renderFigure(figure({ caption: "<img src=x onerror=alert(1)>" }));
    expect(fig.querySelector("figcaption")!.querySelector("img")).toBeNull();
  });

  test("a figure that lost everything is kept, showing its alt text", () => {
    const fig = renderFigure(figure({ svg: "<div>not a diagram</div>", stripped: 3 }));
    expect(fig.querySelector("svg")).toBeNull();
    expect(fig.querySelector(".explainer-figure-empty")!.textContent).toBe("the request path");
  });

  test("a root without a viewBox is alt text, not an uncapped figure", () => {
    const fig = renderFigure(
      figure({ svg: svg('<rect class="d-box" x="0" y="0" width="4" height="4"/>', 'width="800"') }),
    );
    expect(fig.querySelector("svg")).toBeNull();
    expect(fig.querySelector(".explainer-figure-empty")!.textContent).toBe("the request path");
  });

  test("the strip count is rendered, not swallowed", () => {
    const fig = renderFigure(figure({ stripped: 4 }));
    expect(fig.querySelector(".explainer-figure-stripped")!.textContent).toContain("4");
    expect(renderFigure(figure({ stripped: 0 })).querySelector(".explainer-figure-stripped")).toBeNull();
  });
});

// A label's px size is a viewBox user unit, so a figure rendered wider
// than it was drawn magnifies every label with it.
describe("a figure is never larger than its drawn size", () => {
  function rendered(viewBox: string): SVGElement {
    const fig = renderFigure(figure({ svg: svg('<text class="t" x="10" y="10">label</text>', viewBox) }));
    return fig.querySelector("svg")!;
  }

  test("the viewBox width becomes the svg's max-width", () => {
    expect(rendered('viewBox="0 0 660 320"').style.maxWidth).toBe("660px");
  });

  test("a viewBox origin other than 0 0 does not shift the cap", () => {
    expect(rendered('viewBox="-20 -10 480 240"').style.maxWidth).toBe("480px");
  });

  test.each([
    'viewBox="0 0 660"',
    'viewBox="0 0 wide 320"',
    'viewBox="0 0 0 320"',
    'viewBox="0 0 -660 320"',
  ])("%s is not a width, so nothing is capped", (viewBox) => {
    expect(rendered(viewBox).style.maxWidth).toBe("");
  });
});
