// Change explainer — the document as one shareable HTML file.
//
// Serialises the document the pane already rendered rather than drawing
// it a second time: the prompt, viewer.css and the figure sanitiser
// already have to agree on the presentation vocabulary, and a separate
// export renderer would be a fourth party to that contract. What the
// file cannot carry is the viewer: a reference chip that opened a panel
// becomes a link to the hunk on the PR's diff (PR mode) or plain text
// naming it (local mode), and the skip box's button becomes an in-page
// anchor. Any other button left in the copy is a control this module
// has not been taught to translate, so it throws rather than shipping
// a dead one.
//
// The stylesheet is read off the live page's CSSOM — the theme
// variables, the explainer's rules and highlight.js's — so it cannot
// drift from what the pane shows. The file carries a CSP that permits
// its inline styles and nothing else: it is opened from a Slack
// download or a disk, and the prose derives from a diff that may be
// hostile.

//: Rules kept by selector: the explainer's own, and highlight.js's for
//: code blocks in the prose.
const _KEPT_SELECTOR = /explainer|hljs/;
//: The page frame — theme variables, box sizing, background and type.
const _FRAME_SELECTORS = new Set([":root", "*", "html, body", "body"]);

const _CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:";

/** The document as a complete HTML file.
 *
 *  Args:
 *    rendered: The document as `Explainer` rendered it for export —
 *      every section written, nothing pending.
 *    data: The review the document is about: the files its references
 *      address and the PR its links point into.
 *    page: The page whose stylesheets the file's CSS is read from.
 *
 *  Throws:
 *    Error: PR mode without a PR URL, or a button in `rendered` this
 *      module does not translate. */
async function build(rendered: HTMLElement, data: ViewerData, page: Document): Promise<string> {
  const out = document.implementation.createHTMLDocument(_title(data));
  const meta = out.createElement("meta");
  meta.setAttribute("http-equiv", "Content-Security-Policy");
  meta.setAttribute("content", _CSP);
  out.head.appendChild(meta);
  const charset = out.createElement("meta");
  charset.setAttribute("charset", "utf-8");
  out.head.prepend(charset);
  const viewport = out.createElement("meta");
  viewport.setAttribute("name", "viewport");
  viewport.setAttribute("content", "width=device-width, initial-scale=1");
  out.head.appendChild(viewport);
  const style = out.createElement("style");
  style.textContent = collectCss(page);
  out.head.appendChild(style);

  const copy = rendered.cloneNode(true) as HTMLElement;
  copy.prepend(_header(out, data));
  await _translateRefs(copy, data);
  _translateSkipLinks(copy);
  const stray = copy.querySelector("button");
  if (stray) throw new Error(`the document holds a control the export cannot carry: ${stray.outerHTML}`);
  out.body.appendChild(out.importNode(copy, true));
  return `<!DOCTYPE html>\n${out.documentElement.outerHTML}\n`;
}

/** The CSS text the exported document needs, read off `page`'s
 *  stylesheets.
 *
 *  A sheet loaded with a `media` attribute (highlight.js's light and dark
 *  themes) is wrapped in that query, so the file follows the reader's
 *  colour scheme the way the viewer does. Walked by owning element rather
 *  than `document.styleSheets`, because the element is where the
 *  attribute is read (jsdom's sheets carry neither `media` nor
 *  `ownerNode`). */
function collectCss(page: Document): string {
  const out: string[] = [];
  for (const el of Array.from(page.querySelectorAll<HTMLLinkElement | HTMLStyleElement>('link[rel="stylesheet"], style'))) {
    if (el.sheet === null) throw new Error(`a stylesheet that has not loaded: ${el.outerHTML}`);
    const kept = _keptRules(Array.from(el.sheet.cssRules));
    if (kept.length === 0) continue;
    const media = el.getAttribute("media");
    out.push(media ? `@media ${media} {\n${kept.join("\n")}\n}` : kept.join("\n"));
  }
  return out.join("\n");
}

function _keptRules(rules: CSSRule[]): string[] {
  const kept: string[] = [];
  for (const rule of rules) {
    if (rule instanceof CSSStyleRule) {
      if (_KEPT_SELECTOR.test(rule.selectorText) || _FRAME_SELECTORS.has(rule.selectorText)) {
        kept.push(rule.cssText);
      }
    } else if (rule instanceof CSSMediaRule) {
      const inner = _keptRules(Array.from(rule.cssRules));
      if (inner.length > 0) kept.push(`@media ${rule.media.mediaText} {\n${inner.join("\n")}\n}`);
    }
  }
  return kept;
}

