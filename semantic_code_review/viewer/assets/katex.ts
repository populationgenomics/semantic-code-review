// Shared KaTeX loader + one-shot renderer.
//
// KaTeX (bundle + stylesheet + woff2 fonts) is vendored and lazy-loaded
// by `<script>` + `<link>` injection the first time rendered-markdown
// mode meets math, so it never enters viewer.js. Math renders from its
// TeX source via katex, off the sanitized-HTML path: katex output is
// generated from the source with `trust:false`, so it carries no author
// HTML and is injected directly (mirrors mermaid.ts — the sanitized-HTML
// path and the controlled-renderer path never mix, per ADR 0004).

interface KatexApi {
  renderToString(tex: string, opts: Record<string, unknown>): string;
}

// (tex, displayMode) → rendered HTML, so a repaint re-injects synchronously
// without a re-render. Keyed by a display-prefixed source.
const _cache = new Map<string, string>();
// Sources katex rejected (invalid TeX) — kept as their raw placeholder
// rather than re-attempted on every repaint.
const _failed = new Set<string>();
let _load: Promise<KatexApi | null> | null = null;
let _cssInjected = false;

function _key(tex: string, display: boolean): string {
  return (display ? "D" : "I") + tex;
}

// Inject the stylesheet once. Its `url(fonts/…)` requests resolve against
// /static/vendor/, where the woff2 fonts are served (server.py).
function _injectCss(): void {
  if (_cssInjected) return;
  _cssInjected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/vendor/katex.min.css";
  document.head.appendChild(link);
}

/** Inject the vendored katex bundle once and resolve to its global.
 *  Resolves null if the script can't load — the caller then leaves the
 *  raw TeX placeholder in place. Memoised, so callers never double-inject. */
function _loadApi(): Promise<KatexApi | null> {
  if (_load) return _load;
  _load = new Promise<KatexApi | null>((resolve) => {
    _injectCss();
    const existing = (window as unknown as { katex?: KatexApi }).katex;
    if (existing) {
      resolve(existing);
      return;
    }
    const s = document.createElement("script");
    s.src = "/static/vendor/katex.min.js";
    s.async = true;
    s.onload = () => resolve((window as unknown as { katex?: KatexApi }).katex ?? null);
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
  return _load;
}

/** Rendered HTML for a source already rendered, else null. Synchronous —
 *  lets a repaint re-inject known math with no flicker. */
function cached(tex: string, display: boolean): string | null {
  return _cache.get(_key(tex, display)) ?? null;
}

/** Whether katex already rejected this source as invalid TeX. */
function hasFailed(tex: string, display: boolean): boolean {
  return _failed.has(_key(tex, display));
}

/** Render one TeX fragment to HTML. Resolves null if katex can't load or
 *  rejects the source (the caller keeps the raw placeholder). Memoised by
 *  (source, displayMode). `throwOnError` is on so invalid TeX resolves
 *  null rather than baking a katex error node into the output. */
async function render(tex: string, display: boolean): Promise<string | null> {
  const key = _key(tex, display);
  const hit = _cache.get(key);
  if (hit != null) return hit;
  if (_failed.has(key)) return null;
  const k = await _loadApi();
  if (!k) return null;
  try {
    const html = k.renderToString(tex, {
      displayMode: display,
      throwOnError: true,
      output: "htmlAndMathml",
      trust: false,
    });
    _cache.set(key, html);
    return html;
  } catch {
    _failed.add(key);
    return null;
  }
}

export const Katex = { cached, hasFailed, render };
