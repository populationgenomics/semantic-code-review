// Change explainer — the document, and the overview-mode main pane.
//
// Owns the document (fetching it, asking the server to generate it,
// taking it off the SSE bus when another tab paid for it), the pane
// that renders it, and which section the sidebar tree has selected.
// It does NOT own the mode bit: render.ts does, because the mode is a
// property of the viewer's main pane and the mode button lives on the
// same control strip as the collapse level.
//
// Overview mode is orthogonal to the collapse level (ADR 0007), not a
// fifth value of it. It hides nothing — it replaces the pane — so it
// has no span set, and leaving it restores the reviewer's zoom and
// hand-set folds without a latch.
//
// Prose arrives one section at a time, on the reviewer's press: a
// section nobody opens is never paid for. Bodies render through the
// shared markdown-it (`html: false`) + DOMPurify path the console uses
// — model prose derives from a diff that may be hostile, so it never
// reaches innerHTML unsanitised. Inline `[F3]` / `[H3_1]` tokens become
// chips after sanitisation, built as DOM nodes rather than injected
// markup. Figures are a structured slot rendered by
// explainer_figure.ts, which reduces them to the closed vocabulary in
// docs/explainer-presentation.md.

import { renderMarkdown } from "./console_render";
import { ExplainerFigures } from "./explainer_figure";

let _endpoint = "";
let _doc: ExplainerDocument | null = null;
let _activeSectionId: string | null = null;
let _lsKey = "scr-explainer-section:local";
let _phase: "absent" | "loading" | "ready" | "error" = "absent";
let _error = "";
// Set once the overview SSE event lands (or at boot on a page that
// opened after augmentation): the skeleton is seeded with the overview,
// so pressing the button before then can only 409.
let _ready = false;
// Repaint hook — boot points this at Render.render. Injected rather
// than imported so this module doesn't take a cyclic dependency on the
// renderer that hosts its pane.
let _onChange: (() => void) | null = null;
// Called with a file's viewer id when a Map row is clicked. boot wires
// it to "leave overview mode and reveal that file"; the reveal itself
// is render.ts's business.
let _onOpenFile: ((fileId: string) => void) | null = null;
// Same, for an inline hunk chip. A hunk reference means "read this
// hunk", so the reveal unfolds it — unlike a file reference, which only
// scrolls.
let _onOpenHunk: ((hunkId: string) => void) | null = null;

// Per-section generation state, keyed by section id. Distinct from the
// section's own `state`: this is what THIS tab is doing about it, and
// it is dropped on every fresh document.
type SectionPhase =
  | { kind: "error"; message: string }
  | { kind: "waiting"; annotated: number; total: number };
let _sectionPhase: Record<string, SectionPhase> = Object.create(null);
// One section at a time. The server serialises explainer passes anyway
// (a concurrent POST is a 409), so the queue is what turns "opened three
// sections quickly" into three writes rather than two rejections.
let _writing: string | null = null;
const _queue: string[] = [];

interface ExplainerInitOptions {
  onChange?: () => void;
  onOpenFile?: (fileId: string) => void;
  onOpenHunk?: (hunkId: string) => void;
}

function init(endpoint: string, data: ViewerData, opts: ExplainerInitOptions = {}): void {
  _endpoint = endpoint;
  _doc = null;
  _phase = "absent";
  _error = "";
  _activeSectionId = null;
  // A page that boots after augmentation finished can press the button
  // straight away; one that boots mid-pass waits for `overview`.
  _ready = !data.pending;
  _sectionPhase = Object.create(null);
  _writing = null;
  _queue.length = 0;
  if (opts.onChange) _onChange = opts.onChange;
  if (opts.onOpenFile) _onOpenFile = opts.onOpenFile;
  if (opts.onOpenHunk) _onOpenHunk = opts.onOpenHunk;
  _lsKey = "scr-explainer-section:" + (data.pr && data.pr.head_sha ? data.pr.head_sha : "local");
  // Deliberately a different key from the sidebar's `scr-active-group:`
  // pill. Sharing one would make entering overview mode overwrite the
  // reviewer's diff-mode filter, which they expect to find on return.
  try {
    const saved = localStorage.getItem(_lsKey);
    if (saved && saved.startsWith("explainer:")) _activeSectionId = saved.slice("explainer:".length);
  } catch (_) { /* localStorage may be unavailable */ }
}

