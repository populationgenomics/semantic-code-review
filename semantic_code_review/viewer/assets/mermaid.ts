// Shared mermaid loader + one-shot renderer.
//
// The vendored mermaid bundle is MB-class and rarely used, so it never
// enters viewer.js: it is lazy-loaded by `<script>` injection the first
// time any surface needs a diagram. Both the review console
// (console_render.ts) and rendered-mode markdown (rendered.ts) render
// through here, so the load-bearing security config (securityLevel:
// 'strict', htmlLabels:false) and the SVG sanitisation live in one place
// rather than drifting between two copies.

import DOMPurify from "dompurify";

interface MermaidApi {
  initialize(cfg: Record<string, unknown>): void;
  render(id: string, src: string): Promise<{ svg: string }>;
}

// Source → sanitised SVG, so a diagram renders once and re-injects
// synchronously on a later repaint (no flicker, no re-render churn).
const _svgCache = new Map<string, string>();
// Sources mermaid rejected as invalid — kept as raw source forever
// rather than re-attempted on every repaint.
const _failed = new Set<string>();
let _seq = 0;
let _load: Promise<MermaidApi | null> | null = null;

/** Mermaid's built-in theme matching the viewer's active colour scheme.
 *  Mirrors the CSS cascade in viewer.css: `:root` is dark by default and
 *  only a `prefers-color-scheme: light` match flips it to light. So pick
 *  the light ("default") theme only when that query matches; otherwise
 *  dark. Without this, mermaid renders its light theme over the (default)
 *  dark page — light elements on a transparent background, unreadable. */
function _activeTheme(): "dark" | "default" {
  const mq =
    typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: light)")
      : null;
  return mq && mq.matches ? "default" : "dark";
}

function _init(m: MermaidApi): void {
  // `htmlLabels: false` is load-bearing, not cosmetic. Mermaid's default
  // renders node labels as HTML inside an `<foreignObject>`; DOMPurify
  // strips `<foreignObject>` wholesale as an mXSS / namespace-confusion
  // vector, which silently removes every node label. Forcing SVG-native
  // `<text>`/`<tspan>` labels — which survive the sanitizer — fixes that
  // without widening the sanitizer to allow arbitrary HTML in untrusted
  // (repo-sourced) diagram output.
  m.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: _activeTheme(),
    htmlLabels: false,
    flowchart: { htmlLabels: false },
  });
}

/** Inject the vendored mermaid bundle once and resolve to its global.
 *  Resolves to null if the script can't load — the caller then leaves
 *  the raw source in place. Memoised, so callers never double-inject. */
function _loadApi(): Promise<MermaidApi | null> {
  if (_load) return _load;
  _load = new Promise<MermaidApi | null>((resolve) => {
    const existing = (window as unknown as { mermaid?: MermaidApi }).mermaid;
    if (existing) {
      _init(existing);
      resolve(existing);
      return;
    }
    const s = document.createElement("script");
    s.src = "/static/vendor/mermaid.min.js";
    s.async = true;
    s.onload = () => {
      const m = (window as unknown as { mermaid?: MermaidApi }).mermaid;
      if (m) {
        _init(m);
        resolve(m);
      } else {
        resolve(null);
      }
    };
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
  return _load;
}

/** Sanitised SVG for a source already rendered, else null. Synchronous —
 *  lets a repaint re-inject a known diagram with no flicker and no fresh
 *  async render. */
function cachedSvg(src: string): string | null {
  return _svgCache.get(src) ?? null;
}

/** Whether mermaid already rejected this source as invalid. */
function hasFailed(src: string): boolean {
  return _failed.has(src);
}

/** Render one diagram to sanitised SVG. Resolves null if mermaid can't
 *  load or rejects the source (the caller keeps the raw fence). Memoised
 *  by source, so repeated calls for the same diagram render once. The SVG
 *  is sanitised here — the diagram text is untrusted (repo-sourced), so
 *  callers inject the returned string directly without re-sanitising. */
async function renderToSvg(src: string): Promise<string | null> {
  const cached = _svgCache.get(src);
  if (cached) return cached;
  if (_failed.has(src)) return null;
  const m = await _loadApi();
  if (!m) return null;
  try {
    const { svg } = await m.render("scr-mermaid-" + _seq++, src);
    const clean = DOMPurify.sanitize(svg);
    _svgCache.set(src, clean);
    return clean;
  } catch {
    _failed.add(src);
    return null;
  }
}

export const Mermaid = { cachedSvg, hasFailed, renderToSvg };
