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
// mermaid is MB-class and rarely used, so it is not bundled into
// viewer.js: it is lazy-loaded by `<script>` injection the first time an
// answer *completes* a `mermaid` fence. Diagrams render with
// `securityLevel: 'strict'`; an invalid diagram (or a mermaid that
// fails to load) degrades to its raw source block — never an error box.

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

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

// --- mermaid -------------------------------------------------------------

interface MermaidApi {
  initialize(cfg: Record<string, unknown>): void;
  render(id: string, src: string): Promise<{ svg: string }>;
}

// Source → rendered SVG, so a diagram is rendered once and re-injected
// synchronously on later deltas (no flicker, no re-render churn).
const mermaidSvgCache = new Map<string, string>();
// Sources that mermaid rejected as invalid — kept as raw source forever
// rather than re-attempted on every delta.
const mermaidFailed = new Set<string>();
let mermaidSeq = 0;
let mermaidLoad: Promise<MermaidApi | null> | null = null;

function initMermaid(m: MermaidApi): void {
  // `htmlLabels: false` is load-bearing, not cosmetic. Mermaid's default
  // renders node labels as HTML inside an `<foreignObject>`; DOMPurify
  // (both our defence-in-depth pass in swapInSvg and its default policy)
  // strips `<foreignObject>` wholesale as an mXSS / namespace-confusion
  // vector, which silently removes every node label. Forcing SVG-native
  // `<text>`/`<tspan>` labels — which survive the sanitizer — fixes that
  // without widening the sanitizer to allow arbitrary HTML in untrusted
  // (repo-sourced) diagram output.
  m.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    htmlLabels: false,
    flowchart: { htmlLabels: false },
  });
}

/** Inject the vendored mermaid bundle once and resolve to its global.
 *  Resolves to null if the script can't load — the caller then leaves
 *  the raw source in place. Memoised, so deltas never double-inject. */
function loadMermaid(): Promise<MermaidApi | null> {
  if (mermaidLoad) return mermaidLoad;
  mermaidLoad = new Promise<MermaidApi | null>((resolve) => {
    const existing = (window as unknown as { mermaid?: MermaidApi }).mermaid;
    if (existing) {
      initMermaid(existing);
      resolve(existing);
      return;
    }
    const s = document.createElement("script");
    s.src = "/static/vendor/mermaid.min.js";
    s.async = true;
    s.onload = () => {
      const m = (window as unknown as { mermaid?: MermaidApi }).mermaid;
      if (m) {
        initMermaid(m);
        resolve(m);
      } else {
        resolve(null);
      }
    };
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
  return mermaidLoad;
}

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
  // svg is mermaid's strict-mode output; sanitise once more as defence
  // in depth since the diagram text itself is untrusted model output.
  fig.innerHTML = DOMPurify.sanitize(svg);
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
    if (mermaidFailed.has(src)) return; // invalid → leave source
    const pre = code.parentElement;
    if (!pre) return;
    const cached = mermaidSvgCache.get(src);
    if (cached) {
      swapInSvg(pre, cached);
      return;
    }
    void renderOne(src, target, markdown);
  });
}

async function renderOne(src: string, target: HTMLElement, markdown: string): Promise<void> {
  const m = await loadMermaid();
  if (!m) return; // couldn't load mermaid → leave source (may retry on reload)
  try {
    const { svg } = await m.render("scr-mermaid-" + mermaidSeq++, src);
    mermaidSvgCache.set(src, svg);
  } catch {
    mermaidFailed.add(src);
  }
  // A newer delta may have replaced target.innerHTML while we awaited;
  // re-paint the live DOM so the freshly-cached (or now-failed) block
  // settles against whatever is on screen now.
  paintMermaid(target, markdown);
}
