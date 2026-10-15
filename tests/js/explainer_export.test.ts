// The change explainer as a downloaded file: references translated out
// of the viewer, the stylesheet read off the page, and the file's frame.

import { createHash } from "node:crypto";
import { describe, test, expect, beforeEach } from "vitest";
import { ExplainerExport } from "../../semantic_code_review/viewer/assets/explainer_export";

function hunk(id: string, overrides: Partial<HunkBlock> = {}): HunkBlock {
  return { id, old_start: 10, old_count: 3, new_start: 12, new_count: 4, ...overrides } as HunkBlock;
}

function data(overrides: Partial<ViewerData> = {}): ViewerData {
  return {
    version: "1",
    run_id: "local-main-abc12345",
    counterpart: "github",
    listening: false,
    pr: {
      title: "Thread a cursor through List",
      number: 7,
      repo: "acme/widgets",
      url: "https://github.com/acme/widgets/pull/7",
      base_sha: "base1234deadbeef",
      head_sha: "head5678deadbeef",
    } as PRBlock,
    smells_catalogue: {},
    files: [
      { id: "F0", path: "schema/api.proto", hunks: [hunk("H0_0"), hunk("H0_1", { new_count: 0, old_start: 40 })] } as FileBlock,
    ],
    groups: [],
    symbols: [],
    ...overrides,
  } as ViewerData;
}

function sha256(s: string): string {
  return createHash("sha256").update(s).digest("hex");
}

/** A rendered document holding `inner`, as `Explainer` hands it over. */
function rendered(inner: string): HTMLElement {
  const root = document.createElement("div");
  root.className = "explainer";
  root.innerHTML = inner;
  return root;
}

async function exported(inner: string, d: ViewerData = data()): Promise<Document> {
  const html = await ExplainerExport.build(rendered(inner), d, document);
  return new DOMParser().parseFromString(html, "text/html");
}

beforeEach(() => {
  document.head.innerHTML = "";
});

describe("references", () => {
  test("in a PR review, a hunk chip links to its first line on the PR's diff", async () => {
    const out = await exported('<p>see <button class="explainer-ref explainer-arrow" data-ref-id="H0_0">api.proto ↗</button></p>');
    const a = out.querySelector("a.explainer-ref") as HTMLAnchorElement;
    expect(a.getAttribute("href")).toBe(
      `https://github.com/acme/widgets/pull/7/files#diff-${sha256("schema/api.proto")}R12`,
    );
    expect(a.textContent).toBe("api.proto ↗");
    expect(a.classList.contains("explainer-arrow")).toBe(true);
    expect(a.title).toBe("hunk 1 of 2 in schema/api.proto");
  });

  test("a hunk that only deletes anchors on the old side", async () => {
    const out = await exported('<button class="explainer-ref" data-ref-id="H0_1">x</button>');
    expect(out.querySelector("a.explainer-ref")!.getAttribute("href")).toMatch(/L40$/);
  });

  test("a file reference anchors on the file", async () => {
    const out = await exported('<button class="explainer-ref" data-ref-id="F0">schema/api.proto</button>');
    expect(out.querySelector("a.explainer-ref")!.getAttribute("href")).toBe(
      `https://github.com/acme/widgets/pull/7/files#diff-${sha256("schema/api.proto")}`,
    );
  });

  test("in a local review a reference is text naming its place", async () => {
    const out = await exported(
      '<button class="explainer-ref" data-ref-id="H0_0">api.proto ↗</button>',
      data({ counterpart: "claude" }),
    );
    expect(out.querySelector("a.explainer-ref")).toBeNull();
    const span = out.querySelector("span.explainer-ref") as HTMLElement;
    expect(span.textContent).toBe("api.proto ↗");
    expect(span.title).toBe("hunk 1 of 2 in schema/api.proto");
  });

  test("an id no file answers to stays text, even with a PR", async () => {
    const out = await exported('<button class="explainer-ref" data-ref-id="F9">F9</button>');
    expect(out.querySelector("a.explainer-ref")).toBeNull();
    expect(out.querySelector("span.explainer-ref")!.textContent).toBe("F9");
  });

  test("a PR review with no PR URL refuses rather than shipping dead links", async () => {
    const d = data();
    d.pr.url = "";
    await expect(
      ExplainerExport.build(rendered('<button class="explainer-ref" data-ref-id="F0">x</button>'), d, document),
    ).rejects.toThrow(/no PR URL/);
  });
});

