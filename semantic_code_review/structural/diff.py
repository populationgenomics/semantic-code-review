"""Deterministic base→head symbol delta (ADR 0001).

The `Symbol` forest carries *where the code is*; this module carries
*what changed between two revisions*. Buckets are a `qualified_name`
set-diff over the flattened base and head forests, refined by comparing
the two sides' declared signature and span text:

  * added     — qualified name present on head only
  * removed   — present on base only
  * modified  — present on both, and the code differs; `reason` says
                whether the *declaration* moved (`SIGNATURE` — an API
                change) or only the body (`BODY`)
  * moved     — present on both with byte-identical span text: a pure
                relocation, no code change

`moved` is a bucket rather than a `reason` on `modified` because it is
the overwhelming majority and carries no information about the change:
on a measured 6-file diff, 244 of 262 same-name-both-sides symbols had
shifted only because lines above them moved. Keeping them out of
`modified` means "what changed" needs no filtering by every consumer.

Span *text* is what every comparison here turns on — not the span. A
body edit that adds and removes the same number of lines preserves
`end - start`, so a length comparison reports it as a pure shift;
measured on the same diff, that misfiled one of the six real API
changes. Conversely an in-place edit that leaves the span byte-for-byte
unmoved *is* flagged: the range is not the signal.

Cross-file relocation is resolved in `merge`, diff-wide: a qualified
name that is `removed` at one path and `added` at another *with
identical span text* is one move, not two events, and collapses into a
single `moved` entry carrying `from_path`. Identity is required, not
similarity — `qualified_name` is only unique within a file, so two
unrelated files each defining `helper` must not be linked. A symbol
that both moved file and changed stays as separate added/removed
entries; calling those one event would be an inference, and ADR 0001
keeps this layer to parse-and-compare. The rule reads a one-line
boilerplate definition (`log = logging.getLogger(__name__)` deleted from
one module, present in another) as a move. Deterministically true, and
not worth a span-length threshold to suppress.

Each `ChangedSymbol` carries its `path` (the delta is diff-wide, across
files) and the span on its *live* side: head for added/modified/moved,
base for removed.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Iterable

from pydantic import BaseModel, Field

from .symbols import Symbol, SymbolRange


class ChangeReason(enum.StrEnum):
    """Why a symbol is in `SymbolDelta.modified`."""

    SIGNATURE = "signature"
    """The declared header differs — an API change."""

    BODY = "body"
    """The header is unchanged; the implementation differs."""


class ChangedSymbol(BaseModel):
    """One symbol that changed between base and head.

    Flat (the tree is flattened by `qualified_name` before diffing), so
    `children` is intentionally absent. `range` is the symbol's span on
    its live side — head for added/modified/moved, base for removed.
    `reason` is set on `modified` entries only. `from_path` is set on a
    `moved` entry whose relocation crossed files, and names the base-side
    path.

    `body_sha` digests the symbol's span text. It is the comparison key
    for `moved` — including across files, where `merge` has the entries
    but not the sources — and is excluded from every serialisation: it is
    an internal identity, not part of the `Symbol` currency the ADR pins.
    """

    path: str
    kind: str
    name: str
    qualified_name: str
    range: SymbolRange
    signature: str | None = None
    reason: ChangeReason | None = None
    from_path: str | None = None
    body_sha: str = Field(exclude=True)


class SymbolDelta(BaseModel):
    """The diff-wide structural delta, bucketed by what changed."""

    added: list[ChangedSymbol] = Field(default_factory=list)
    removed: list[ChangedSymbol] = Field(default_factory=list)
    modified: list[ChangedSymbol] = Field(default_factory=list)
    moved: list[ChangedSymbol] = Field(default_factory=list)


def flatten(symbols: list[Symbol]) -> dict[str, Symbol]:
    """Map `qualified_name → Symbol` over the whole forest, depth-first.

    Within a file `qualified_name` is unique (the dotted path through
    enclosing definitions), so collisions don't occur; insertion order
    is source order, which the diff buckets inherit.
    """
    out: dict[str, Symbol] = {}

    def walk(syms: list[Symbol]) -> None:
        for s in syms:
            out[s.qualified_name] = s
            walk(s.children)

    walk(symbols)
    return out


def _span_text(src: str, rng: SymbolRange) -> str:
    """The source lines the symbol spans, 1-indexed inclusive."""
    return "\n".join(src.splitlines()[rng.start_line - 1 : rng.end_line])


def _body_sha(src: str | None, sym: Symbol) -> str:
    """Digest of a symbol's span text.

    `src` is `None` only when the file is absent on that side, in which
    case no symbol from it can exist.
    """
    if src is None:
        raise ValueError(f"no source for {sym.qualified_name}: a symbol cannot come from an absent file")
    return hashlib.sha256(_span_text(src, sym.range).encode("utf-8")).hexdigest()


def _changed(
    path: str,
    sym: Symbol,
    src: str | None,
    *,
    reason: ChangeReason | None = None,
) -> ChangedSymbol:
    return ChangedSymbol(
        path=path,
        kind=sym.kind,
        name=sym.name,
        qualified_name=sym.qualified_name,
        range=sym.range,
        signature=sym.signature,
        reason=reason,
        body_sha=_body_sha(src, sym),
    )


def diff_file(
    path: str,
    base: list[Symbol],
    head: list[Symbol],
    *,
    base_src: str | None,
    head_src: str | None,
) -> SymbolDelta:
    """Per-file `qualified_name` set-diff between two `Symbol` forests.

    An added file passes `base=[]` and `base_src=None`; a deleted file
    passes `head=[]` and `head_src=None`. The sources are the text the
    forests were parsed from — `moved` vs `modified` compares span text,
    which the `Symbol` tree does not carry.

    Cross-file relocation is not visible here; `merge` resolves it.
    """
    b = flatten(base)
    h = flatten(head)
    delta = SymbolDelta(
        added=[_changed(path, h[q], head_src) for q in h if q not in b],
        removed=[_changed(path, b[q], base_src) for q in b if q not in h],
    )
    for q, hs in h.items():
        bs = b.get(q)
        if bs is None:
            continue
        if _body_sha(head_src, hs) == _body_sha(base_src, bs):
            if hs.range != bs.range:
                delta.moved.append(_changed(path, hs, head_src))
            continue
        reason = ChangeReason.SIGNATURE if hs.signature != bs.signature else ChangeReason.BODY
        delta.modified.append(_changed(path, hs, head_src, reason=reason))
    return delta


def merge(deltas: Iterable[SymbolDelta]) -> SymbolDelta:
    """Concatenate per-file deltas into one diff-wide delta, order preserved.

    Collapses cross-file relocations: an `added` entry whose
    `(qualified_name, body_sha)` also appears in some other file's
    `removed` is the same code in a new home, so both entries are
    replaced by one `moved` entry carrying `from_path`. Pairing is
    one-to-one in source order.
    """
    out = SymbolDelta()
    for d in deltas:
        out.added.extend(d.added)
        out.removed.extend(d.removed)
        out.modified.extend(d.modified)
        out.moved.extend(d.moved)

    candidates: dict[tuple[str, str], list[ChangedSymbol]] = {}
    for r in out.removed:
        candidates.setdefault((r.qualified_name, r.body_sha), []).append(r)

    relocated: list[ChangedSymbol] = []
    paired: set[int] = set()
    kept_added: list[ChangedSymbol] = []
    for a in out.added:
        queue = candidates.get((a.qualified_name, a.body_sha))
        source = next((r for r in queue if r.path != a.path and id(r) not in paired), None) if queue else None
        if source is None:
            kept_added.append(a)
            continue
        paired.add(id(source))
        relocated.append(a.model_copy(update={"from_path": source.path}))

    out.added = kept_added
    out.removed = [r for r in out.removed if id(r) not in paired]
    out.moved.extend(relocated)
    return out
