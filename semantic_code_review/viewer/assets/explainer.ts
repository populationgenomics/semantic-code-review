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
// Nothing here renders model-authored markup. Prose is plain text
// until the markdown + DOMPurify path lands (slice 2); figures are a
// structured slot rendered by explainer_figure.ts, which reduces them
// to the closed vocabulary in docs/explainer-presentation.md.

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

interface ExplainerInitOptions {
  onChange?: () => void;
  onOpenFile?: (fileId: string) => void;
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
  if (opts.onChange) _onChange = opts.onChange;
  if (opts.onOpenFile) _onOpenFile = opts.onOpenFile;
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

/** Take a document off the SSE bus. Another tab pressed the button; the
 *  spend is already made, so this tab shows the result rather than
 *  offering to pay for it again. */
function onEvent(payload: SseExplainerEvent): void {
  _adopt(payload);
}

function _adopt(doc: ExplainerDocument): void {
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
  if (_activeSectionId === null || !doc.sections.some((s) => s.id === _activeSectionId)) {
    _activeSectionId = doc.sections.length > 0 ? doc.sections[doc.sections.length - 1].id : null;
  }
  _onChange?.();
}

function sections(): ExplainerSection[] {
  return _doc ? _doc.sections : [];
}

function activeSectionId(): string | null {
  return _activeSectionId;
}

function setActiveSection(id: string): void {
  _activeSectionId = id;
  try {
    localStorage.setItem(_lsKey, `explainer:${id}`);
  } catch (_) { /* ignore */ }
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
  if (section.state === "pending") {
    el.appendChild(_el("p", "explainer-status", "Not written yet."));
    return el;
  }
  if (section.state === "failed") {
    el.appendChild(_el("p", "explainer-status explainer-error", "This section could not be written."));
    return el;
  }
  if (section.body) {
    // Plain text until the markdown + DOMPurify path lands (slice 2).
    el.appendChild(_el("p", "explainer-body", section.body));
  }
  // Figures sit after the prose rather than inside it: they are a
  // structured slot, not markup the model embedded in its markdown.
  for (const figure of section.figures) el.appendChild(ExplainerFigures.renderFigure(figure));
  return el;
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

/** Coverage, the dropped-reference count, and the toy-data notice —
 *  rendered rather than logged. References thinning out unnoticed is
 *  exactly the failure the count exists to catch, and a worked example
 *  read as real data is the failure the notice exists to catch. */
function _renderFooter(doc: ExplainerDocument): HTMLElement | null {
  const bits: string[] = [];
  const covered = new Set<string>();
  for (const s of doc.sections) for (const r of s.refs) covered.add(`${r.kind}:${r.id}`);
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
  onEvent,
  sections,
  activeSectionId,
  setActiveSection,
  setFilePaths,
  renderPane,
};
