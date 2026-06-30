import { describe, test, expect, afterEach, vi } from "vitest";
import {
  renderConsoleMarkdown,
  scanMermaidFences,
} from "../../semantic_code_review/viewer/assets/console_render";

// Flush the microtask queue a few times so the async mermaid
// load/render chain (loadMermaid → render → cache → re-paint) settles.
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

function render(md: string): HTMLElement {
  const el = document.createElement("div");
  renderConsoleMarkdown(el, md);
  return el;
}

afterEach(() => {
  document.body.innerHTML = "";
  delete (window as unknown as { mermaid?: unknown }).mermaid;
  vi.restoreAllMocks();
});

describe("renderConsoleMarkdown — markdown", () => {
  test("renders headings, emphasis, and lists", () => {
    const el = render("# Title\n\nSome **bold** and *em*.\n\n- one\n- two\n");
    expect(el.querySelector("h1")?.textContent).toBe("Title");
    expect(el.querySelector("strong")?.textContent).toBe("bold");
    expect(el.querySelector("em")?.textContent).toBe("em");
    expect(el.querySelectorAll("li").length).toBe(2);
  });

  test("renders inline code and fenced code blocks", () => {
    const el = render("Call `foo()`.\n\n```js\nconst a = 1;\n```\n");
    expect(el.querySelector("p code")?.textContent).toBe("foo()");
    const code = el.querySelector("pre code");
    expect(code).not.toBeNull();
    // hljs is absent under jsdom → markdown-it's escaped fallback, tagged
    // with the language class.
    expect(code?.className).toContain("language-js");
    expect(code?.textContent).toContain("const a = 1;");
  });

  test("linkifies bare URLs into anchors", () => {
    const el = render("see https://example.com/x for details");
    const a = el.querySelector("a");
    expect(a?.getAttribute("href")).toBe("https://example.com/x");
  });
});

describe("renderConsoleMarkdown — sanitisation", () => {
  test("neutralises a raw <script> tag in model output", () => {
    const el = render("before\n\n<script>window.__pwned = 1;</script>\n\nafter");
    expect(el.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  test("strips an onerror handler from an injected <img>", () => {
    const el = render('<img src=x onerror="window.__pwned = 1">');
    const img = el.querySelector("img");
    // html:false means the tag is escaped to text in the common case;
    // either way there must be no live onerror attribute.
    if (img) expect(img.getAttribute("onerror")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });
});

describe("scanMermaidFences", () => {
  test("flags a closed fence closed and an unterminated one open", () => {
    expect(scanMermaidFences("```mermaid\ngraph TD;A-->B\n```\n")).toEqual([
      { closed: true },
    ]);
    expect(scanMermaidFences("```mermaid\ngraph TD;A-->B")).toEqual([
      { closed: false },
    ]);
  });

  test("keeps source order across interleaved non-mermaid fences", () => {
    const src =
      "```js\nx\n```\n" + // non-mermaid, consumed but not recorded
      "```mermaid\nA\n```\n" + // closed
      "```mermaid\nB"; // trailing, unclosed
    expect(scanMermaidFences(src)).toEqual([{ closed: true }, { closed: false }]);
  });

  test("ignores prose with no fences", () => {
    expect(scanMermaidFences("just text\nmore text")).toEqual([]);
  });
});

describe("renderConsoleMarkdown — mermaid", () => {
  // `console_render` memoises the mermaid load + per-source render (load
  // once in production), so each mermaid test gets a fresh module to
  // avoid leaking that state between cases.
  async function freshRender(): Promise<(el: HTMLElement, md: string) => void> {
    vi.resetModules();
    const mod = await import(
      "../../semantic_code_review/viewer/assets/console_render"
    );
    return mod.renderConsoleMarkdown;
  }

  test("leaves an unterminated mermaid fence as raw source", async () => {
    const renderMd = await freshRender();
    const el = document.createElement("div");
    renderMd(el, "```mermaid\ngraph TD;A-->B");
    await flush();
    expect(el.querySelector(".console-mermaid")).toBeNull();
    expect(el.querySelector("pre code.language-mermaid")?.textContent).toContain(
      "graph TD;A-->B",
    );
  });

  test("renders a completed fence into an svg via the mermaid global", async () => {
    const renderFn = vi.fn(async (_id: string, _src: string) => ({
      svg: "<svg data-test='diagram'></svg>",
    }));
    (window as unknown as { mermaid: unknown }).mermaid = {
      initialize: vi.fn(),
      render: renderFn,
    };
    const renderMd = await freshRender();
    const el = document.createElement("div");
    renderMd(el, "```mermaid\ngraph TD;A-->B\n```\n");
    await flush();
    expect(renderFn).toHaveBeenCalledOnce();
    expect(el.querySelector(".console-mermaid svg")).not.toBeNull();
    expect(el.querySelector("pre code.language-mermaid")).toBeNull();
  });

  test("falls back to source when mermaid rejects the diagram", async () => {
    (window as unknown as { mermaid: unknown }).mermaid = {
      initialize: vi.fn(),
      render: vi.fn(async () => {
        throw new Error("parse error");
      }),
    };
    const renderMd = await freshRender();
    const el = document.createElement("div");
    renderMd(el, "```mermaid\nnot a real diagram\n```\n");
    await flush();
    expect(el.querySelector(".console-mermaid")).toBeNull();
    expect(el.querySelector("pre code.language-mermaid")?.textContent).toContain(
      "not a real diagram",
    );
  });
});
