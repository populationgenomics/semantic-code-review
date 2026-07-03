"""Emit a structured `AnnotatedDiff` as augmented-unified-diff text.

Canonical form: deterministic byte output for any structured input, so
`parse(emit(x)) == x` and `emit(parse(y)) == y` when y is already canonical.

Formatting rules:
- Scalar text directives: one line if total length fits `_MAX_WIDTH`; else
  word-wrap at `_WRAP_WIDTH` columns with `#scr>` continuations.
- JSON directives: `json.dumps(..., indent=2)` pretty-printed, first JSON
  line after `#scr: name: `, remaining JSON lines after `#scr> `.
- Directive ordering within each zone matches the spec table in plan §2.2.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from ..augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    Overview,
    Segment,
    Smell,
)

_MAX_WIDTH = 100  # total characters per emitted line
_WRAP_WIDTH = 92  # body width for wrapped text directives


def emit_augmented_diff(diff: AnnotatedDiff) -> str:
    out: list[str] = []
    out.extend(_emit_preamble(diff))
    for f in diff.files:
        out.extend(_emit_file(f))
    return "\n".join(out) + "\n"


def _emit_preamble(diff: AnnotatedDiff) -> list[str]:
    lines: list[str] = []
    lines.extend(_text("scr-version", str(diff.version)))
    lines.extend(_text("scr-pr", diff.pr.pr_url))
    lines.extend(_text("scr-base", diff.pr.base_sha))
    lines.extend(_text("scr-head", diff.pr.head_sha))
    if diff.pr.model:
        lines.extend(_text("scr-model", diff.pr.model))
    if isinstance(diff.overview, Overview):
        lines.extend(_json("scr-overview", _overview_to_jsonable(diff.overview)))
    return lines


def _overview_to_jsonable(ov: Overview) -> dict[str, Any]:
    return {
        "summary": ov.summary,
        "symbols_added": [s.model_dump() for s in ov.symbols_added],
        "symbols_modified": [s.model_dump() for s in ov.symbols_modified],
        "symbols_removed": [s.model_dump() for s in ov.symbols_removed],
        "callgraph_edges": [e.model_dump(by_alias=True) for e in ov.callgraph_edges],
        "themes": list(ov.themes),
    }


def _emit_file(f: AnnotatedFile) -> list[str]:
    lines: list[str] = [f.diff_git_line]
    lines.extend(f.extra_header_lines)
    if f.old_file_marker:
        lines.append(f.old_file_marker)
    if f.new_file_marker:
        lines.append(f.new_file_marker)
    ann = f.ann
    if ann.summary:
        lines.extend(_text("scr-file-summary", ann.summary))
    if ann.role is not None:
        lines.extend(_text("scr-file-role", ann.role.value))
    if ann.lang:
        lines.extend(_text("scr-file-lang", ann.lang))
    if ann.symbols is not None:
        lines.extend(_json("scr-file-symbols", ann.symbols.model_dump()))
    for h in f.hunks:
        lines.extend(_emit_hunk(h))
    return lines


def _emit_hunk(h: AnnotatedHunk) -> list[str]:
    lines: list[str] = [h.parsed.header]
    body = h.parsed.body
    if body.endswith("\n"):
        body = body[:-1]
    if body:
        lines.extend(body.split("\n"))
    ann = h.ann
    if ann.intent:
        lines.extend(_text("scr-hunk-intent", ann.intent))
    for s in ann.smells:
        lines.extend(_text("scr-hunk-smell", _smell_value(s)))
    if ann.context:
        lines.extend(_text("scr-hunk-context", ann.context))
    if ann.refs:
        lines.extend(_json("scr-hunk-refs", [r.model_dump() for r in ann.refs]))
    if ann.confidence is not None:
        lines.extend(_text("scr-hunk-confidence", str(ann.confidence)))
    for seg in ann.segments:
        lines.extend(_emit_segment(seg))
    for fd in ann.fold_descriptions:
        if fd.context == "right":
            body = f'right {fd.right_start}..{fd.right_end} "{fd.summary}"'
        elif fd.context == "left":
            body = f'left {fd.left_start}..{fd.left_end} "{fd.summary}"'
        else:  # both
            body = (
                f'both R{fd.right_start}..{fd.right_end} '
                f'L{fd.left_start}..{fd.left_end} "{fd.summary}"'
            )
        lines.extend(_text("scr-fold", body))
    for ln in ann.line_notes:
        lines.extend(_text("scr-line", f'+{ln.line} "{ln.body}"'))
    return lines


def _emit_segment(seg: Segment) -> list[str]:
    lines: list[str] = []
    end = seg.new_start + seg.new_count - 1
    lines.extend(_text("scr-segment-begin", f"+{seg.new_start}..+{end}"))
    if seg.intent:
        lines.extend(_text("scr-segment-intent", seg.intent))
    for s in seg.smells:
        lines.extend(_text("scr-segment-smell", _smell_value(s)))
    if seg.context:
        lines.extend(_text("scr-segment-context", seg.context))
    if seg.refs:
        lines.extend(_json("scr-segment-refs", [r.model_dump() for r in seg.refs]))
    lines.append("#scr: scr-segment-end")
    return lines


def _smell_value(s: Smell) -> str:
    if s.note:
        return f'{s.tag} "{s.note}"'
    return s.tag


def _text(name: str, value: str) -> list[str]:
    """Emit a text directive with optional #scr> wrapping for long values."""
    if value == "":
        return [f"#scr: {name}"]
    first = f"#scr: {name}: {value}"
    if "\n" not in value and len(first) <= _MAX_WIDTH:
        return [first]
    wrapped = textwrap.wrap(value, width=_WRAP_WIDTH, break_long_words=False, break_on_hyphens=False)
    if not wrapped:
        return [f"#scr: {name}:"]
    lines = [f"#scr: {name}: {wrapped[0]}"]
    for w in wrapped[1:]:
        lines.append(f"#scr>   {w}")
    return lines


def _json(name: str, value: Any) -> list[str]:
    text = json.dumps(value, indent=2, ensure_ascii=False)
    text_lines = text.split("\n")
    out = [f"#scr: {name}: {text_lines[0]}"]
    for tl in text_lines[1:]:
        out.append(f"#scr> {tl}")
    return out
