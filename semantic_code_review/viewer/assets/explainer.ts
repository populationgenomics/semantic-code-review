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
// Prose arrives on the reviewer's press: prose nobody asks for is never
// paid for. A press names a section but buys a *call*, and a call may
// write more than one section (`pass_id` says which share one), so the
// queue is keyed on the pass — otherwise entering the mode would buy
// the merged walkthrough twice. Bodies render through the
// shared markdown-it (`html: false`) + DOMPurify path the console uses
// — model prose derives from a diff that may be hostile, so it never
// reaches innerHTML unsanitised. Inline `[F3]` / `[H3_1]` tokens become
// chips after sanitisation, built as DOM nodes rather than injected
// markup. Figures are a structured slot rendered by
// explainer_figure.ts, which reduces them to the closed vocabulary in
// docs/explainer-presentation.md.

import { renderMarkdown, renderInlineMarkdown } from "./console_render";
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
// it to "open that file beside the document"; where a reference lands
// is render.ts's business, not this module's.
let _onOpenFile: ((fileId: string) => void) | null = null;
// Same, for an inline hunk chip. A hunk reference means "read this
// hunk", so it arrives unfolded — unlike a file reference, which opens
// the file at the collapse level the reviewer left the diff on.
let _onOpenHunk: ((hunkId: string) => void) | null = null;

// Per-section generation state, keyed by section id. Distinct from the
// section's own `state`: this is what THIS tab is doing about it, and
// it is dropped on every fresh document.
type SectionPhase =
  | { kind: "error"; message: string }
  | { kind: "queued" }
  | { kind: "waiting"; annotated: number; total: number };
let _sectionPhase: Record<string, SectionPhase> = Object.create(null);
// One call at a time. The server serialises explainer passes anyway (a
// concurrent POST is a 409), so the queue is what turns "opened every
// section at once" into writes rather than rejections. Entries are
// section ids, but membership is tested by pass: two sections of one
// call are one entry, and asking for the second while the first is in
// flight is asking for what is already running.
let _writing: string | null = null;
const _queue: string[] = [];
// When the in-flight call started, and the repaint timer that keeps the
// elapsed figure on its status line moving. A prose pass runs for
// minutes, and a status line that has said the same four words for eight
// of them cannot be told from a wedged one.
let _writingSince = 0;
let _tick: number | null = null;

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
  _writingSince = 0;
  _stopTicking();
  _deferred.length = 0;
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
/** Queue every prose call the document still needs.
 *
 *  Entering overview mode is the decision to spend; asking again per
 *  section asks twice for one choice. The server runs one pass at a
 *  time, so the run costs the same wall-clock whether the reviewer
 *  clicks each one or not — the difference is only whether they have to
 *  sit and watch each finish before starting the next.
 *
 *  Iterating sections and letting `generateSection` fold the ones that
 *  share a pass: the merged walkthrough is two pending sections and one
 *  call, and enqueuing per section would pay for it twice.
 *
 *  Nothing is queued under `not_warranted`: the skeleton's whole answer
 *  is that prose would not beat reading the hunks. */
function generateAllPending(): void {
  if (_doc === null || _doc.verdict !== "narrate") return;
  for (const s of _doc.sections) {
    if (s.kind !== "map" && s.state === "pending") generateSection(s.id);
  }
}

/** The call that writes a section, or null when it has no document
 *  entry. Sections of one pass share it. */
function _passOf(id: string): string | null {
  const section = _findSection(id);
  return section ? section.pass_id : null;
}

/** True when a call for this section's pass is already running or
 *  queued. Asking for the second half of a merged pass while the first
 *  is in flight is asking for what is already running. */