/** The file name the download is offered under. */
function filename(data: ViewerData): string {
  const pr = data.pr;
  if (data.counterpart !== "github") return `${data.run_id}-overview.html`;
  if (pr.number === null || !pr.repo) throw new Error("a PR review with no PR number or repo to name the file by");
  return `${pr.repo.replace("/", "-")}-pr${pr.number}-overview.html`;
}

/** Offer `html` to the browser as a download. */
function save(html: string, name: string): void {
  const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function _title(data: ViewerData): string {
  return `${data.pr.title || "(untitled PR)"} — overview`;
}

/** What the viewer's top bar says, since the file has no top bar. */
function _header(out: Document, data: ViewerData): HTMLElement {
  const pr = data.pr;
  const head = out.createElement("header");
  head.className = "explainer-export-head";
  const h1 = out.createElement("h1");
  h1.textContent = pr.title || "(untitled PR)";
  head.appendChild(h1);
  const meta = out.createElement("p");
  if (pr.repo) meta.append(pr.repo, " · ");
  if (data.counterpart === "github") {
    const a = out.createElement("a");
    a.href = _prUrl(data);
    a.textContent = `#${pr.number}`;
    meta.append(a, " · ");
  }
  meta.append(`${pr.base_sha.slice(0, 8)}..${pr.head_sha.slice(0, 8)}`);
  head.appendChild(meta);
  return head;
}

function _prUrl(data: ViewerData): string {
  const url = data.pr.url.replace(/\/+$/, "");
  if (!url) throw new Error("a PR review with no PR URL: its references have nothing to link to");
  return url;
}

/** Replace every reference button with a link to its place on the PR's
 *  diff, or with text naming the place when there is no PR. Keeps the
 *  button's classes and label, so it reads as the chip it was. */
async function _translateRefs(root: HTMLElement, data: ViewerData): Promise<void> {
  const files: Record<string, FileBlock> = Object.create(null);
  for (const f of data.files) files[f.id] = f;
  const pr = data.counterpart === "github" ? _prUrl(data) : null;
  for (const btn of Array.from(root.querySelectorAll<HTMLElement>("button[data-ref-id]"))) {
    const id = btn.dataset.refId as string;
    const target = _resolve(id, files);
    const href = pr !== null && target !== null ? `${pr}/files#${await _diffAnchor(target)}` : null;
    const el = document.createElement(href !== null ? "a" : "span");
    el.className = btn.className;
    el.textContent = btn.textContent;
    el.dataset.refId = id;
    if (href !== null) el.setAttribute("href", href);
    el.title = target === null ? id : _describe(target);
    btn.replaceWith(el);
  }
}

interface ExportTarget {
  file: FileBlock;
  hunk: HunkBlock | null;
}

/** What a reference id addresses, or null for one no file answers to —
 *  a prose token the model invented. */
function _resolve(id: string, files: Record<string, FileBlock>): ExportTarget | null {
  const m = /^H(\d+)_(\d+)$/.exec(id);
  const file = files[m ? `F${m[1]}` : id];
  if (!file) return null;
  if (!m) return { file, hunk: null };
  const hunk = file.hunks.find((h) => h.id === id);
  return hunk ? { file, hunk } : null;
}

function _describe(t: ExportTarget): string {
  if (t.hunk === null) return t.file.path;
  const ordinal = t.file.hunks.indexOf(t.hunk) + 1;
  return `hunk ${ordinal} of ${t.file.hunks.length} in ${t.file.path}`;
}

/** GitHub's anchor for a file on a PR's diff — `diff-` and the SHA-256
 *  of its path — with the hunk's first line appended for a hunk: the
 *  new side's, or the old side's for a hunk that only deletes. */
async function _diffAnchor(t: ExportTarget): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(t.file.path));
  const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  if (t.hunk === null) return `diff-${hex}`;
  return t.hunk.new_count > 0 ? `diff-${hex}R${t.hunk.new_start}` : `diff-${hex}L${t.hunk.old_start}`;
}

/** The skip box's "Skip to …" button becomes a link to that section's
 *  heading, which the rendered document already carries as an id. */
function _translateSkipLinks(root: HTMLElement): void {
  for (const btn of Array.from(root.querySelectorAll<HTMLElement>("button.explainer-skip-link"))) {
    const a = document.createElement("a");
    a.className = btn.className;
    a.textContent = btn.textContent;
    a.setAttribute("href", `#${btn.dataset.targetHeading}`);
    btn.replaceWith(a);
  }
}

export const ExplainerExport = { build, collectCss, filename, save };
