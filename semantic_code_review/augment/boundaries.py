"""Boundary lines: the post-image lines a span may start or end on (ADR 0008).

The per-hunk pass does not emit line numbers. Each hunk's prompt carries a
numbered list of *boundary lines*, and a span is a pair of those ids. The
ids are unforgeable — an id the list does not carry resolves to nothing —
so a span cannot overshoot the hunk, land in pre-image coordinates, or
have an inverted extent by arithmetic. The extent stays the model's: any
pair of boundaries is a span.

A post-image line of a hunk is a boundary when it is any of:

- the hunk's first or last post-image line;
- a changed (`+`) line, or the line a run of deletions lands on — so
  every line a note could be anchored to is reachable, and changed lines
  with no structural edge between them can still be grouped;
- the first or last line of a definition (tree-sitter, head side), where
  the language has a grammar;
- the first or last line of an indentation block, or of a run of
  non-blank lines — blocks and paragraphs stand in for AST nodes where
  there is no grammar, and approximate statement groups where there is.

A deletion-only hunk has no post-image lines and therefore no
boundaries: the hunk's own `intent` is its annotation.

Ids are `b<n>`, numbered from 1 per prompt. A batched prompt numbers its
hunks continuously (`for_batch`), so an id names one line in the whole
call and a span filed under the wrong `hunk_index` fails to resolve
rather than landing on a different hunk's line.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from ..viewer import fold_regions, hunk_layout
from .schemas import ParsedHunk

ID_PREFIX = "b"


@dataclasses.dataclass(frozen=True)
class Boundaries:
    """One hunk's boundary list: sorted post-image lines with their ids.

    `lines[i]` carries id `b{first_id + i}`. Empty for a deletion-only
    hunk.
    """

    lines: tuple[int, ...]
    first_id: int = 1
    #: The definitions the hunk touches, each as a boundary pair
    #: (`structure_lines`), for the prompt.
    structure: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if list(self.lines) != sorted(set(self.lines)):
            raise ValueError(f"boundary lines must be sorted and unique: {self.lines}")
        if self.first_id < 1:
            raise ValueError(f"first_id must be positive: {self.first_id}")

    @classmethod
    def for_hunk(
        cls,
        parsed: ParsedHunk,
        head_spans: Sequence[Mapping[str, Any]] = (),
        *,
        first_id: int = 1,
    ) -> Boundaries:
        """Compute the boundaries of `parsed`.

        Args:
            parsed: The hunk.
            head_spans: The file's head-side definition spans as
                `viewer.build_json.file_fold_spans` flattens them
                (`start_line` / `end_line` / `kind` / `qualified_name`);
                empty when the language has no grammar or the worktree
                is unavailable.
            first_id: The number of the first id, for continuous numbering
                across a batch.
        """
        bounds = cls(tuple(boundary_lines(parsed, head_spans)), first_id)
        return dataclasses.replace(bounds, structure=tuple(structure_lines(bounds, head_spans)))

    @property
    def next_id(self) -> int:
        """The first id number free after this list."""
        return self.first_id + len(self.lines)

    def id_for(self, line: int) -> str:
        """The id of boundary `line`.

        Raises:
            KeyError: If `line` is not a boundary.
        """
        try:
            return f"{ID_PREFIX}{self.first_id + self.lines.index(line)}"
        except ValueError:
            raise KeyError(line) from None

    def line_for(self, boundary_id: str) -> int | None:
        """The post-image line `boundary_id` names, or None for an id not in the list."""
        if not boundary_id.startswith(ID_PREFIX) or not boundary_id[len(ID_PREFIX) :].isdigit():
            return None
        index = int(boundary_id[len(ID_PREFIX) :]) - self.first_id
        if not 0 <= index < len(self.lines):
            return None
        return self.lines[index]

    def gutter(self) -> dict[int, str]:
        """`line -> id`, the column `format.linenos.number_for_prompt` prints."""
        return {line: self.id_for(line) for line in self.lines}


def for_batch(
    hunks: Sequence[tuple[int, ParsedHunk]],
    head_spans: Sequence[Mapping[str, Any]] = (),
) -> dict[int, Boundaries]:
    """Boundaries for every hunk of one batched call, numbered continuously.

    Keyed by the hunk's index within its file — the `hunk_index` the
    batch payload addresses hunks by.
    """
    out: dict[int, Boundaries] = {}
    next_id = 1
    for index, parsed in hunks:
        out[index] = Boundaries.for_hunk(parsed, head_spans, first_id=next_id)
        next_id = out[index].next_id
    return out


@dataclasses.dataclass(frozen=True)
class _PostImage:
    """A hunk's post-image lines: text by line, plus the lines a note can anchor to."""

    text: dict[int, str]
    anchors: set[int]  # `+` lines and the line after a run of deletions


def _post_image(parsed: ParsedHunk) -> _PostImage:
    text: dict[int, str] = {}
    anchors: set[int] = set()
    line = parsed.new_start
    after_deletion = False
    for raw in parsed.body.splitlines():
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        if raw.startswith("-"):
            after_deletion = True
            continue
        text[line] = raw[1:] if raw else ""
        if raw.startswith("+") or after_deletion:
            anchors.add(line)
        after_deletion = False
        line += 1
    return _PostImage(text, anchors)


def boundary_lines(parsed: ParsedHunk, head_spans: Sequence[Mapping[str, Any]] = ()) -> list[int]:
    """The sorted boundary lines of `parsed`; empty for a deletion-only hunk."""
    post = _post_image(parsed)
    if not post.text:
        return []
    ordered = sorted(post.text)
    first, last = ordered[0], ordered[-1]
    out: set[int] = {first, last, *post.anchors}
    for span in head_spans:
        for edge in (span["start_line"], span["end_line"]):
            if first <= edge <= last:
                out.add(edge)
    texts = [post.text[line] for line in ordered]
    for start, end in fold_regions.indent_regions(texts):
        out.update((ordered[start - 1], ordered[end - 1]))
    out.update(_run_edges(ordered, texts))
    return sorted(out)


def _run_edges(lines: Sequence[int], texts: Sequence[str]) -> set[int]:
    """First and last line of every run of non-blank lines."""
    out: set[int] = set()
    in_run = False
    for i, text in enumerate(texts):
        blank = hunk_layout.text_indent(text) == -1
        if not blank and not in_run:
            out.add(lines[i])
            in_run = True
        elif blank and in_run:
            out.add(lines[i - 1])
            in_run = False
    if in_run:
        out.add(lines[-1])
    return out


def structure_lines(
    bounds: Boundaries,
    head_spans: Sequence[Mapping[str, Any]],
) -> list[str]:
    """The definitions a hunk touches, each as a boundary pair.

    One entry per head-side definition whose range meets the hunk's
    post-image lines, clipped to the hunk (the hunk's edges are
    boundaries). Gives the model the pair for "this whole function"
    without reading it off the gutter.
    """
    if not bounds.lines:
        return []
    first, last = bounds.lines[0], bounds.lines[-1]
    out: list[str] = []
    for span in head_spans:
        start, end = span["start_line"], span["end_line"]
        if end < first or start > last:
            continue
        clipped = start < first or end > last
        pair = f"{bounds.id_for(max(start, first))}..{bounds.id_for(min(end, last))}"
        note = " (continues past the hunk)" if clipped else ""
        out.append(f"{span['kind']} {span['qualified_name']}: {pair}{note}")
    return out