function _passInFlight(id: string): boolean {
  const wanted = _passOf(id);
  if (wanted === null) return false;
  if (_writing !== null && _passOf(_writing) === wanted) return true;
  if (_deferred.some((d) => _passOf(d) === wanted)) return true;
  return _queue.some((queued) => _passOf(queued) === wanted);
}

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
    if (r.status === 409 && payload.retry) {
      // Augmentation has not finished, or another pass holds the slot.
      // Both clear on their own, so this is a wait rather than a failure.
      _phase = "absent";
      _onChange?.();
      window.setTimeout(() => void generate(), _RETRY_MS);
      return;
    }
    if (!r.ok) throw new Error(payload.error || `POST /explainer/skeleton -> ${r.status}`);
    _adopt(payload as ExplainerDocument);
    generateAllPending();
  } catch (e) {
    _phase = "error";
    _error = String(e instanceof Error ? e.message : e);
    _onChange?.();
  }
}

/** Ask the server to write a section's prose. No-op for a section that
 *  is already written, or whose call is already running or queued: the
 *  call is not free and a second press must not buy the same paragraph
 *  twice. */
function generateSection(id: string): void {
  const section = _findSection(id);
  if (!section || section.kind === "map" || section.state === "ready") return;
  if (_passInFlight(id)) return;
  _queue.push(id);
  for (const s of _sectionsInPass(section.pass_id)) delete _sectionPhase[s.id];
  _onChange?.();
  void _drainQueue();
}

/** Every top-level section one call writes, in document order. */
function _sectionsInPass(passId: string): ExplainerSection[] {
  if (!_doc) return [];
  return _doc.sections.filter((s) => s.kind !== "map" && s.pass_id === passId);
}

//: Backoff before retrying a section the server was too busy to write.
//: Fallback only. A deferred call is normally woken by the `explainer`
//: SSE frame the finishing pass publishes; this covers a frame that
//: never arrives (a dropped stream, a pass that died without emitting).
//: Short values are what made the pane flash: every poll rendered
//: "Writing…" on the way out and "Queued…" on the way back.
const _RETRY_MS = 30000;

//: Calls the server refused as busy. Kept out of `_queue` on purpose:
//: re-queueing from inside `_writeSection` lands back in the drain
//: loop's own `while`, which retries immediately and spins — the timer
//: never paces anything. These move back only when something wakes
//: them.
const _deferred: string[] = [];

/** Move everything the server was too busy for back into the queue and
 *  drain. Called when a pass finishes (the SSE frame) or the fallback
 *  timer fires. */
function _wakeDeferred(): void {
  if (_deferred.length === 0) return;
  _queue.push(..._deferred.splice(0, _deferred.length));
  void _drainQueue();
}

async function _drainQueue(): Promise<void> {
  if (_writing !== null) return;
  while (_queue.length > 0) {
    const id = _queue.shift() as string;
    _writing = id;
    _writingSince = Date.now();
    _startTicking();
    _onChange?.();
    await _writeSection(id);
    _writing = null;
    _onChange?.();
  }
  _stopTicking();
}

//: How often the pane repaints while a call is in flight, so the elapsed
//: figure on its status line moves. Fine enough to land each minute mark
//: within half a minute of it, and the repaint is the whole pane — which
//: is what every other state change here already costs.
const _TICK_MS = 30000;

function _startTicking(): void {
  if (_tick !== null) return;
  _tick = window.setInterval(() => _onChange?.(), _TICK_MS);
}

function _stopTicking(): void {
  if (_tick === null) return;
  window.clearInterval(_tick);
  _tick = null;
}

/** How long the in-flight call has been running, as a suffix for its
 *  status line. Minutes: a passing second is not news, and rounding down
 *  keeps the figure from claiming time that has not passed. */
function _elapsedSuffix(): string {
  const minutes = Math.floor((Date.now() - _writingSince) / 60000);
  return minutes < 1 ? "" : ` ${minutes} min`;
}

