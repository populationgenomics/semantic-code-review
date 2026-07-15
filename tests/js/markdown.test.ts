// GFM fidelity + sanitization for the rendered-markdown renderer.
//
// markdown-it output crosses DOMPurify before it reaches the DOM, so
// these cases pin both the GFM features the ADR calls table stakes
// (tables, task lists, strikethrough, autolinks) and the sanitization
// that untrusted .md demands.

import { describe, test, expect, afterEach, vi } from "vitest";
import { Markdown } from "../../semantic_code_review/viewer/assets/markdown";

describe("Markdown.render — GFM core", () => {
  test("headings render as heading elements", () => {
    const html = Markdown.render("# Title\n\n## Sub");
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<h2>Sub</h2>");
  });

  test("tables render", () => {
    const src = "| a | b |\n| --- | --- |\n| 1 | 2 |";
    const html = Markdown.render(src);
    expect(html).toContain("<table>");
    expect(html).toContain("<th>a</th>");
    expect(html).toContain("<td>1</td>");
  });

  test("strikethrough renders", () => {
    expect(Markdown.render("~~gone~~")).toContain("<s>gone</s>");
  });

  test("bare URLs autolink", () => {
    const html = Markdown.render("see https://example.com/x for more");
    expect(html).toContain('href="https://example.com/x"');
  });

  test("task lists render disabled checkboxes", () => {
    const html = Markdown.render("- [ ] todo\n- [x] done");
    expect(html).toContain('class="contains-task-list"');
    expect(html).toContain('type="checkbox"');
    expect(html).toContain("disabled");
    // The checked box carries `checked`; the unchecked one doesn't.
    const boxes = html.match(/<input[^>]*>/g) || [];
    expect(boxes.length).toBe(2);
    expect(boxes.filter((b) => b.includes("checked")).length).toBe(1);
    // The marker text is stripped, leaving the label.
    expect(html).toContain("todo");
    expect(html).toContain("done");
    expect(html).not.toContain("[ ]");
    expect(html).not.toContain("[x]");
  });
});

