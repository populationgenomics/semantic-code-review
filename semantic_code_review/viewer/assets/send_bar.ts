// The send bar — the counterpart's fixed place in the `.pr-bar` (ADR
// 0009): *Send all drafts* with the count of unsent drafts, and beside
// it what the counterpart decides. Claude: whether it is listening.
// GitHub: the pending review's state (how many comments GitHub refused,
// retried on an interval), *Submit* with its chooser — what it would
// publish, listed as GitHub holds it at that moment; Comment / Approve /
// Request changes; an optional body — and the review's link once it is
// published, from here or from GitHub.
//
// State is carried in text and shape, never in colour alone: the
// listening glyph is a filled disc or a hollow ring, the unsent count is
// a number, the submitted review a link, a notice a line of text.

import { Comments, type PendingSummary } from "./comments";
import { Render } from "./render";

interface SendBarOptions {
  counterpart: Counterpart;
  /** Whether a `--wait` is attached at first paint (`/data.json`). */
  listening: boolean;
  /** PR mode: the pending review at first paint (`/data.json`). */
  pendingReview: PendingReviewState | null;
  /** How many drafts await a Send; read on every refresh. */
  draftCount: () => number;
  /** Send every draft as one batch. */
  sendAll: () => Promise<void>;
  /** PR mode: deliver every unsent comment again. */
  retry: () => Promise<PendingReviewState | null>;
  /** PR mode: re-derive the pending review from GitHub; null when it
   *  could not be reached. */
  reconcile: () => Promise<ReconcileResponse | null>;
  /** PR mode: publish the pending review. */
  submit: (event: ReviewEvent, body: string) => Promise<SubmitOutcome>;
}

/** How often unsent comments are retried while the tab is open. */
export const RETRY_INTERVAL_MS = 30_000;

const EMPTY_REVIEW: PendingReviewState = { unsent: [], submitted_url: null, submitted_from: null, unanchored: 0 };

const EVENTS: ReadonlyArray<{ value: ReviewEvent; label: string; hint: string }> = [
  { value: "COMMENT", label: "Comment", hint: "Publish the comments without a verdict" },
  { value: "APPROVE", label: "Approve", hint: "Approve the PR; with no comments this is the LGTM" },
  { value: "REQUEST_CHANGES", label: "Request changes", hint: "Ask for changes before it can merge" },
];

let _button: HTMLButtonElement | null = null;
let _count: HTMLElement | null = null;
let _indicator: HTMLElement | null = null;
let _status: HTMLElement | null = null;
let _link: HTMLAnchorElement | null = null;
let _submitButton: HTMLButtonElement | null = null;
let _chooser: Chooser | null = null;
let _opts: SendBarOptions | null = null;
let _pending: PendingReviewState | null = null;
let _retryTimer: ReturnType<typeof setInterval> | null = null;

function install(bar: Element, opts: SendBarOptions): void {
  _opts = opts;
  const root = document.createElement("div");
  root.className = "send-bar";
  root.dataset.counterpart = opts.counterpart;

  if (opts.counterpart === "claude") {
    _indicator = document.createElement("span");
    _indicator.className = "listening-indicator";
    _indicator.setAttribute("role", "status");
    root.appendChild(_indicator);
  } else {
    _status = document.createElement("span");
    _status.className = "pending-review-status";
    _status.setAttribute("role", "status");
    root.appendChild(_status);
    _link = document.createElement("a");
    _link.className = "submitted-review-link hidden";
    _link.target = "_blank";
    _link.rel = "noopener noreferrer";
    root.appendChild(_link);
  }

  _button = document.createElement("button");
  _button.className = "send-all-btn";
  _button.appendChild(document.createTextNode("Send all drafts "));
  _count = document.createElement("span");
  _count.className = "send-all-count";
  _button.appendChild(_count);
  _button.addEventListener("click", () => {
    if (!_button || !_opts) return;
    _button.disabled = true;
    _opts.sendAll().finally(() => refresh());
  });
  root.appendChild(_button);

  if (opts.counterpart === "github") {
    _submitButton = document.createElement("button");
    _submitButton.className = "submit-btn";
    _submitButton.textContent = "Submit…";
    _submitButton.title = "Publish your pending review on GitHub";
    _submitButton.addEventListener("click", (e) => {
      e.stopPropagation();
      if (_chooser?.isOpen()) _chooser.hide();
      else openChooser();
    });
    root.appendChild(_submitButton);
    _chooser = buildChooser();
    root.appendChild(_chooser.root);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && _chooser?.isOpen()) _chooser.hide();
    });
  }

  bar.appendChild(root);
  if (opts.counterpart === "claude") setListening(opts.listening);
  else setPendingReview(opts.pendingReview ?? EMPTY_REVIEW);
  refresh();
}

