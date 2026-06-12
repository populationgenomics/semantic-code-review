// Intra-line ("character") sub-diff + a DOM range-wrapping primitive.
//
// `charDiff` finds the changed span between a deleted and inserted line
// of a `pair` row; `wrapRanges` paints character-offset ranges onto an
// already-rendered code cell (which may already hold highlight.js spans)
// by splitting its text nodes. The two are independent: `wrapRanges` is
// also what the symbol-focus search highlight uses, over whole-line
// match ranges rather than diff ranges.

/** A half-open character range `[start, end)` over a string / a node's
 *  `textContent`. */
export type CharRange = [number, number];

/** Changed character ranges on each side of a paired delete/insert. */
export interface SideRanges {
  oldRanges: CharRange[];
  newRanges: CharRange[];
}

// A line with more tokens than this skips the O(n*m) word LCS and falls
// back to the linear prefix/suffix diff (guards against minified blobs).
const _MAX_WORD_DIFF_TOKENS = 200;

// Tokens for word-level diffing: identifier runs, whitespace runs, and
// single "other" characters — each tagged with its offset in the line.
const _TOKEN_RE = /[A-Za-z0-9_$]+|\s+|[^A-Za-z0-9_$\s]/g;

interface _Token { text: string; start: number; }

function _tokenize(s: string): _Token[] {
  const out: _Token[] = [];
  for (let m = _TOKEN_RE.exec(s); m !== null; m = _TOKEN_RE.exec(s)) {
    out.push({ text: m[0], start: m.index });
  }
  _TOKEN_RE.lastIndex = 0;
  return out;
}

const _span = (t: _Token): CharRange => [t.start, t.start + t.text.length];

/** Token-level diff between a deleted line `a` and an inserted line `b`:
 *  the changed-token ranges on each side, leaving unchanged tokens between
 *  edits unmarked (GitHub/GitLab-style word diff). Adjacent changed tokens
 *  coalesce once `wrapRanges` normalises the ranges.
 *
 *  Computed as a longest-common-subsequence over tokens; falls back to the
 *  linear `charDiff` for pathologically long lines. A single contiguous
 *  edit yields the same result as `charDiff`. */
export function wordDiff(a: string, b: string): SideRanges {
  const A = _tokenize(a);
  const B = _tokenize(b);
  if (A.length > _MAX_WORD_DIFF_TOKENS || B.length > _MAX_WORD_DIFF_TOKENS) {
    return charDiff(a, b);
  }
  const n = A.length;
  const m = B.length;
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
  // whichever side keeps the longer LCS becomes a changed range.
  const oldRanges: CharRange[] = [];
  const newRanges: CharRange[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (A[i].text === B[j].text) {
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      oldRanges.push(_span(A[i++]));
    } else {
      newRanges.push(_span(B[j++]));
    }
  }
  while (i < n) oldRanges.push(_span(A[i++]));
  while (j < m) newRanges.push(_span(B[j++]));
  return { oldRanges, newRanges };
}

/** Changed character ranges between a deleted line `a` and an inserted
 *  line `b`, computed as the gap left after stripping the common prefix
 *  and common suffix — one contiguous span per side. Used directly as the
 *  long-line fallback for `wordDiff`; on a single-edit line the two agree.
 *
 *  Returns one range per side (or none, when that side is wholly within
 *  the shared prefix/suffix — e.g. a pure insertion leaves `oldRanges`
 *  empty). */
export function charDiff(a: string, b: string): SideRanges {
  const shorter = Math.min(a.length, b.length);
  let prefix = 0;
  while (prefix < shorter && a[prefix] === b[prefix]) prefix++;
  let suffix = 0;
  while (
    suffix < shorter - prefix &&
    a[a.length - 1 - suffix] === b[b.length - 1 - suffix]
  ) {
    suffix++;
  }
  const aEnd = a.length - suffix;
  const bEnd = b.length - suffix;
  return {
    oldRanges: prefix < aEnd ? [[prefix, aEnd]] : [],
    newRanges: prefix < bEnd ? [[prefix, bEnd]] : [],
  };
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
