// Review-console answer rendering — markdown + inline mermaid.
//
// Slice 3 (ADR 0002): console answers are markdown, rendered fully
// client-side. The answer buffer is re-rendered on every streamed delta
// with markdown-it (raw HTML disabled) and the output run through
// DOMPurify — model output is repo-sourced, so a malicious repo can try
// to prompt-inject `<script>` / `<img onerror>`, and localhost is not a
// safe boundary. Non-mermaid code fences are highlighted with the
// vendored hljs (the same global the diff cells use).
//
// mermaid rendering (lazy load, strict mode, SVG sanitisation) lives in
// the shared mermaid.ts module — the console and rendered-mode markdown
// both render through it. This module keeps only the console-specific
// streaming glue: scanning the answer buffer for closed fences and
// swapping each into its diagram as the source completes. An invalid
// diagram (or a mermaid that fails to load) degrades to its raw source
// block — never an error box.

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import { Mermaid } from "./mermaid";

// hljs is loaded as a classic script (vendored) and exposed as a global,
// exactly as render.ts consumes it for diff cells.
interface Hljs {
  highlight(code: string, opts: { language: string; ignoreIllegals: boolean }): { value: string };
  getLanguage(name: string): unknown;
}
function hljs(): Hljs | undefined {
  return (window as unknown as { hljs?: Hljs }).hljs;
}

const md: MarkdownIt = new MarkdownIt({
  html: false, // never interpret raw HTML in model output
  linkify: true,
  highlight(code: string, lang: string): string {
    // mermaid and unknown languages fall back to markdown-it's own
    // escaping (`<pre><code class="language-…">`). For mermaid this
    // `<pre>` is exactly the fallback we keep when no diagram renders.
    if (!lang || lang.toLowerCase() === "mermaid") return "";
    const h = hljs();
    if (h && h.getLanguage(lang)) {
      try {
        const out = h.highlight(code, { language: lang, ignoreIllegals: true }).value;
        return `<pre class="hljs"><code class="language-${lang}">${out}</code></pre>`;
      } catch {
        /* fall through to markdown-it's default escaping */
      }
    }
    return "";
  },
});

/** Render the accumulated answer markdown into `target`, then paint any
 *  completed mermaid fences. Safe to call on every delta — it fully
 *  replaces `target`'s content each time. */
export function renderConsoleMarkdown(target: HTMLElement, markdown: string): void {
  target.innerHTML = DOMPurify.sanitize(md.render(markdown));
  paintMermaid(target, markdown);
}

// --- mermaid streaming glue ----------------------------------------------

/** A fence per mermaid block in source order, flagged closed/unclosed.
 *  Mirrors markdown-it's fence scan closely enough to keep 1:1 ordering
 *  with the rendered `code.language-mermaid` blocks: every fence (any
 *  language) is consumed so sequential fences stay aligned, but only the
 *  mermaid ones are recorded. An unclosed fence (still streaming) is
 *  left as source until its closer arrives. */
export function scanMermaidFences(src: string): { closed: boolean }[] {
  const lines = src.split("\n");
  const blocks: { closed: boolean }[] = [];
  let i = 0;
  while (i < lines.length) {
    const open = /^[ \t]*(`{3,}|~{3,})\s*([^\s`~]*)/.exec(lines[i]);
    if (!open) {
      i++;
      continue;
    }
    const marker = open[1][0];
    const len = open[1].length;
    const isMermaid = open[2].toLowerCase() === "mermaid";
    let j = i + 1;
    let closed = false;
    for (; j < lines.length; j++) {
      const close = /^[ \t]*(`{3,}|~{3,})\s*$/.exec(lines[j]);
      if (close && close[1][0] === marker && close[1].length >= len) {
        closed = true;
        break;
      }
    }
    if (isMermaid) blocks.push({ closed });
    i = closed ? j + 1 : lines.length;
  }
  return blocks;
}

function swapInSvg(pre: HTMLElement, svg: string): void {
  const fig = document.createElement("div");
  fig.className = "console-mermaid";
  // svg is already sanitised by the shared mermaid module.
  fig.innerHTML = svg;
  pre.replaceWith(fig);
}

function paintMermaid(target: HTMLElement, markdown: string): void {
  const fences = scanMermaidFences(markdown);
  if (fences.length === 0) return;
  const codes = target.querySelectorAll<HTMLElement>("pre > code.language-mermaid");
  codes.forEach((code, idx) => {
    const fence = fences[idx];
    if (!fence || !fence.closed) return; // still streaming → leave source
    const src = (code.textContent || "").replace(/\n$/, "");
    if (Mermaid.hasFailed(src)) return; // invalid → leave source
    const pre = code.parentElement;
    if (!pre) return;
    const cached = Mermaid.cachedSvg(src);
    if (cached) {
      swapInSvg(pre, cached);
      return;
    }
    void renderOne(src, target, markdown);
  });
}

async function renderOne(src: string, target: HTMLElement, markdown: string): Promise<void> {
  await Mermaid.renderToSvg(src);
  // A newer delta may have replaced target.innerHTML while we awaited;
  // re-paint the live DOM so the freshly-cached (or now-failed) block
  // settles against whatever is on screen now.
  paintMermaid(target, markdown);
}
