// Intra-line ("token") sub-diff + a DOM range-wrapping primitive.
//
// `blockDiff` token-diffs a *block* of deleted lines against a block of
// inserted lines — across line boundaries, not line-by-line — and returns
// the changed-character ranges for each line. `wrapRanges` paints those
// ranges onto an already-rendered code cell (which may already hold
// highlight.js spans) by splitting its text nodes. The two are
// independent: `wrapRanges` is also what the symbol-focus search highlight
// uses, over whole-line match ranges rather than diff ranges.

/** A half-open character range `[start, end)` over a string / a node's
 *  `textContent`. */
export type CharRange = [number, number];

/** Changed-character ranges for a block, parallel to the input line
 *  arrays: `old[i]` is the ranges within deleted line `i`, `new[j]` within
 *  inserted line `j`. */
export interface BlockRanges {
  old: CharRange[][];
  new: CharRange[][];
}

// Guard the O(n*m) LCS: a block whose token counts multiply past this
// (e.g. a minified blob) skips the sub-diff and renders with the whole-row
// tint only. Generous enough that real multi-line blocks always run.
const _MAX_DIFF_TOKEN_PRODUCT = 250_000;

// Tokens for the diff: identifier runs, whitespace runs, and single
// "other" characters. A token carries its line index within the block and
// its offset within that line; cross-line `\n` sentinels (line -1) keep
// the two token streams aligned at line boundaries without ever being
// rendered as a range.
const _TOKEN_RE = /[A-Za-z0-9_$]+|\s+|[^A-Za-z0-9_$\s]/g;

interface _Token { text: string; line: number; start: number; end: number; }

function _tokenizeBlock(lines: string[]): _Token[] {
  const out: _Token[] = [];
  lines.forEach((line, li) => {
    if (li > 0) out.push({ text: "\n", line: -1, start: 0, end: 0 });
    for (let m = _TOKEN_RE.exec(line); m !== null; m = _TOKEN_RE.exec(line)) {
      out.push({ text: m[0], line: li, start: m.index, end: m.index + m[0].length });
    }
    _TOKEN_RE.lastIndex = 0;
  });
  return out;
}

/** Token-level diff of a deleted-line block against an inserted-line block.
 *  Each changed token becomes a range on its line; unchanged tokens between
 *  edits stay unmarked (GitHub/GitLab-style word diff), and because the
 *  diff runs over the whole block, a change that spans several old lines
 *  (e.g. an inline object type collapsed to a named type) is marked as one
 *  deletion + one insertion rather than per line.
 *
 *  Adjacent changed tokens coalesce once `wrapRanges` normalises the
 *  ranges. Computed as a longest-common-subsequence over tokens; a block
 *  past the token-product guard returns empty ranges (row tint only). */
export function blockDiff(oldLines: string[], newLines: string[]): BlockRanges {
  const old: CharRange[][] = oldLines.map(() => []);
  const neu: CharRange[][] = newLines.map(() => []);
  const A = _tokenizeBlock(oldLines);
  const B = _tokenizeBlock(newLines);
  const n = A.length;
  const m = B.length;
  if (n * m > _MAX_DIFF_TOKEN_PRODUCT) return { old, new: neu };

  // dp[i][j] = LCS length of A[i:] and B[j:].
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i].text === B[j].text
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  // Backtrack: matched tokens advance both sides; an unmatched token on
  // whichever side keeps the longer LCS becomes a changed range on its
  // line. Sentinels (line -1) are dropped — they only steer alignment.
  const mark = (t: _Token, into: CharRange[][]): void => {
    if (t.line >= 0) into[t.line].push([t.start, t.end]);
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (A[i].text === B[j].text) { i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) mark(A[i++], old);
    else mark(B[j++], neu);
  }
  while (i < n) mark(A[i++], old);
  while (j < m) mark(B[j++], neu);
  return { old, new: neu };
}

/** Whole-identifier occurrences of `term` in `text`, as character ranges.
 *
 *  Bounded by `[^\w$]` on each side so focusing the symbol `get` matches
 *  the call `get(x)` but not the substring inside `getName` or `widget`.
 *  Returns `[]` for an empty term. Used by the symbol-focus search
 *  highlight; the ranges feed `wrapRanges`. */
export function matchRanges(text: string, term: string): CharRange[] {
  if (!term) return [];
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`(?<![\\w$])${escaped}(?![\\w$])`, "g");
  const out: CharRange[] = [];
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    out.push([m.index, m.index + m[0].length]);
    if (re.lastIndex === m.index) re.lastIndex++; // defensive: never loop
  }
  return out;
}

/** Wrap each character range of `root`'s text content in a
 *  `<span class={className}>`, splitting text nodes (and crossing inline
 *  element boundaries such as highlight.js token spans) as needed.
 *
 *  Offsets are measured over `root.textContent`, which highlight.js
 *  leaves byte-for-byte identical to the source line, so diff/search
 *  ranges computed against the raw line text line up. Ranges may be
 *  unsorted or overlapping; they're normalised first. A no-op for an
 *  empty list. */
export function wrapRanges(
  root: Node,
  ranges: CharRange[],
  className: string,
): void {
  const merged = _normaliseRanges(ranges);
  if (merged.length === 0) return;

  // Snapshot text nodes with their global offsets before mutating: each
  // node is replaced independently, so earlier replacements don't shift
  // the offsets recorded for later nodes.
  const doc = root.ownerDocument || document;
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: { node: Text; start: number; end: number }[] = [];
  let offset = 0;
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const text = n as Text;
    const len = text.nodeValue?.length ?? 0;
    nodes.push({ node: text, start: offset, end: offset + len });
    offset += len;
  }

  for (const { node, start, end } of nodes) {
    const local: CharRange[] = [];
    for (const [rs, re] of merged) {
      const s = Math.max(rs, start);
      const e = Math.min(re, end);
      if (s < e) local.push([s - start, e - start]);
    }
    if (local.length > 0) _wrapTextNode(node, local, className, doc);
  }
}

/** Replace one text node with a fragment where `local` (sorted,
 *  non-overlapping, node-relative) ranges are wrapped in span.className. */
function _wrapTextNode(
  node: Text,
  local: CharRange[],
  className: string,
  doc: Document,
): void {
  const text = node.nodeValue ?? "";
  const frag = doc.createDocumentFragment();
  let cursor = 0;
  for (const [s, e] of local) {
    if (s > cursor) frag.appendChild(doc.createTextNode(text.slice(cursor, s)));
    const span = doc.createElement("span");
    span.className = className;
    span.textContent = text.slice(s, e);
    frag.appendChild(span);
    cursor = e;
  }
  if (cursor < text.length) frag.appendChild(doc.createTextNode(text.slice(cursor)));
  node.parentNode?.replaceChild(frag, node);
}

/** Sort by start, drop empties, and merge touching/overlapping ranges. */
function _normaliseRanges(ranges: CharRange[]): CharRange[] {
  const valid = ranges.filter(([s, e]) => e > s).sort((x, y) => x[0] - y[0]);
  const out: CharRange[] = [];
  for (const [s, e] of valid) {
    const last = out[out.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else out.push([s, e]);
  }
  return out;
}