async function _writeSection(id: string): Promise<void> {
  // The POST names a section, but the outcome belongs to its whole
  // call: a merged pass that is waiting or failed is waiting or failed
  // for both of its sections, and marking only the one the reviewer
  // happened to press leaves its sibling claiming it was never asked
  // for.
  const passId = _passOf(id);
  const affected = passId === null ? [id] : _sectionsInPass(passId).map((s) => s.id);
  const setPhase = (phase: SectionPhase | null): void => {
    for (const sid of affected) {
      if (phase === null) delete _sectionPhase[sid];
      else _sectionPhase[sid] = phase;
    }
  };
  try {
    const r = await fetch(`${_endpoint}/explainer/section/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await r.json().catch(() => ({}));
    if (r.status === 409 && typeof payload.total === "number") {
      // Not an error: the hunks this call is anchored to aren't all
      // annotated, and prose over the gaps reads exactly as fluently as
      // prose over the whole thing.
      setPhase({ kind: "waiting", annotated: payload.annotated || 0, total: payload.total });
      return;
    }
    if (r.status === 409 && payload.retry) {
      // Another pass holds the server's slot — one per server, shared by
      // the skeleton and every prose call. It clears on its own, so this
      // is a wait, not a failure: re-queue rather than making the
      // reviewer press again for a condition that resolves itself.
      // Hold the section visibly queued rather than clearing its phase:
      // the drain loop sets `_writing` before every attempt, so a
      // cleared phase renders "Writing…" for the length of each retry
      // and "Queued…" between them. The phase outranks `_writing` in
      // the render, so the pane stays still until the outcome changes.
      setPhase({ kind: "queued" });
      _deferred.push(id);
      window.setTimeout(_wakeDeferred, _RETRY_MS);
      return;
    }
    if (!r.ok) throw new Error(payload.error || `POST /explainer/section/${id} -> ${r.status}`);
    _adopt(payload as ExplainerDocument, { keepPhases: true });
  } catch (e) {
    setPhase({ kind: "error", message: String(e instanceof Error ? e.message : e) });
  }
}

/** Take a document off the SSE bus. Another tab pressed the button; the
 *  spend is already made, so this tab shows the result rather than
 *  offering to pay for it again. */
function onEvent(payload: SseExplainerEvent): void {
  _adopt(payload, { keepPhases: true });
  // A pass just finished, so the server's single slot is free. Whatever
  // deferred on a busy 409 can go now, without waiting out the fallback.
  _wakeDeferred();
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
  // A document is proof its own skeleton ran, so its inputs are on
  // disk whether or not this tab saw the `overview` frame. Without
  // this a tab that boots mid-pass onto an earlier run's document
  // holds one it cannot open — and, since the viewer now opens into
  // the document, one it cannot leave by the button either.
  _ready = true;
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
  // Still queues on selection, for the section that failed or that a
  // reviewer reached before the auto-queue drained.
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
  for (const node of _proseNodes(section)) el.appendChild(node);
  // Figures sit after the prose rather than inside it: they are a
  // structured slot, not markup the model embedded in its markdown.
  for (const figure of section.figures || []) el.appendChild(ExplainerFigures.renderFigure(figure));
  // The glossary follows the section's own body, which is what it
  // consolidates. The skip box above it is navigation, not content.
  if (section.terms && section.terms.length > 0) el.appendChild(_renderTerms(section.terms));
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

/** A term list, each entry a callout.
 *
 *  A definition *is* the `concept` callout — "a definition or
 *  load-bearing idea" is what that kind was written for — so the two
 *  share one visual rather than inventing a second way to say the same
 *  thing. The pair stays inside a `dl`: HTML5 allows a `div` to group a
 *  `dt`/`dd`, so the box costs nothing semantically.
 *
 *  The title is not uppercased the way an alert's fixed label is. These
 *  are the model's words and often identifiers — `ServiceImpl<typeof
 *  Workbench>` in caps is not the name of anything. */
function _renderTerms(terms: ExplainerTerm[]): HTMLElement {
  const dl = _el("dl", "explainer-terms");
  for (const t of terms) {
    const box = _el("div", "explainer-callout explainer-callout-term");
    const dt = _el("dt", "explainer-callout-title explainer-term-title");
    renderInlineMarkdown(dt, t.term);
    box.appendChild(dt);
    const dd = _el("dd", "explainer-callout-body");
    dd.appendChild(_renderBody(t.definition));
    box.appendChild(dd);
    dl.appendChild(box);
  }
  return dl;
}

/** What the call that wrote this section actually opened. Recorded from
 *  the tool surface, so prose citing nothing is visibly prose that read
 *  nothing — which is legible without judging it.
 *
 *  One line per *call*, under the last section that call wrote: the read
 *  list belongs to the call, and repeating the identical sentence under
 *  each of a merged pair says nothing the first one did not. */
function _renderSources(section: ExplainerSection): HTMLElement | null {
  if (section.state !== "ready" || section.kind === "map") return null;
  if (!_isLastOfPass(section)) return null;
  const sources = section.sources || [];
  const text = sources.length > 0
    ? `Read while writing this: ${sources.join(", ")}`
    : "Written without reading any file.";
  return _el("p", "explainer-sources", text);
}

/** True when no later top-level section shares this one's pass. */
function _isLastOfPass(section: ExplainerSection): boolean {
  const siblings = _sectionsInPass(section.pass_id);
  return siblings.length === 0 || siblings[siblings.length - 1].id === section.id;
}

/** A model-chosen part of the walkthrough. It carries its own
 *  references and its own sidebar node, but it is never generated on
 *  its own — its parent's pass wrote it. */
function _renderSubsection(section: ExplainerSection): HTMLElement {
  const el = _el("section", "explainer-subsection");
  el.dataset.sectionId = section.id;
  const heading = _el("h3", "explainer-subsection-title");
  renderInlineMarkdown(heading, section.title);
  heading.id = _headingId(section.id);
  el.appendChild(heading);
  if (section.body) el.appendChild(_renderBody(section.body));
  // A subsection's figures render like its parent's: the server
  // sanitises and namespaces them wherever they sit in the tree, so a
  // slot that renders at one depth and not the other is a figure that
  // was written, cleaned, counted and never shown.
  for (const figure of section.figures || []) el.appendChild(ExplainerFigures.renderFigure(figure));
  return el;
}

// What this tab is doing about a section's call. `deferred` is the
// server's slot held elsewhere, which is a different wait from queueing
// behind a call this tab started.
type WriteState = "writing" | "queued" | "deferred";

/** Which of those a section is in, or null when its own state is the
 *  whole answer.
 *
 *  By pass, not by id: the reviewer pressed one section of a merged
 *  call, and the other one is being written too. The `queued` phase
 *  outranks `_writing` — see the busy-409 branch of `_writeSection`. */
function _writeState(section: ExplainerSection): WriteState | null {
  const phase = _sectionPhase[section.id];
  if (phase && phase.kind === "queued") {
    return _blockingSection(section.pass_id) === null ? "deferred" : "queued";
  }
  if (_writing !== null && _passOf(_writing) === section.pass_id) return "writing";
  if (_queue.some((queued) => _passOf(queued) === section.pass_id)) return "queued";
  return null;
}

/** The section this tab is writing ahead of `passId`'s call. Null when
 *  it is writing nothing, or writing `passId`'s own call — a deferred
 *  section being retried is not queued behind itself. */
function _blockingSection(passId: string): ExplainerSection | null {
  if (_writing === null || _passOf(_writing) === passId) return null;
  return _findSection(_writing);
}

/** The status line for a section whose prose is on its way. */
function _writeStatusText(section: ExplainerSection, state: WriteState): string {
  if (state === "writing") return `Writing this section…${_elapsedSuffix()}`;
  if (state === "deferred") return "Waiting for the server — retrying.";
  const blocker = _blockingSection(section.pass_id);
  return blocker ? `Queued behind ${blocker.title}…` : "Queued behind another section…";
}

//: The same states as the sidebar tree shows them: a glance, next to a
//: section's title, of what the pane says at length.
const _SECTION_STATUS: Record<WriteState, string> = {
  writing: "writing…",
  queued: "queued",
  deferred: "waiting",
};

/** What the sidebar's tree puts beside a section's title, or null when
 *  this tab is doing nothing about it. */
function sectionStatus(id: string): string | null {
  const section = _findSection(id);
  if (section === null) return null;
  const state = _writeState(section);
  return state === null ? null : _SECTION_STATUS[state];
}

/** The body of one prose section: what it says, or what this tab is
 *  doing about the fact that it does not say anything yet. */
function _proseNodes(section: ExplainerSection): HTMLElement[] {
  const writeState = _writeState(section);
  if (writeState !== null) {
    return [_el("p", "explainer-status", _writeStatusText(section, writeState))];
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
  btn.title = "Open this file beside the document";
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
      const chip = _refChip(m[1]);
      _labelRef(chip, m[1], _precedingText(node, m.index));
      frag.appendChild(chip);
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

//: How much prose before a reference to search for the name it cites.
//: One clause is enough — further back and an unrelated earlier mention
//: would silently strip a label the sentence needs.
const _NAMED_WINDOW = 80;

/** The text immediately before `offset` in `node`, walking back into
 *  earlier siblings so a name inside a code span still counts. */
function _precedingText(node: Text, offset: number): string {
  let out = (node.nodeValue || "").slice(0, offset);
  let cur: Node | null = node;
  while (out.length < _NAMED_WINDOW && cur) {
    const prev: Node | null = cur.previousSibling;
    if (!prev) { cur = cur.parentNode; if (!cur || cur.nodeName === "DIV") break; continue; }
    out = (prev.textContent || "") + out;
    cur = prev;
  }
  return out.slice(-_NAMED_WINDOW);
}

/** An inline reference: an arrow, labelled only when it has to be.
 *
 *  Where the prose has just named its target — "models/workbench.proto
 *  [F10] holds the message shapes" — a chip repeating the full path
 *  breaks the line for no information, and a bare arrow reads as the
 *  citation it is. Where it has not, the reference is carrying the
 *  sentence: "[F11] declares the three RPCs" and "everything under
 *  [F0], [F1], [F14] is the protoc output" both become unreadable rows
 *  of anonymous glyphs. So the label is dropped only when it would be a
 *  repetition, and the basename stands in otherwise — never the full
 *  path, which is what made the chip a slab. */
function _refChip(id: string): HTMLElement {
  const isFile = id.charAt(0) === "F";
  const target = isFile ? _fileLabel(id) : _hunkLabel(id);
  const btn = _el("button", "explainer-ref explainer-arrow");
  btn.dataset.refId = id;
  btn.title = `Open ${target} beside the document`;
  btn.setAttribute("aria-label", btn.title);
  btn.addEventListener("click", () => {
    if (isFile) _onOpenFile?.(id);
    else _onOpenHunk?.(id);
  });
  return btn;
}

/** Give a reference its label, or leave it bare. Split from `_refChip`
 *  because only the caller walking the prose knows what came before. */
function _labelRef(btn: HTMLElement, id: string, preceding: string): void {
  const m = /^H(\d+)_(\d+)$/.exec(id);
  // A hunk's path is its file's — `_fileLabel` on a hunk id finds nothing
  // and hands back the raw id, which is what a reviewer cannot act on.
  const path = m ? _fileLabel(`F${m[1]}`) : _fileLabel(id);
  const base = path.slice(path.lastIndexOf("/") + 1);
  if (base.length > 0 && preceding.indexOf(base) !== -1) {
    btn.textContent = "\u2197";
    return;
  }
  btn.classList.add("explainer-arrow-labelled");
  btn.textContent = m ? `${base}:${Number(m[2]) + 1} \u2197` : `${base} \u2197`;
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
  generateAllPending,
  onEvent,
  sections,
  sectionStatus,
  activeSectionId,
  setActiveSection,
  setFilePaths,
  renderPane,
};
