// Review console — bottom-bar Q&A over the change under review.
//
// Slice 1 (ADR 0002): one-shot, plain-text turns. A persistent,
// unobtrusive prompt input shares the footer with the status counts
// (right); `Ctrl-P` focuses it (intercepting the browser Print
// shortcut — acceptable on a dedicated localhost tab). On submit it
// POSTs /console/ask and renders the answer as PLAIN TEXT in a
// transcript drawer that grows upward above the footer. `Esc`
// collapses the drawer and drops the server-side conversation
// (POST /console/reset).
//
// The conversation `message_history` lives server-side and is
// ephemeral; this module only holds the visible transcript. Streaming,
// markdown/mermaid rendering, and selection-awareness arrive in later
// slices — answers are rendered with `textContent` here, so any
// `<script>`-laden model output is inert by construction.

let _endpoint = "";
let _drawer: HTMLElement | null = null;
let _transcript: HTMLElement | null = null;
let _input: HTMLTextAreaElement | null = null;
let _busy = false;

const MAX_INPUT_ROWS = 6;

/** Mount the console bar into the footer. No-op (returns) when the
 *  footer is absent — e.g. a DOM that doesn't include the status bar. */
function init(endpoint: string): void {
  _endpoint = endpoint;
  const footer = document.getElementById("status-bar");
  if (!footer || _input) return; // idempotent: don't double-mount
  build(footer);
  wireGlobalKeys();
}

function build(footer: HTMLElement): void {
  footer.classList.add("has-console");

  // Transcript drawer: fixed above the footer, hidden until the first
  // turn. Grows upward (CSS caps its height, then it scrolls).
  _drawer = document.createElement("div");
  _drawer.className = "console-drawer hidden";
  _transcript = document.createElement("div");
  _transcript.className = "console-transcript";
  _drawer.appendChild(_transcript);
  document.body.appendChild(_drawer);

  // Prompt input on the left of the footer, ahead of the status counts.
  _input = document.createElement("textarea");
  _input.className = "console-input";
  _input.rows = 1;
  _input.placeholder = "Ask about this change…  (Ctrl-P)";
  _input.setAttribute("aria-label", "Review console prompt");
  footer.insertBefore(_input, footer.firstChild);

  _input.addEventListener("input", autogrow);
  _input.addEventListener("keydown", onInputKey);
}

function wireGlobalKeys(): void {
  window.addEventListener("keydown", (e: KeyboardEvent) => {
    // Ctrl-P / Cmd-P focuses the prompt, suppressing the print dialog.
    if ((e.ctrlKey || e.metaKey) && (e.key === "p" || e.key === "P")) {
      e.preventDefault();
      reveal();
      _input?.focus();
    }
  });
}

function onInputKey(e: KeyboardEvent): void {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void submit();
  } else if (e.key === "Escape") {
    e.preventDefault();
    dismiss();
  }
}

// Grow the textarea with its content, 1 → MAX_INPUT_ROWS lines, then
// let it scroll. Measured off scrollHeight after a reset to auto.
function autogrow(): void {
  if (!_input) return;
  _input.style.height = "auto";
  const line = parseFloat(getComputedStyle(_input).lineHeight) || 18;
  const max = line * MAX_INPUT_ROWS;
  _input.style.height = Math.min(_input.scrollHeight, max) + "px";
}

async function submit(): Promise<void> {
  if (!_input || _busy) return;
  const question = _input.value.trim();
  if (!question) return;
  _input.value = "";
  autogrow();
  reveal();
  appendQuestion(question);
  const pending = appendAnswer();
  setBusy(true);
  try {
    const r = await fetch(`${_endpoint}/console/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!r.ok) {
      pending.classList.add("console-error");
      pending.textContent = await errorText(r);
    } else {
      const data = (await r.json()) as { answer?: string };
      pending.textContent = data.answer || "(empty answer)";
    }
  } catch (e) {
    pending.classList.add("console-error");
    pending.textContent = `request failed: ${e}`;
  } finally {
    setBusy(false);
    scrollToEnd();
  }
}

// Esc: drop the conversation and collapse. The server-side history is
// ephemeral; resetting it means the next turn re-seeds from scratch.
function dismiss(): void {
  _input?.blur();
  collapse();
  if (_transcript) _transcript.textContent = "";
  fetch(`${_endpoint}/console/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => { /* server may be tearing down; reset is best-effort */ });
}

// --- transcript rendering ------------------------------------------------

function appendQuestion(text: string): void {
  if (!_transcript) return;
  const q = document.createElement("div");
  q.className = "console-q";
  q.textContent = text; // plain text — never interpreted as HTML
  _transcript.appendChild(q);
  scrollToEnd();
}

function appendAnswer(): HTMLElement {
  const a = document.createElement("div");
  a.className = "console-a console-pending";
  a.textContent = "…";
  _transcript?.appendChild(a);
  scrollToEnd();
  return a;
}

async function errorText(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { error?: string };
    if (body && body.error) return body.error;
  } catch (_) { /* fall through to status text */ }
  return `request failed (${r.status})`;
}

function setBusy(busy: boolean): void {
  _busy = busy;
  if (_input) _input.classList.toggle("busy", busy);
  // Drop the pending marker on the just-resolved answer.
  if (!busy && _transcript) {
    _transcript.querySelectorAll(".console-pending").forEach((el) => {
      el.classList.remove("console-pending");
    });
  }
}

function reveal(): void {
  _drawer?.classList.remove("hidden");
}

function collapse(): void {
  _drawer?.classList.add("hidden");
}

function scrollToEnd(): void {
  if (_drawer) _drawer.scrollTop = _drawer.scrollHeight;
}

export const Console = { init };
