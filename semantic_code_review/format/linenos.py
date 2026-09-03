"""Decorate a unified diff with post-image line numbers for LLM prompts.

The augment passes ask the model for post-image (new-side) line
coordinates — same-file `refs[].line`, and the extra-review pass's
`line`. The raw hunk body carries only `+`/`-`/` ` markers, so the model
has to *count* `+` lines from the `@@` header to derive an absolute line
number. It undercounts, and the drift grows with distance from the
header — notes land several lines above their true target.

`number_for_prompt` prepends each body line with its post-image line
number so producing a coordinate is a copy, not a count. Deleted lines
(present pre-image only) and no-newline markers get a blank gutter —
they have no post-image line. File- and hunk-header lines pass through
unchanged. An optional second gutter carries the hunk's boundary ids
(`augment.boundaries`), the names a span's edges are given by. This is a
prompt-only view; the persisted wire/sidecar forms are untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple

# `@@ -old_start[,old_count] +new_start[,new_count] @@ [section]`
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class _Entry(NamedTuple):
    """One output line: `gutter` marks a body line that needs a (possibly
    blank) number column — it disambiguates a `-` body deletion from a
    `--- a/f` file marker, which are both `number=None`.
    """

    gutter: bool
    number: int | None
    raw: str


def number_for_prompt(diff_text: str, boundary_ids: Mapping[int, str] | None = None) -> str:
    r"""Return `diff_text` with a post-image line-number gutter per body line.

    Handles a whole multi-file diff or a single `header + body` block:
    the new-side counter resets at every `@@` header. Lines outside a
    hunk (file headers, `diff --git`, `---`/`+++` markers) pass through
    verbatim.

    Args:
        diff_text: A unified diff (raw, un-annotated). May span multiple
            files and hunks.
        boundary_ids: `post-image line -> id`. When given, a second
            gutter left of the number carries the id of each boundary
            line and is blank elsewhere. For a single hunk only, since
            post-image lines repeat across hunks.

    Returns:
        The same text with each in-hunk line prefixed by right-aligned
        gutters: the post-image line number for context/added lines, blank
        for deleted lines and `\\ No newline` markers.
    """
    # Build entries first so the gutter width can be sized to the largest
    # number actually emitted. `in_hunk` disambiguates a `-`/`+` file
    # marker (pre-hunk, passed through) from a body deletion/addition.
    entries: list[_Entry] = []
    new_line: int | None = None
    max_no = 0
    for line in diff_text.split("\n"):
        m = _HUNK_HEADER.match(line)
        if m:
            new_line = int(m.group(1))
            entries.append(_Entry(gutter=False, number=None, raw=line))
            continue
        if new_line is None:
            entries.append(_Entry(gutter=False, number=None, raw=line))
            continue
        if line == "" or line.startswith(("+", " ")):
            entries.append(_Entry(gutter=True, number=new_line, raw=line))
            max_no = max(max_no, new_line)
            new_line += 1
        elif line.startswith(("-", "\\")):
            # Deletions and the no-newline marker are body lines with no
            # post-image number: a blank gutter keeps them column-aligned.
            entries.append(_Entry(gutter=True, number=None, raw=line))
        else:
            entries.append(_Entry(gutter=False, number=None, raw=line))

    width = len(str(max_no)) if max_no else 1
    ids = boundary_ids or {}
    id_width = max((len(i) for i in ids.values()), default=0)
    out: list[str] = []
    for e in entries:
        if not e.gutter:
            out.append(e.raw)
            continue
        col = str(e.number) if e.number is not None else ""
        numbered = f"{col:>{width}} {e.raw}"
        if id_width:
            bid = ids.get(e.number, "") if e.number is not None else ""
            numbered = f"{bid:<{id_width}} {numbered}"
        out.append(numbered)
    result = "\n".join(out)
    if diff_text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result