describe("controls", () => {
  test("the skip box's button becomes a link to the section heading", async () => {
    const out = await exported(
      '<button class="explainer-skip-link" data-target-heading="explainer-section-intuition">Skip to Intuition</button>',
    );
    const a = out.querySelector("a.explainer-skip-link") as HTMLAnchorElement;
    expect(a.getAttribute("href")).toBe("#explainer-section-intuition");
    expect(a.textContent).toBe("Skip to Intuition");
  });

  test("a control the export does not translate is refused, not shipped dead", async () => {
    await expect(
      ExplainerExport.build(rendered('<button class="explainer-generate">Try again</button>'), data(), document),
    ).rejects.toThrow(/cannot carry/);
  });
});

describe("the file", () => {
  test("carries a CSP that allows its inline styles and nothing else", async () => {
    const out = await exported("<p>x</p>");
    const csp = out.querySelector('meta[http-equiv="Content-Security-Policy"]') as HTMLMetaElement;
    expect(csp.content).toBe("default-src 'none'; style-src 'unsafe-inline'; img-src data:");
  });

  test("heads the document with what the viewer's top bar says", async () => {
    const out = await exported("<p>x</p>");
    const head = out.querySelector(".explainer > .explainer-export-head") as HTMLElement;
    expect(head.querySelector("h1")!.textContent).toBe("Thread a cursor through List");
    expect(head.querySelector("a")!.getAttribute("href")).toBe("https://github.com/acme/widgets/pull/7");
    expect(head.querySelector("p")!.textContent).toBe("acme/widgets · #7 · base1234..head5678");
    expect(out.title).toBe("Thread a cursor through List — overview");
  });

  test("is named for the PR, or for the run when there is none", () => {
    expect(ExplainerExport.filename(data())).toBe("acme-widgets-pr7-overview.html");
    expect(ExplainerExport.filename(data({ counterpart: "claude" }))).toBe("local-main-abc12345-overview.html");
  });
});

describe("stylesheet", () => {
  function sheet(css: string, media = ""): void {
    const style = document.createElement("style");
    if (media) style.setAttribute("media", media);
    style.textContent = css;
    document.head.appendChild(style);
  }

  test("keeps the theme, the explainer's rules and highlight.js's; drops the rest", () => {
    sheet(":root { --bg: #000; } .explainer { color: red; } .pr-bar { color: blue; } .hljs-keyword { color: green; }");
    const css = ExplainerExport.collectCss(document);
    expect(css).toContain("--bg");
    expect(css).toContain(".explainer");
    expect(css).toContain(".hljs-keyword");
    expect(css).not.toContain(".pr-bar");
  });

  test("keeps a media query around the rules it holds", () => {
    sheet("@media (prefers-color-scheme: light) { :root { --bg: #fff; } .sidebar { color: red; } }");
    const css = ExplainerExport.collectCss(document);
    expect(css).toMatch(/@media \(prefers-color-scheme: light\) \{\s*:root/);
    expect(css).not.toContain(".sidebar");
  });

  test("wraps a sheet loaded for one colour scheme in that scheme's query", () => {
    sheet(".hljs { color: white; }", "(prefers-color-scheme: dark)");
    expect(ExplainerExport.collectCss(document)).toMatch(/^@media \(prefers-color-scheme: dark\) \{\s*\.hljs/);
  });
});