/** Re-read the draft count. Boot wires this to Comments' onChange. */
function refresh(): void {
  if (!_button || !_count || !_opts) return;
  const n = _opts.draftCount();
  _count.textContent = String(n);
  _button.disabled = n === 0;
  const where = _opts.counterpart === "claude" ? "to Claude as one batch" : "to your pending review on GitHub";
  _button.title = n === 0 ? "No drafts to send" : `Send ${n} draft${n === 1 ? "" : "s"} ${where}`;
  if (_chooser?.isOpen()) _chooser.repaint();
}

/** The `listening` SSE frame: a `--wait` attached or detached. */
function setListening(listening: boolean): void {
  if (!_indicator) return;
  _indicator.dataset.listening = listening ? "true" : "false";
  _indicator.textContent = "";
  const glyph = document.createElement("span");
  glyph.className = "listening-glyph";
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = listening ? "●" : "○";
  _indicator.appendChild(glyph);
  _indicator.appendChild(document.createTextNode(
    listening ? " Claude listening" : " Claude not listening — ask it to resume",
  ));
  _indicator.title = listening
    ? "A `scr review --wait` is attached: a Send reaches Claude now"
    : "Nothing is waiting on this review: a Send is held until Claude runs `scr review --wait` again";
}

// --- PR mode: the pending review --------------------------------------------

/** The `pending-review` SSE frame, and `/data.json` at first paint: what
 *  GitHub does not hold, and the review once submitted. Arms the retry
 *  interval while anything is unsent. */
function setPendingReview(state: PendingReviewState): void {
  _pending = state;
  if (_status) {
    const n = state.unsent.length;
    _status.dataset.unsent = String(n);
    if (n === 0) {
      _status.textContent = "";
      _status.title = "";
    } else {
      _status.textContent = `${n} unsent — retrying`;
      _status.title = state.unsent.map(describeUnsent).join("\n");
    }
  }
  if (_link) {
    if (state.submitted_url) {
      _link.href = state.submitted_url;
      _link.dataset.from = state.submitted_from ?? "viewer";
      _link.textContent = state.submitted_from === "github" ? "Submitted on GitHub ↗" : "Review submitted ↗";
      _link.title = state.submitted_from === "github"
        ? "Your pending review was published from GitHub's web UI; the comments it held are upstream now"
        : "The review you submitted from here";
      _link.classList.remove("hidden");
    } else {
      _link.classList.add("hidden");
    }
  }
  if (_chooser?.isOpen()) _chooser.repaint();
  armRetry(state.unsent.length > 0);
}

function armRetry(needed: boolean): void {
  if (needed && _retryTimer === null) {
    _retryTimer = setInterval(() => {
      _opts?.retry().then((state) => { if (state) setPendingReview(state); });
    }, RETRY_INTERVAL_MS);
  } else if (!needed && _retryTimer !== null) {
    clearInterval(_retryTimer);
    _retryTimer = null;
  }
}

function describeUnsent(u: UnsentComment): string {
  const what = u.deleted ? "deleted comment" : firstLine(u.body);
  const why = u.error ? ` — ${u.error}` : "";
  return `${u.file}:${u.line} (${u.side}) ${what}${why}`;
}

function firstLine(body: string): string {
  const line = body.split("\n").find((l) => l.trim()) ?? "";
  return line.length > 60 ? line.slice(0, 57) + "…" : line;
}

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

// --- the chooser -------------------------------------------------------------
// A panel under the bar, not a modal. Opening it asks GitHub where the
// pending review stands (`reconcile`) and lists what Submit would publish
// from the answer, so the reviewer sees what GitHub holds at that moment
// rather than the store's last belief; unsent comments are listed apart,
// above the button, as the refusal would name them.

