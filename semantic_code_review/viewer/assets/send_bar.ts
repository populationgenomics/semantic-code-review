// The send bar — the counterpart's fixed place in the `.pr-bar` (ADR
// 0009): *Send all drafts* with the count of unsent drafts, and beside
// it what the counterpart decides. Claude: whether it is listening.
// GitHub: the pending review's state (how many comments GitHub refused,
// retried on an interval), *Submit* with its chooser — Comment / Approve
// / Request changes and an optional body — and the review's link once
// it is published.
//
// State is carried in text and shape, never in colour alone: the
// listening glyph is a filled disc or a hollow ring, the unsent count is
// a number, the submitted review a link.

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
  /** PR mode: publish the pending review. */
  submit: (event: ReviewEvent, body: string) => Promise<SubmitOutcome>;
}

/** How often unsent comments are retried while the tab is open. */
export const RETRY_INTERVAL_MS = 30_000;

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
let _chooser: HTMLElement | null = null;
let _chooserError: HTMLElement | null = null;
let _chooserNote: HTMLElement | null = null;
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
    _link.textContent = "Review submitted ↗";
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
      toggleChooser();
    });
    root.appendChild(_submitButton);
    _chooser = buildChooser();
    root.appendChild(_chooser);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && _chooser && !_chooser.classList.contains("hidden")) hideChooser();
    });
  }

  bar.appendChild(root);
  if (opts.counterpart === "claude") setListening(opts.listening);
  else setPendingReview(opts.pendingReview ?? { unsent: [], submitted_url: null, unanchored: 0 });
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
  if (_chooserNote) _chooserNote.textContent = chooserNote(n);
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
      _link.classList.remove("hidden");
    } else {
      _link.classList.add("hidden");
    }
  }
  if (_chooserNote && _opts) _chooserNote.textContent = chooserNote(_opts.draftCount());
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

/** What the chooser says about what Submit will not carry: drafts the
 *  reviewer has not sent, and pending comments this diff cannot show. */
function chooserNote(drafts: number): string {
  const parts: string[] = [];
  if (drafts > 0) {
    parts.push(`${drafts} draft${drafts === 1 ? "" : "s"} stay${drafts === 1 ? "s" : ""} yours — Send all first to include ${drafts === 1 ? "it" : "them"}.`);
  }
  const hidden = _pending?.unanchored ?? 0;
  if (hidden > 0) {
    parts.push(`${hidden} pending comment${hidden === 1 ? "" : "s"} on GitHub ${hidden === 1 ? "has" : "have"} no line in this diff and ${hidden === 1 ? "is" : "are"} not shown; ${hidden === 1 ? "it is" : "they are"} submitted with the review.`);
  }
  return parts.join(" ");
}

// --- the chooser -------------------------------------------------------------

function buildChooser(): HTMLElement {
  const panel = document.createElement("div");
  panel.className = "submit-chooser hidden";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Submit review");
  panel.addEventListener("click", (e) => e.stopPropagation());

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

  _chooserNote = document.createElement("p");
  _chooserNote.className = "submit-note";
  panel.appendChild(_chooserNote);

  _chooserError = document.createElement("p");
  _chooserError.className = "submit-error";
  _chooserError.setAttribute("role", "alert");
  panel.appendChild(_chooserError);

  const actions = document.createElement("div");
  actions.className = "submit-actions";
  const cancel = document.createElement("button");
  cancel.className = "submit-cancel";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => hideChooser());
  const go = document.createElement("button");
  go.className = "submit-confirm";
  go.textContent = "Submit review";
  go.addEventListener("click", () => {
    const checked = panel.querySelector<HTMLInputElement>('input[name="submit-event"]:checked');
    if (!checked) return;
    go.disabled = true;
    submitReview(checked.value as ReviewEvent, body.value.trim()).finally(() => { go.disabled = false; });
  });
  actions.appendChild(cancel);
  actions.appendChild(go);
  panel.appendChild(actions);
  return panel;
}

function toggleChooser(): void {
  if (!_chooser) return;
  if (_chooser.classList.contains("hidden")) showChooser();
  else hideChooser();
}

function showChooser(): void {
  if (!_chooser || !_chooserError) return;
  _chooserError.textContent = "";
  _chooser.classList.remove("hidden");
  _chooser.querySelector<HTMLInputElement>('input[name="submit-event"]:checked')?.focus();
}

function hideChooser(): void {
  _chooser?.classList.add("hidden");
}

async function submitReview(event: ReviewEvent, body: string): Promise<void> {
  if (!_opts || !_chooserError) return;
  const outcome = await _opts.submit(event, body);
  if (outcome.ok) {
    hideChooser();
    if (_pending) setPendingReview({ ..._pending, submitted_url: outcome.response.review_url });
    return;
  }
  if (outcome.unsent.length > 0) {
    _chooserError.textContent = "";
    _chooserError.appendChild(document.createTextNode(`${outcome.error}:`));
    const list = document.createElement("ul");
    list.className = "submit-unsent";
    for (const u of outcome.unsent) {
      const item = document.createElement("li");
      item.dataset.commentId = u.id;
      item.textContent = describeUnsent(u);
      list.appendChild(item);
    }
    _chooserError.appendChild(list);
    return;
  }
  _chooserError.textContent = outcome.error;
}

export const SendBar = { install, refresh, setListening, setPendingReview };