/** True once the skeleton's inputs exist server-side. The mode button
 *  stays disabled until then — the route would 409. */
function isReady(): boolean {
  return _ready;
}

function markReady(): void {
  _ready = true;
}

function hasDocument(): boolean {
  return _doc !== null;
}

/** Fetch any document the server already holds. Safe to call at boot:
 *  a 404 is the ordinary "nobody has pressed the button" answer and
 *  leaves the pane on its call-to-action. */
async function load(): Promise<void> {
  try {
    const r = await fetch(`${_endpoint}/explainer`, { cache: "no-store" });
    if (r.status === 404 || r.status === 409) return;
    if (!r.ok) throw new Error(`GET /explainer -> ${r.status}`);
    _adopt((await r.json()) as ExplainerDocument);
  } catch (e) {
    // A failed pre-fetch is not worth a visible error: the button still
    // works and generating produces the document anyway.
    console.warn("explainer: could not load an existing document", e);
  }
}

/** Ask the server to generate the skeleton. No-op when one is already
 *  in hand or a generation is in flight — the call is not free. */
async function generate(): Promise<void> {
  if (_doc !== null || _phase === "loading") return;
  _phase = "loading";
  _error = "";
  _onChange?.();
  try {
    const r = await fetch(`${_endpoint}/explainer/skeleton`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(payload.error || `POST /explainer/skeleton -> ${r.status}`);
    _adopt(payload as ExplainerDocument);
  } catch (e) {
    _phase = "error";
    _error = String(e instanceof Error ? e.message : e);
    _onChange?.();
  }
}

/** Ask the server to write one section's prose. No-op for a section
 *  that is already written, or already queued here: the call is not
 *  free and a second press must not buy the same paragraph twice. */
function generateSection(id: string): void {
  const section = _findSection(id);
  if (!section || section.kind === "map" || section.state === "ready") return;
  if (_writing === id || _queue.indexOf(id) !== -1) return;
  _queue.push(id);
  delete _sectionPhase[id];
  _onChange?.();
  void _drainQueue();
}

//: Backoff before retrying a section the server was too busy to write.
//: One pass takes tens of seconds, so polling faster buys nothing.
const _RETRY_MS = 4000;

async function _drainQueue(): Promise<void> {
  if (_writing !== null) return;
  while (_queue.length > 0) {
    const id = _queue.shift() as string;
    _writing = id;
    _onChange?.();
    await _writeSection(id);
    _writing = null;
    _onChange?.();
  }
}

async function _writeSection(id: string): Promise<void> {
  try {
    const r = await fetch(`${_endpoint}/explainer/section/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await r.json().catch(() => ({}));
    if (r.status === 409 && typeof payload.total === "number") {
      // Not an error: the hunks this section is anchored to aren't all
      // annotated, and prose over the gaps reads exactly as fluently as
      // prose over the whole thing.
      _sectionPhase[id] = { kind: "waiting", annotated: payload.annotated || 0, total: payload.total };
      return;
    }
    if (r.status === 409 && payload.retry) {
      // Another pass holds the server's slot — one per server, shared by
      // the skeleton and every section. It clears on its own, so this is
      // a wait, not a failure: re-queue rather than making the reviewer
      // press again for a condition that resolves itself.
      delete _sectionPhase[id];
      _queue.push(id);   // renders as "Queued behind another section…"
      window.setTimeout(() => void _drainQueue(), _RETRY_MS);
      return;
    }
    if (!r.ok) throw new Error(payload.error || `POST /explainer/section/${id} -> ${r.status}`);
    _adopt(payload as ExplainerDocument, { keepPhases: true });
  } catch (e) {
    _sectionPhase[id] = { kind: "error", message: String(e instanceof Error ? e.message : e) };
  }
}

/** Take a document off the SSE bus. Another tab pressed the button; the
 *  spend is already made, so this tab shows the result rather than
 *  offering to pay for it again. */
function onEvent(payload: SseExplainerEvent): void {
  _adopt(payload, { keepPhases: true });
}

function _adopt(doc: ExplainerDocument, opts: { keepPhases?: boolean } = {}): void {
  // Loud but not fatal: a payload this shape is a server bug, and
  // throwing out of a click handler would wedge the pane with no way
  // back. Surface it where the reviewer can see it instead.
  if (!doc || !Array.isArray(doc.sections)) {
    _phase = "error";
    _error = "the server returned something that is not a document";
    _onChange?.();
    return;
  }
  _doc = doc;
  _phase = "ready";
  _error = "";
  // A whole new document (a regenerate, or a first load) invalidates
  // every per-section note; a section write leaves its neighbours'
  // alone.
  if (!opts.keepPhases) _sectionPhase = Object.create(null);
  for (const s of _walk(doc.sections)) {
    if (s.state === "ready") delete _sectionPhase[s.id];
  }
  if (_activeSectionId === null || !_findSection(_activeSectionId)) {
    _activeSectionId = doc.sections.length > 0 ? doc.sections[doc.sections.length - 1].id : null;
  }
  _onChange?.();
}

function sections(): ExplainerSection[] {
  return _doc ? _doc.sections : [];
}

/** Every section and subsection, depth-first in document order. */
function _walk(nodes: ExplainerSection[]): ExplainerSection[] {
  const out: ExplainerSection[] = [];
  for (const s of nodes) {
    out.push(s);
    out.push(..._walk(s.subsections || []));
  }
  return out;
}

function _findSection(id: string): ExplainerSection | null {
  if (!_doc) return null;
  for (const s of _walk(_doc.sections)) if (s.id === id) return s;
  return null;
}

function activeSectionId(): string | null {
  return _activeSectionId;
}

function setActiveSection(id: string): void {
  _activeSectionId = id;
  try {
    localStorage.setItem(_lsKey, `explainer:${id}`);
  } catch (_) { /* ignore */ }
  // Opening a section that has never been written is the press that
  // pays for it. Nothing generates on load: the pane stacks every
  // section, so writing on render would buy all three unasked.
  const section = _findSection(id);
  if (section && section.state === "pending") generateSection(id);
  const heading = document.getElementById(_headingId(id));
  if (heading) heading.scrollIntoView({ block: "start" });
  _onChange?.();
}

// --- Pane -----------------------------------------------------------------

/** The whole overview-mode main pane. render.ts appends this in place
 *  of the file list; the diff renderer is untouched. */
function renderPane(): HTMLElement {
  const pane = _el("div", "explainer");
  if (_phase === "loading") {
    pane.appendChild(_el("p", "explainer-status", "Working out a reading order…"));
    return pane;
  }
  if (_phase === "error") {
    const p = _el("p", "explainer-status explainer-error", `Could not generate the document: ${_error}`);
    pane.appendChild(p);
    pane.appendChild(_generateButton("Try again"));
    return pane;
  }
  if (_doc === null) {
    pane.appendChild(
      _el(
        "p",
        "explainer-status",
        "No reading guide yet. Generating one costs a single model call and"
        + " produces the order to read this change in.",
      ),
    );
    pane.appendChild(_generateButton("Generate reading guide"));
    return pane;
  }
  return _renderDocument(_doc, pane);
}

function _renderDocument(doc: ExplainerDocument, pane: HTMLElement): HTMLElement {
  if (doc.verdict_note) {
    pane.appendChild(_el("p", "explainer-lede", doc.verdict_note));
  }
  if (doc.verdict === "not_warranted") {
    // A real answer, not an empty state: the note above IS the document,
    // and saying so beats three headings offering prose nobody needs.
    pane.appendChild(
      _el("p", "explainer-status", "This change reads faster directly than through a document about it."),
    );
  }
  for (const section of doc.sections) pane.appendChild(_renderSection(section));
  const footer = _renderFooter(doc);
  if (footer) pane.appendChild(footer);
  return pane;
}

function _renderSection(section: ExplainerSection): HTMLElement {
  const el = _el("section", "explainer-section");
  el.dataset.sectionId = section.id;
  const heading = _el("h2", "explainer-section-title", section.title);
  heading.id = _headingId(section.id);
  el.appendChild(heading);
  if (section.kind === "map") {
    el.appendChild(_renderMap(section));
    return el;
  }
  if (section.skip_box) el.appendChild(_renderSkipBox(section.skip_box));
  if (section.terms && section.terms.length > 0) el.appendChild(_renderTerms(section.terms));
  for (const node of _proseNodes(section)) el.appendChild(node);
  // Figures sit after the prose rather than inside it: they are a
  // structured slot, not markup the model embedded in its markdown.
  for (const figure of section.figures || []) el.appendChild(ExplainerFigures.renderFigure(figure));
  for (const sub of section.subsections || []) el.appendChild(_renderSubsection(sub));
  const sources = _renderSources(section);
  if (sources) el.appendChild(sources);
  return el;
}

/** "If you already know X, jump to Y." Background is written in two
 *  layers and this is the way past the first one. */
function _renderSkipBox(box: ExplainerSkipBox): HTMLElement {
  const el = _el("div", "explainer-skip");
  el.appendChild(_renderBody(box.body));
  const target = _findSection(box.target_section_id);
  const btn = _el("button", "explainer-skip-link", `Skip to ${target ? target.title : box.target_section_id}`);
  btn.addEventListener("click", () => setActiveSection(box.target_section_id));
  el.appendChild(btn);
  return el;
}

/** Names the section introduces, as a definition list — cheaper to read
 *  than a paragraph each, and it keeps them out of the prose. */
function _renderTerms(terms: ExplainerTerm[]): HTMLElement {
  const dl = _el("dl", "explainer-terms");
  for (const t of terms) {
    dl.appendChild(_el("dt", null, t.term));
    const dd = _el("dd", null);
    dd.appendChild(_renderBody(t.definition));
    dl.appendChild(dd);
  }
  return dl;
}

/** What the section's pass actually opened. Recorded from the tool
 *  surface, so a section citing nothing is visibly one that read
 *  nothing — which is legible without judging the prose. */
function _renderSources(section: ExplainerSection): HTMLElement | null {
  if (section.state !== "ready" || section.kind !== "background") return null;
  const sources = section.sources || [];
  const text = sources.length > 0
    ? `Read while writing this: ${sources.join(", ")}`
    : "Written without reading any file.";
  return _el("p", "explainer-sources", text);
}

/** A model-chosen part of the walkthrough. It carries its own
 *  references and its own sidebar node, but it is never generated on
 *  its own — its parent's pass wrote it. */
function _renderSubsection(section: ExplainerSection): HTMLElement {
  const el = _el("section", "explainer-subsection");
  el.dataset.sectionId = section.id;
  const heading = _el("h3", "explainer-subsection-title", section.title);
  heading.id = _headingId(section.id);
  el.appendChild(heading);
  if (section.body) el.appendChild(_renderBody(section.body));
  return el;
}

/** The body of one prose section: what it says, or what this tab is
 *  doing about the fact that it does not say anything yet. */
function _proseNodes(section: ExplainerSection): HTMLElement[] {
  if (_writing === section.id) {
    return [_el("p", "explainer-status", "Writing this section…")];
  }
  if (_queue.indexOf(section.id) !== -1) {
    return [_el("p", "explainer-status", "Queued behind another section…")];
  }
  const phase = _sectionPhase[section.id];
  if (phase && phase.kind === "waiting") {
    return [
      _el(
        "p",
        "explainer-status",
        `${phase.annotated} of ${phase.total} hunks under this section are annotated.`
        + " Writing now would narrate over the gaps.",
      ),
      _sectionButton(section.id, "Check again"),
    ];
  }
  if (phase && phase.kind === "error") {
    return [
      _el("p", "explainer-status explainer-error", `Could not write this section: ${phase.message}`),
      _sectionButton(section.id, "Try again"),
    ];
  }
  if (section.state === "failed") {
    return [
      _el("p", "explainer-status explainer-error", "This section could not be written."),
      _sectionButton(section.id, "Try again"),
    ];
  }
  if (section.state === "pending") {
    return [
      _el("p", "explainer-status", "Not written yet."),
      _sectionButton(section.id, "Write this section"),
    ];
  }
  return section.body ? [_renderBody(section.body)] : [];
}

function _sectionButton(id: string, label: string): HTMLElement {
  const btn = _el("button", "explainer-generate", label);
  btn.addEventListener("click", () => generateSection(id));
  return btn;
}

/** Markdown → sanitised HTML through the shared console pipeline, then
 *  the reference tokens the prompt asks for swapped into chips. */
function _renderBody(markdown: string): HTMLElement {
  const body = _el("div", "explainer-body");
  renderMarkdown(body, markdown);
  _calloutify(body);
  _chipify(body);
  return body;
}

//: GitHub's alert convention, which is a blockquote whose first line is
//: a bracketed kind. Markdown has no callout of its own, and this is the
//: spelling a model already knows. Only these three map; the prompt asks
//: for no others, and an unrecognised kind stays an ordinary blockquote
//: rather than being silently restyled as something it did not ask for.
const _ALERT_KINDS: Record<string, { cls: string; label: string }> = {
  NOTE: { cls: "", label: "Concept" },
  WARNING: { cls: "explainer-callout-edge", label: "Edge case" },
  TIP: { cls: "explainer-callout-aside", label: "Aside" },
};
const _ALERT_HEAD = /^\s*\[!([A-Z]+)\]\s*/;

/** Turn GitHub-style alert blockquotes into callouts.
 *
 *  Runs over the sanitised DOM for the same reason `_chipify` does: the
 *  prose derives from a diff that may be hostile, so nothing goes back
 *  through innerHTML. A callout placed inline in the prose is why this
 *  is markdown rather than a schema field — a detached list of callouts
 *  on the section has no position, and position is most of what a
 *  callout is for. */
function _calloutify(root: HTMLElement): void {
  for (const quote of Array.from(root.querySelectorAll("blockquote"))) {
    const first = quote.firstElementChild;
    if (!first) continue;
    const m = _ALERT_HEAD.exec(first.textContent || "");
    if (!m) continue;
    const kind = _ALERT_KINDS[m[1]];
    if (!kind) continue;
    // Drop the marker from the text it was read off, leaving the prose.
    first.textContent = (first.textContent || "").replace(_ALERT_HEAD, "");
    const box = _el("div", `explainer-callout${kind.cls ? " " + kind.cls : ""}`);
    box.appendChild(_el("div", "explainer-callout-title", kind.label));
    const inner = _el("div", "explainer-callout-body");
    while (quote.firstChild) inner.appendChild(quote.firstChild);
    box.appendChild(inner);
    quote.parentNode?.replaceChild(box, quote);
  }
}

function _renderMap(section: ExplainerSection): HTMLElement {
  if (section.map_rows.length === 0) {
    return _el("p", "explainer-status", "No reading order — the files stand on their own.");
  }
  const table = _el("table", "explainer-map");
  const head = _el("tr", null);
  head.appendChild(_el("th", "explainer-map-read", "read"));
  head.appendChild(_el("th", "explainer-map-why", "because"));
  table.appendChild(head);
  for (const row of section.map_rows) {
    table.appendChild(_renderMapRow(row));
  }
  return table;
}

function _renderMapRow(row: ExplainerMapRow): HTMLElement {
  const tr = _el("tr", "explainer-map-row");
  const readCell = _el("td", "explainer-map-read");
  const btn = _el("button", "explainer-ref", _fileLabel(row.ref.id));
  btn.dataset.refId = row.ref.id;
  btn.title = "Open this file in the diff";
  btn.addEventListener("click", () => _onOpenFile?.(row.ref.id));
  readCell.appendChild(btn);
  tr.appendChild(readCell);
  tr.appendChild(_el("td", "explainer-map-why", row.why));
  return tr;
}

//: `[F3]` / `[H3_1]` — the inline reference form the section prompt
//: asks for. Anchored on the bracket so a bare `F3` in prose is left
//: alone.
const _REF_TOKEN = /\[(F\d+|H\d+_\d+)\]/g;

/** Swap inline reference tokens in rendered prose for clickable chips.
 *
 *  Runs over the sanitised DOM, building elements rather than splicing
 *  HTML — the prose derives from a diff that may be hostile, and a
 *  second innerHTML pass would hand it a way back in. Code spans and
 *  blocks are skipped: a `[F3]` inside a snippet is the snippet's. */
function _chipify(root: HTMLElement): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let n = walker.nextNode(); n !== null; n = walker.nextNode()) texts.push(n as Text);
  for (const node of texts) {
    if (_inCode(node)) continue;
    const text = node.nodeValue || "";
    if (!/\[(F\d+|H\d+_\d+)\]/.test(text)) continue;
    const frag = document.createDocumentFragment();
    let last = 0;
    _REF_TOKEN.lastIndex = 0;
    for (let m = _REF_TOKEN.exec(text); m !== null; m = _REF_TOKEN.exec(text)) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(_refChip(m[1]));
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode?.replaceChild(frag, node);
  }
}

function _inCode(node: Node): boolean {
  for (let p = node.parentElement; p !== null; p = p.parentElement) {
    const tag = p.tagName.toLowerCase();
    if (tag === "code" || tag === "pre") return true;
  }
  return false;
}

/** An inline reference is a link-out arrow, not a label.
 *
 *  The prose has almost always just named the thing it is citing
 *  ("models/workbench.proto [F10] holds the message shapes"), so a chip
 *  carrying the full repo path repeats it at slab width and breaks the
 *  line. The arrow attaches to the phrase before it and the target
 *  moves to the tooltip, where it costs nothing to read past. */
function _refChip(id: string): HTMLElement {
  const isFile = id.charAt(0) === "F";
  const target = isFile ? _fileLabel(id) : _hunkLabel(id);
  const btn = _el("button", "explainer-ref explainer-arrow", "\u2197");
  btn.dataset.refId = id;
  btn.title = isFile ? `Open ${target} in the diff` : `Open ${target} in the diff`;
  btn.setAttribute("aria-label", btn.title);
  btn.addEventListener("click", () => {
    if (isFile) _onOpenFile?.(id);
    else _onOpenHunk?.(id);
  });
  return btn;
}

/** Coverage, the dropped-reference count, and the toy-data notice —
 *  rendered rather than logged. References thinning out unnoticed is
 *  exactly the failure the count exists to catch, and a worked example
 *  read as real data is the failure the notice exists to catch. */
function _renderFooter(doc: ExplainerDocument): HTMLElement | null {
  const bits: string[] = [];
  const covered = new Set<string>();
  for (const s of _walk(doc.sections)) for (const r of s.refs) covered.add(`${r.kind}:${r.id}`);
  if (covered.size > 0) bits.push(`${covered.size} references`);
  if (doc.dropped_refs > 0) {
    bits.push(`${doc.dropped_refs} dropped (addressed nothing in this diff)`);
  }
  if (bits.length === 0 && !doc.toy_data) return null;
  const footer = _el("div", "explainer-footer");
  if (bits.length > 0) footer.appendChild(_el("p", "explainer-footnote", bits.join(" · ")));
  if (doc.toy_data) {
    // Its own line, not another stat: it qualifies what the reader just
    // read rather than measuring it.
    footer.appendChild(
      _el(
        "p",
        "explainer-toy-notice",
        "Identifiers, counts and values in the worked examples above are illustrative, not taken from this codebase.",
      ),
    );
  }
  return footer;
}

function _generateButton(label: string): HTMLElement {
  const btn = _el("button", "explainer-generate", label);
  btn.addEventListener("click", () => void generate());
  return btn;
}

/** The path behind `F<i>`, from the loaded diff. The document carries
 *  ids, not paths, because ids are what the viewer addresses nodes by. */
let _filePaths: Record<string, string> = Object.create(null);

function setFilePaths(data: ViewerData): void {
  _filePaths = Object.create(null);
  for (const f of data.files || []) _filePaths[f.id] = f.path;
}

function _fileLabel(fileId: string): string {
  return _filePaths[fileId] || fileId;
}

/** `H3_1` as `path:2` — the hunk's position within its file, 1-based,
 *  because "the second hunk of api.proto" is what a reader can act on
 *  and `H3_1` is not. */
function _hunkLabel(hunkId: string): string {
  const m = /^H(\d+)_(\d+)$/.exec(hunkId);
  if (!m) return hunkId;
  const path = _filePaths[`F${m[1]}`];
  return path ? `${path}:${Number(m[2]) + 1}` : hunkId;
}

function _headingId(sectionId: string): string {
  return `explainer-section-${sectionId}`;
}

function _el(tag: string, className: string | null, text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

export const Explainer = {
  init,
  isReady,
  markReady,
  hasDocument,
  load,
  generate,
  generateSection,
  onEvent,
  sections,
  activeSectionId,
  setActiveSection,
  setFilePaths,
  renderPane,
};
