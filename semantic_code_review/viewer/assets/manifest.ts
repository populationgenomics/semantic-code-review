// Collapsed content becomes a manifest, not an absence (ADR 0006).
//
// Hiding a range used to erase what was inside it, the reviewer's own
// notes included: a folded hunk showed its `@@` header and nothing else,
// so a comment three lines into it stopped existing until something
// reopened it. Every hide is therefore headed by this list — two
// columns, old side left and new side right, because which side a note
// sits on is half of where it is — of line number plus the note's first
// line, one entry per line.
//
// What is listed, and why a comment and a summary part company here: a
// fold summary *describes* hidden content and is redundant the moment
// that content is hidden again, so it drops out. A comment is something
// the reviewer needs to know exists wherever it sits, so it stays —
// local or ingested, promoted from an LLM annotation or not; a human
// owns it, and that is the test. A resolved thread is finished business
// and drops out with the summaries. An LLM line note stays too,
// separated from a comment by the colour of its sidebar rather than by
// being left out.
//
// The list is deliberately *not navigable* — an entry does not scroll to
// or reveal its line on click — and deliberately *not bounded*: a file
// rarely carries an unmanageable number of notes, and "…and 12 more" is
// the count ADR 0006 rejected.
//
// Which notes a hide covers is `Visibility.covering`'s answer rather
// than a second address model: `under` asks the span store which spans
// cover a note's (side, line) and keeps what the named span is hiding.
// `inRange` is the exception, for the places where the renderer stands
// something in for a line range that no single span expresses — see its
// docstring.

import { Comments } from "./comments";
import { Visibility, type LineRange } from "./visibility";

/** What an entry is, which is what its colour sidebar encodes. The two
 *  kinds match the palette of the expanded view: a reviewer comment is
 *  amber there and here, an LLM annotation blue. */
export type ManifestKind = "comment" | "annotation";

/** One line of a manifest: a note, reduced to where it is and the first
 *  line of what it says. */
export interface ManifestNote {
  kind: ManifestKind;
  side: "old" | "new";
  /** Absolute 1-indexed line on `side`, the same address a `HiddenSpan`
   *  covers — for an ingested comment that is its propagated head line,
   *  as it is for the rendered thread. */
  line: number;
  text: string;
}

// --- What is a note -------------------------------------------------------

/** Every note in a file that a manifest may list, hidden or not.
 *
 *  Pure in its inputs so the rules — resolved drops out, a reply folds
 *  into its thread root, a promoted annotation defers to the comment
 *  that replaced it — are assertable without a document.
 *
 *  Args:
 *      file: The file whose hunks carry the LLM line notes.
 *      comments: Every known reviewer comment, across all files.
 */
function notes(file: FileBlock, comments: ReviewerComment[]): ManifestNote[] {
  const out: ManifestNote[] = [];
  for (const c of comments) {
    if (c.file !== file.path) continue;
    // One entry per thread, at its root: a reply says nothing new about
    // where the discussion is. Resolution is denormalised onto every
    // member and read from the root, so the same pass answers both.
    if (c.in_reply_to_id) continue;
    if (c.thread_resolved) continue;
    const line = Comments.displayLine(c);
    if (line === null) continue;   // file_gone / commit_unavailable: no line to name
    out.push({ kind: "comment", side: c.side, line, text: _firstLine(c.body) });
  }
  for (const h of file.hunks) {
    for (const n of h.line_notes || []) {
      // A promoted note has become a comment, which is already listed;
      // the expanded view drops the source annotation for the same reason.
      if (comments.some((c) => c.derived_from === `${h.id}:line_note:${n.line}`)) continue;
      out.push({ kind: "annotation", side: "new", line: n.line, text: _firstLine(n.body) });
    }
  }
  return out;
}

/** The note's first non-blank line. The entry is one line tall by
 *  construction; the CSS ellipsis handles a first line that is itself
 *  too long. */
function _firstLine(body: string): string {
  for (const line of String(body || "").split("\n")) {
    if (line.trim()) return line.trim();
  }
  return "";
}

// --- What a hide covers ---------------------------------------------------

/** The notes the named span is hiding, in line order.
 *
 *  Asks the store rather than the span's own ranges, so a manifest can
 *  only ever be built for a hide that is actually in place.
 */
function under(fileId: string, spanId: string, all: ManifestNote[]): ManifestNote[] {
  return _sorted(all.filter(
    (n) => Visibility.covering(fileId, n.side, n.line).some((s) => s.id === spanId),
  ));
}

/** The notes inside a pair of line ranges, in line order.
 *
 *  For the two hides the span store does not express as one span:
 *
 *  - A hunk body rendered as a `seg-list`. A segment span is a binary
 *    switch on the body rather than a hide of its own lines (slice 3),
 *    so while every segment is collapsed the hunk's context rows and its
 *    whole base side are covered by no span and rendered by nothing.
 *  - The band standing in for unchanged context on a file `/file-text`
 *    serves no content for (over `_FILE_TEXT_CAP_BYTES`, or binary).
 *    There are no rows to reveal, so there is no span either.
 */
function inRange(
  all: ManifestNote[], right: LineRange | null, left: LineRange | null,
): ManifestNote[] {
  return _sorted(all.filter((n) => _covers(n.side === "new" ? right : left, n.line)));
}

function _covers(range: LineRange | null, line: number): boolean {
  if (range === null) return false;
  return line >= range.start && line <= range.end;
}

function _sorted(ns: ManifestNote[]): ManifestNote[] {
  return [...ns].sort((a, b) => a.line - b.line);
}

// --- Presentation ---------------------------------------------------------

function _el(tag: string, className: string, text?: string): HTMLElement {
  const n = document.createElement(tag);
  n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

/** The two-column list heading a hide, or null when the hide covers no
 *  note (the common case — the caller renders nothing).
 *
 *  Position is the side: left column is base, right is head, matching
 *  the grid the notes sit over. The columns are therefore unlabelled and
 *  both are always emitted — a one-sided hide leaves its opposite column
 *  empty, which is the cue, and a label would only repeat it.
 */
function render(entries: ManifestNote[]): HTMLElement | null {
  if (entries.length === 0) return null;
  const wrap = _el("div", "manifest");
  wrap.appendChild(_column("old", entries));
  wrap.appendChild(_column("new", entries));
  // Not navigable, and not a way to open what it stands in for: the
  // chrome a manifest sits inside (a segment row, a gap chip) toggles on
  // click, and an entry is not that click.
  wrap.addEventListener("click", (e) => e.stopPropagation());
  return wrap;
}

function _column(side: "old" | "new", entries: ManifestNote[]): HTMLElement {
  const col = _el("div", `manifest-col manifest-col-${side}`);
  for (const n of entries) {
    if (n.side !== side) continue;
    const entry = _el("div", `manifest-entry manifest-${n.kind}`);
    entry.title = n.text;
    entry.appendChild(_el("span", "manifest-line", String(n.line)));
    entry.appendChild(_el("span", "manifest-text", n.text));
    col.appendChild(entry);
  }
  return col;
}

export const Manifest = { notes, under, inRange, render };
