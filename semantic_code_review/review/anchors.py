"""Resolve a viewer comment's line onto an anchor GitHub will accept.

GitHub only threads a review comment to a line inside a diff hunk. The
viewer, by design, shows more than the diff — revealed context, folded
regions — so a reviewer can anchor a comment on a line the API cannot
take. Measured behaviour, on a PR whose only hunk covered new lines
198-204:

- `addPullRequestReviewThread` with a line in the hunk succeeds.
- With a line outside it, the mutation returns HTTP 200, no `errors`,
  and a null thread. Nothing is created and nothing explains why.
- REST is at least explicit: `line ... could not be resolved`.
- The legacy `position` field is an offset *into* the hunk, so it
  cannot address an out-of-hunk line either (position 1 -> line 198).
- `subjectType: FILE` works, but only with `line` omitted entirely;
  passing both fails the same silent way. The thread then carries no
  line at all and renders against the file.

GitHub's own UI can anchor anywhere in a changed file — such comments
come back with an empty `diffHunk` and `isOutdated` set — but no public
API surface exposes that, so it is not reproducible here.

Hence two degradations, preferred in this order: move the comment to
the nearest line inside a hunk (position is most of a review comment's
value), or fall back to a file-level thread. Either way the reader is
told, in the body, where the comment was meant to sit.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

#: `@@ -old,count +new,count @@` — the counts are optional (git omits
#: `,1`), and only the side we anchor against is needed.
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: How far a comment may be moved to reach a hunk before a file-level
#: thread is the more honest answer. A comment a line or two outside a
#: hunk is almost always about that hunk; one fifty lines away is not.
NEAREST_LINE_LIMIT = 10


@dataclasses.dataclass(frozen=True)
class Anchor:
    """Where a comment will actually be posted.

    `line` is None for a file-level thread. `note` is the human-readable
    reason the anchor moved, appended to the body so a reader is never
    silently misdirected; it is None when the anchor was already valid.
    """

    path: str
    line: int | None
    side: str | None
    note: str | None = None

    @property
    def is_file_level(self) -> bool:
        return self.line is None


def postable_ranges(diff_text: str) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """Map `(path, side)` to the line ranges GitHub will thread against.

    Both sides are collected: a comment on a deleted line anchors to
    LEFT, on an added or context line to RIGHT.
    """
    out: dict[tuple[str, str], list[tuple[int, int]]] = {}
    path: str | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:].strip()
            continue
        if raw.startswith("+++ ") and raw[4:].strip() == "/dev/null":
            path = None
            continue
        if not raw.startswith("@@") or path is None:
            continue
        m = _HUNK_HEADER.match(raw)
        if m is None:
            continue
        old_start, old_count = int(m.group(1)), int(m.group(2) or 1)
        new_start, new_count = int(m.group(3)), int(m.group(4) or 1)
        if old_count:
            out.setdefault((path, "LEFT"), []).append((old_start, old_start + old_count - 1))
        if new_count:
            out.setdefault((path, "RIGHT"), []).append((new_start, new_start + new_count - 1))
    return out


def resolve(
    path: str,
    line: int,
    side: str,
    ranges: dict[tuple[str, str], list[tuple[int, int]]],
) -> Anchor:
    """Resolve one comment onto a postable anchor.

    Unchanged when the line already sits in a hunk. Otherwise moved to
    the nearest hunk line within `NEAREST_LINE_LIMIT`, else demoted to a
    file-level thread.
    """
    spans = ranges.get((path, side), [])
    if any(start <= line <= end for start, end in spans):
        return Anchor(path=path, line=line, side=side)

    nearest = _nearest_line(line, spans)
    if nearest is not None and abs(nearest - line) <= NEAREST_LINE_LIMIT:
        return Anchor(
            path=path,
            line=nearest,
            side=side,
            note=f"originally anchored at line {line}, moved to {nearest} — {line} falls outside the diff",
        )
    return Anchor(
        path=path,
        line=None,
        side=None,
        note=f"originally anchored at line {line}, which falls outside the diff — posted against the file",
    )


def _nearest_line(line: int, spans: Iterable[tuple[int, int]]) -> int | None:
    best: int | None = None
    for start, end in spans:
        candidate = start if line < start else end if line > end else line
        if best is None or abs(candidate - line) < abs(best - line):
            best = candidate
    return best


def with_note(body: str, note: str | None) -> str:
    """Append the relocation note to a comment body, if there is one."""
    if not note:
        return body
    return f"{body}\n\n_({note})_"


__all__ = ["NEAREST_LINE_LIMIT", "Anchor", "postable_ranges", "resolve", "with_note"]