interface Chooser {
  root: HTMLElement;
  isOpen: () => boolean;
  hide: () => void;
  /** Redraw the lists and notes from the store and the pending state. */
  repaint: () => void;
  /** The line above the list: checking, stale, or nothing. */
  setStatus: (text: string, state: "checking" | "stale" | "fresh") => void;
  setError: (render: (into: HTMLElement) => void) => void;
  body: HTMLTextAreaElement;
  event: () => ReviewEvent;
}

function buildChooser(): Chooser {
  const panel = document.createElement("div");
  panel.className = "submit-chooser hidden";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Submit review");
  panel.addEventListener("click", (e) => e.stopPropagation());

  const heading = document.createElement("h3");
  heading.className = "submit-heading";
  panel.appendChild(heading);

  const status = document.createElement("p");
  status.className = "submit-status";
  status.setAttribute("role", "status");
  panel.appendChild(status);

  const list = document.createElement("div");
  list.className = "submit-list comment-manifest";
  panel.appendChild(list);

  const unsentSection = document.createElement("div");
  unsentSection.className = "submit-unsent-section";
  panel.appendChild(unsentSection);

  const note = document.createElement("p");
  note.className = "submit-note";
  panel.appendChild(note);

  const options = document.createElement("div");
  options.className = "submit-events";
  for (const ev of EVENTS) {
    const label = document.createElement("label");
    label.className = "submit-event";
    label.title = ev.hint;
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "submit-event";
    input.value = ev.value;
    if (ev.value === "COMMENT") input.checked = true;
    input.addEventListener("change", () => repaint());
    label.appendChild(input);
    label.appendChild(document.createTextNode(" " + ev.label));
    options.appendChild(label);
  }
  panel.appendChild(options);

  const body = document.createElement("textarea");
  body.className = "submit-body";
  body.rows = 3;
  body.placeholder = "Review summary (optional)";
  panel.appendChild(body);

  const error = document.createElement("p");
  error.className = "submit-error";
  error.setAttribute("role", "alert");
  panel.appendChild(error);

  const actions = document.createElement("div");
  actions.className = "submit-actions";
  const cancel = document.createElement("button");
  cancel.className = "submit-cancel";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => hide());
  const go = document.createElement("button");
  go.className = "submit-confirm";
  go.textContent = "Submit review";
  go.addEventListener("click", () => {
    go.disabled = true;
    submitReview(chooser.event(), body.value.trim()).finally(() => { go.disabled = false; });
  });
  actions.appendChild(cancel);
  actions.appendChild(go);
  panel.appendChild(actions);

  const eventLabel = (): string => EVENTS.find((e) => e.value === chooser.event())!.label;

  function repaint(): void {
    const pending = Comments.pendingSummaries();
    list.textContent = "";
    if (pending.length === 0) {
      heading.textContent = `Submit as ${eventLabel()}`;
      const empty = document.createElement("p");
      empty.className = "submit-empty";
      empty.textContent = "No comments — an Approve is an LGTM";
      list.appendChild(empty);
    } else {
      heading.textContent = `Submit ${plural(pending.length, "comment")} as ${eventLabel()}`;
      for (const p of pending) list.appendChild(pendingRow(p));
    }
    unsentSection.textContent = "";
    const unsent = _pending?.unsent ?? [];
    if (unsent.length > 0) {
      const title = document.createElement("p");
      title.className = "submit-unsent-title";
      title.textContent = `${plural(unsent.length, "comment")} unsent — not in the review, and Submit is refused while ${unsent.length === 1 ? "it is" : "they are"}:`;
      unsentSection.appendChild(title);
      const ul = document.createElement("ul");
      ul.className = "submit-unsent";
      for (const u of unsent) {
        const item = document.createElement("li");
        item.dataset.commentId = u.id;
        item.textContent = describeUnsent(u);
        ul.appendChild(item);
      }
      unsentSection.appendChild(ul);
    }
    note.textContent = chooserNote(_opts?.draftCount() ?? 0);
  }

  function hide(): void {
    panel.classList.add("hidden");
  }

  const chooser: Chooser = {
    root: panel,
    isOpen: () => !panel.classList.contains("hidden"),
    hide,
    repaint,
    setStatus: (text, state) => {
      status.textContent = text;
      status.dataset.state = state;
    },
    setError: (render) => {
      error.textContent = "";
      render(error);
    },
    body,
    event: () => (panel.querySelector<HTMLInputElement>('input[name="submit-event"]:checked')?.value ?? "COMMENT") as ReviewEvent,
  };
  return chooser;
}