describe("Markdown.render — sanitization", () => {
  test("script tags never reach the output as live elements", () => {
    // html:false escapes the source; the escaped text is inert (the
    // literal string "alert(1)" surviving as visible text is harmless).
    const html = Markdown.render("hi\n\n<script>alert(1)</script>");
    expect(html).not.toContain("<script");
    expect(html).toContain("&lt;script&gt;");
  });

  test("javascript: URLs never become live links", () => {
    const html = Markdown.render("[click](javascript:alert(1))");
    expect(html).not.toContain('href="javascript');
  });

  test("raw HTML in the source is escaped, not parsed", () => {
    const html = Markdown.render("<img src=x onerror=alert(1)>");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});

describe("Markdown.render — links open safely", () => {
  test("anchors get target=_blank and rel=noopener", () => {
    const html = Markdown.render("[home](https://example.com)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain("noopener");
  });
});

describe("Markdown.renderBlocks — per-block spans", () => {
  test("splits top-level blocks with 1-indexed inclusive line spans", () => {
    // line 1: heading, 2: blank, 3-4: paragraph.
    const blocks = Markdown.renderBlocks("# Title\n\nfirst\nsecond");
    expect(blocks).toHaveLength(2);
    expect(blocks[0].startLine).toBe(1);
    expect(blocks[0].endLine).toBe(1);
    expect(blocks[0].html).toContain("<h1>Title</h1>");
    expect(blocks[1].startLine).toBe(3);
    expect(blocks[1].endLine).toBe(4);
    expect(blocks[1].html).toContain("first");
  });

  test("keeps a blockquote whole with its full span", () => {
    const blocks = Markdown.renderBlocks("> a\n> b\n> c");
    expect(blocks).toHaveLength(1);
    expect(blocks[0].startLine).toBe(1);
    expect(blocks[0].endLine).toBe(3);
    expect(blocks[0].html).toContain("<blockquote>");
  });

  test("a fenced code block is one block spanning its fences", () => {
    const blocks = Markdown.renderBlocks("intro\n\n```\ncode\n```\n");
    expect(blocks).toHaveLength(2);
    expect(blocks[1].startLine).toBe(3);
    expect(blocks[1].endLine).toBe(5);
    expect(blocks[1].html).toContain("<pre>");
  });

  test("per-block HTML is sanitized", () => {
    const blocks = Markdown.renderBlocks("ok\n\n<script>alert(1)</script>");
    const all = blocks.map((b) => b.html).join("");
    expect(all).not.toContain("<script");
  });

  test("a mermaid fence renders as a plain code block (hydrated later)", () => {
    const blocks = Markdown.renderBlocks("```mermaid\ngraph TD;A-->B\n```\n");
    expect(blocks).toHaveLength(1);
    expect(blocks[0].html).toContain('class="language-mermaid"');
    expect(blocks[0].html).toContain("graph TD;A--&gt;B");
  });
});

describe("Markdown.hydrate — mermaid", () => {
  // mermaid.ts memoises the load + per-source render; reset the module
  // graph per case so cached SVGs / failures don't leak between tests.
  async function fresh(): Promise<typeof Markdown> {
    vi.resetModules();
    const mod = await import("../../semantic_code_review/viewer/assets/markdown");
    return mod.Markdown;
  }

  function blockEl(block: { html: string }): HTMLElement {
    const el = document.createElement("div");
    el.innerHTML = block.html;
    document.body.appendChild(el);
    return el;
  }

  async function flush(): Promise<void> {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  }

  afterEach(() => {
    document.body.innerHTML = "";
    delete (window as unknown as { mermaid?: unknown }).mermaid;
    vi.restoreAllMocks();
  });

  test("swaps a mermaid fence into an svg via the mermaid global", async () => {
    const render = vi.fn(async () => ({ svg: "<svg data-test='d'></svg>" }));
    (window as unknown as { mermaid: unknown }).mermaid = { initialize: vi.fn(), render };
    const md = await fresh();
    const [block] = md.renderBlocks("```mermaid\ngraph TD;A-->B\n```\n");
    const el = blockEl(block);
    md.hydrate(el);
    await flush();
    expect(render).toHaveBeenCalledOnce();
    expect(el.querySelector(".rmd-mermaid svg")).not.toBeNull();
    expect(el.querySelector("pre > code.language-mermaid")).toBeNull();
  });

  test("re-hydrating a fresh copy re-injects the cached svg synchronously", async () => {
    const render = vi.fn(async () => ({ svg: "<svg></svg>" }));
    (window as unknown as { mermaid: unknown }).mermaid = { initialize: vi.fn(), render };
    const md = await fresh();
    const [block] = md.renderBlocks("```mermaid\nA-->B\n```\n");
    md.hydrate(blockEl(block));
    await flush();
    expect(render).toHaveBeenCalledOnce();
    // A repaint rebuilds the DOM; hydrate on a fresh copy must not render
    // again — the cached svg swaps in with no async round-trip.
    const el2 = blockEl(block);
    md.hydrate(el2);
    expect(el2.querySelector(".rmd-mermaid svg")).not.toBeNull();
    expect(render).toHaveBeenCalledOnce();
  });

  test("leaves the source fence when mermaid rejects the diagram", async () => {
    (window as unknown as { mermaid: unknown }).mermaid = {
      initialize: vi.fn(),
      render: vi.fn(async () => { throw new Error("parse error"); }),
    };
    const md = await fresh();
    const [block] = md.renderBlocks("```mermaid\nnope\n```\n");
    const el = blockEl(block);
    md.hydrate(el);
    await flush();
    expect(el.querySelector(".rmd-mermaid")).toBeNull();
    expect(el.querySelector("pre > code.language-mermaid")?.textContent).toContain("nope");
  });
});

describe("Markdown.renderBlocks — list splitting", () => {
  test("a list becomes one block per item, each its own single-item list", () => {
    const blocks = Markdown.renderBlocks("- a\n- b\n- c");
    expect(blocks).toHaveLength(3);
    expect(blocks.map((b) => [b.startLine, b.endLine])).toEqual([[1, 1], [2, 2], [3, 3]]);
    for (const b of blocks) {
      expect(b.html).toContain("<ul>");
      expect((b.html.match(/<li>/g) || []).length).toBe(1);
    }
  });

  test("only the changed item highlights — items carry independent spans", () => {
    // A 3-item list where the middle item spans two source lines; the
    // per-item spans are what let classification tint just that item.
    const blocks = Markdown.renderBlocks("- one\n- two\n  more\n- three");
    expect(blocks.map((b) => [b.startLine, b.endLine])).toEqual([[1, 1], [2, 3], [4, 4]]);
  });

  test("ordered list items keep their numbering via per-item start", () => {
    const blocks = Markdown.renderBlocks("3. c\n4. d\n5. e");
    expect(blocks).toHaveLength(3);
    expect(blocks[0].html).toContain('start="3"');
    expect(blocks[1].html).toContain('start="4"');
    expect(blocks[2].html).toContain('start="5"');
  });

  test("items are marked, the last distinctly, for stacked-item spacing", () => {
    const blocks = Markdown.renderBlocks("- a\n- b");
    expect(blocks.map((b) => b.listItem)).toEqual(["item", "last"]);
  });

  test("a nested list rides whole inside its parent item block", () => {
    const blocks = Markdown.renderBlocks("- top\n  - n1\n  - n2\n- second");
    expect(blocks).toHaveLength(2);
    // The parent item spans the nested list; the nested items are not
    // split out into their own blocks.
    expect(blocks[0].startLine).toBe(1);
    expect(blocks[0].endLine).toBe(3);
    expect((blocks[0].html.match(/<li>/g) || []).length).toBe(3); // top + 2 nested
    expect(blocks[1].html).toContain("second");
  });

  test("a task list splits per item and each keeps its checkbox + class", () => {
    const blocks = Markdown.renderBlocks("- [ ] todo\n- [x] done");
    expect(blocks).toHaveLength(2);
    for (const b of blocks) {
      expect(b.html).toContain("contains-task-list");
      expect(b.html).toContain('type="checkbox"');
    }
    expect(blocks[0].html).not.toContain("checked");
    expect(blocks[1].html).toContain("checked");
  });
});

describe("Markdown.render — math delimiters", () => {
  test("inline $…$ becomes a placeholder carrying the raw TeX", () => {
    const html = Markdown.render("mass is $E = mc^2$ ok");
    expect(html).toContain('class="rmd-math rmd-math-inline"');
    expect(html).toContain("E = mc^2");
  });

  test("an escaped \\$ is not a delimiter", () => {
    const html = Markdown.render("costs \\$5 to \\$9");
    expect(html).not.toContain("rmd-math");
  });

  test("bare $ around numbers reads as currency, not math", () => {
    const html = Markdown.render("it costs $5 and $10 total");
    expect(html).not.toContain("rmd-math");
  });

  test("$$…$$ on its own lines is one display-math block with its span", () => {
    const blocks = Markdown.renderBlocks("intro\n\n$$\na + b\n$$\n");
    expect(blocks).toHaveLength(2);
    expect(blocks[1].html).toContain('class="rmd-math rmd-math-display"');
    expect(blocks[1].html).toContain("a + b");
    expect(blocks[1].startLine).toBe(3);
  });

  test("single-line $$…$$ is a display-math block", () => {
    const blocks = Markdown.renderBlocks("$$a + b$$\n");
    expect(blocks).toHaveLength(1);
    expect(blocks[0].html).toContain('class="rmd-math rmd-math-display"');
    expect(blocks[0].html).toContain("a + b");
  });

  test("math placeholder text is escaped, so TeX with < survives sanitisation", () => {
    const html = Markdown.render("cond $a < b$ holds");
    expect(html).toContain('class="rmd-math rmd-math-inline"');
    expect(html).toContain("a &lt; b");
    expect(html).not.toContain("<b>");
  });
});

describe("Markdown.hydrate — math", () => {
  // katex.ts memoises the load + per-source render; reset the module
  // graph per case so cached renders / failures don't leak between tests.
  async function fresh(): Promise<typeof Markdown> {
    vi.resetModules();
    const mod = await import("../../semantic_code_review/viewer/assets/markdown");
    return mod.Markdown;
  }

  function blockEl(block: { html: string }): HTMLElement {
    const el = document.createElement("div");
    el.innerHTML = block.html;
    document.body.appendChild(el);
    return el;
  }

  async function flush(): Promise<void> {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  }

  afterEach(() => {
    document.body.innerHTML = "";
    delete (window as unknown as { katex?: unknown }).katex;
    vi.restoreAllMocks();
  });

  test("fills an inline math placeholder via the katex global", async () => {
    const renderToString = vi.fn((tex: string, opts: { displayMode: boolean }) => {
      expect(opts.displayMode).toBe(false);
      return `<span class="katex">rendered:${tex}</span>`;
    });
    (window as unknown as { katex: unknown }).katex = { renderToString };
    const md = await fresh();
    const [block] = md.renderBlocks("energy $E=mc^2$ done");
    const el = blockEl(block);
    md.hydrate(el);
    await flush();
    expect(renderToString).toHaveBeenCalledOnce();
    expect(el.querySelector(".rmd-math .katex")?.textContent).toBe("rendered:E=mc^2");
  });

  test("renders display math with displayMode:true", async () => {
    const renderToString = vi.fn((_tex: string, opts: { displayMode: boolean }) => {
      expect(opts.displayMode).toBe(true);
      return "<span class='katex-display'>d</span>";
    });
    (window as unknown as { katex: unknown }).katex = { renderToString };
    const md = await fresh();
    const [block] = md.renderBlocks("$$\na+b\n$$\n");
    const el = blockEl(block);
    md.hydrate(el);
    await flush();
    expect(renderToString).toHaveBeenCalledOnce();
    expect(el.querySelector(".rmd-math-display .katex-display")).not.toBeNull();
  });

  test("re-hydrating a fresh copy re-injects the cached render synchronously", async () => {
    const renderToString = vi.fn(() => "<span class='katex'>x</span>");
    (window as unknown as { katex: unknown }).katex = { renderToString };
    const md = await fresh();
    const [block] = md.renderBlocks("val $x$ here");
    md.hydrate(blockEl(block));
    await flush();
    expect(renderToString).toHaveBeenCalledOnce();
    const el2 = blockEl(block);
    md.hydrate(el2);
    expect(el2.querySelector(".rmd-math .katex")).not.toBeNull();
    expect(renderToString).toHaveBeenCalledOnce();
  });

  test("leaves the raw TeX when katex rejects the source", async () => {
    (window as unknown as { katex: unknown }).katex = {
      renderToString: vi.fn(() => { throw new Error("bad TeX"); }),
    };
    const md = await fresh();
    const [block] = md.renderBlocks("bad $\\frobnicate$ here");
    const el = blockEl(block);
    md.hydrate(el);
    await flush();
    expect(el.querySelector(".rmd-math .katex")).toBeNull();
    expect(el.querySelector(".rmd-math")?.textContent).toContain("\\frobnicate");
  });
});
