// The send bar — review mode's fixed place for the lifecycle (ADR 0009):
// *Send all drafts* with the count of unsent drafts, and whether Claude
// is listening. Mounted in the `.pr-bar` where Done sits in PR mode; the
// two never share a page, since the counterpart decides which.
//
// The listening indicator carries its state in text and in the glyph
// (a filled disc while a `--wait` is attached, a hollow ring when not),
// never in colour alone.

interface SendBarOptions {
  /** Whether a `--wait` is attached at first paint (`/data.json`). */
  listening: boolean;
  /** How many drafts await a Send; read on every refresh. */
  draftCount: () => number;
  /** Send every draft as one batch. */
  sendAll: () => Promise<void>;
}

let _button: HTMLButtonElement | null = null;
let _count: HTMLElement | null = null;
let _indicator: HTMLElement | null = null;
let _opts: SendBarOptions | null = null;

function install(bar: Element, opts: SendBarOptions): void {
  _opts = opts;
  const root = document.createElement("div");
  root.className = "send-bar";

  _indicator = document.createElement("span");
  _indicator.className = "listening-indicator";
  _indicator.setAttribute("role", "status");
  root.appendChild(_indicator);

  _button = document.createElement("button");
  _button.className = "send-all-btn";
  _button.title = "Send every draft to Claude as one batch";
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

  bar.appendChild(root);
  setListening(opts.listening);
  refresh();
}

/** Re-read the draft count. Boot wires this to Comments' onChange. */
function refresh(): void {
  if (!_button || !_count || !_opts) return;
  const n = _opts.draftCount();
  _count.textContent = String(n);
  _button.disabled = n === 0;
  _button.title = n === 0
    ? "No drafts to send"
    : `Send ${n} draft${n === 1 ? "" : "s"} to Claude as one batch`;
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

export const SendBar = { install, refresh, setListening };