/** One pending comment as the chooser lists it: the label row the fold
 *  manifests use, which reveals the thread on click; a reply indented
 *  and marked as answering its parent. The chooser stays open. */
function pendingRow(p: PendingSummary): HTMLElement {
  const row = Render.renderCommentLabel(p.file, p.thread);
  row.title = p.body;
  const range = row.querySelector<HTMLElement>(".label-range");
  if (range) range.textContent = `${p.file}:${p.thread.line} (${p.thread.side})`;
  if (p.isReply) {
    row.classList.add("label-reply");
    const text = row.querySelector<HTMLElement>(".label-text");
    if (text) text.textContent = `↳ ${text.textContent}`;
    if (p.parentText) row.title = `${p.body}\n\nin reply to: ${p.parentText}`;
  }
  return row;
}

/** What the chooser says about what Submit will not carry: drafts the
 *  reviewer has not sent, and pending comments this diff cannot show. */
function chooserNote(drafts: number): string {
  const parts: string[] = [];
  if (drafts > 0) {
    parts.push(`${plural(drafts, "draft")} stay${drafts === 1 ? "s" : ""} yours — Send all first to include ${drafts === 1 ? "it" : "them"}.`);
  }
  const hidden = _pending?.unanchored ?? 0;
  if (hidden > 0) {
    parts.push(`${plural(hidden, "pending comment")} on GitHub ${hidden === 1 ? "has" : "have"} no line in this diff and ${hidden === 1 ? "is" : "are"} not shown; ${hidden === 1 ? "it is" : "they are"} submitted with the review.`);
  }
  return parts.join(" ");
}

/** Open the chooser: paint the last known state at once, ask GitHub, and
 *  repaint from the answer. GitHub unreachable leaves the last known
 *  state, marked as such; Submit stays available and reconciles again
 *  itself before deciding. */
function openChooser(): void {
  if (!_chooser || !_opts) return;
  _chooser.setError(() => {});
  _chooser.setStatus("Checking GitHub…", "checking");
  _chooser.repaint();
  _chooser.root.classList.remove("hidden");
  _chooser.root.querySelector<HTMLInputElement>('input[name="submit-event"]:checked')?.focus();
  _opts.reconcile().then((result) => {
    if (!_chooser?.isOpen()) return;
    if (result) {
      setPendingReview(result);
      _chooser.setStatus("", "fresh");
    } else {
      _chooser.setStatus("Could not reach GitHub — showing the last known state", "stale");
      _chooser.repaint();
    }
  });
}

async function submitReview(event: ReviewEvent, body: string): Promise<void> {
  if (!_opts || !_chooser) return;
  const chooser = _chooser;
  const outcome = await _opts.submit(event, body);
  if (outcome.ok) {
    chooser.hide();
    setPendingReview({ ...(_pending ?? EMPTY_REVIEW), submitted_url: outcome.response.review_url, submitted_from: "viewer" });
    return;
  }
  if (outcome.submitted_url) {
    const url = outcome.submitted_url;
    setPendingReview({ ...(_pending ?? EMPTY_REVIEW), submitted_url: url, submitted_from: "github" });
    chooser.setError((into) => {
      into.appendChild(document.createTextNode("Already submitted on GitHub — the comments it held are upstream now. "));
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open the review ↗";
      into.appendChild(link);
    });
    return;
  }
  if (outcome.unsent.length > 0) {
    setPendingReview({ ...(_pending ?? EMPTY_REVIEW), unsent: outcome.unsent });
    chooser.setError((into) => {
      into.appendChild(document.createTextNode(`${outcome.error}:`));
      const list = document.createElement("ul");
      list.className = "submit-unsent";
      for (const u of outcome.unsent) {
        const item = document.createElement("li");
        item.dataset.commentId = u.id;
        item.textContent = describeUnsent(u);
        list.appendChild(item);
      }
      into.appendChild(list);
    });
    return;
  }
  chooser.setError((into) => { into.textContent = outcome.error; });
}

export const SendBar = { install, refresh, setListening, setPendingReview };
