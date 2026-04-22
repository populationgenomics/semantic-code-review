r"""Parse an augmented unified diff into structured form.

Grammar, by zone:

- Preamble: a run of `#scr:` directives before the first `diff --git`.
- File header: `diff --git` line, optional git metadata lines (index, rename,
  new file mode), then `---` / `+++` markers. `#scr:` directives anywhere in
  this zone attach to the file.
- Hunk body: lines starting with ` `, `+`, `-`, or `\` following a `@@` header.
- Hunk trailer: `#scr:` directives between a hunk's body and the next
  `@@` / `diff --git` / EOF; attach to the hunk just closed.

Within a trailer, `scr-segment-begin` ... `scr-segment-end` brackets enclose
per-segment directives.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator

from ..augment.schemas import (
    AugmentedDiff,
    FilePatch,
    FileRole,
    FileSymbols,
    FoldDescription,
    Hunk,
    LineNote,
    Overview,
    OverviewEdge,
    OverviewSymbol,
    PRInfo,
    Ref,
    Segment,
    Smell,
)


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@"
)
_SEGMENT_RANGE_RE = re.compile(r"^\s*\+(\d+)\.\.\+(\d+)\s*$")
_FOLD_RE = re.compile(r'^\s*\+(\d+)\.\.\+(\d+)\s+(?:"(.*)"|(.+))\s*$')
_LINE_NOTE_RE = re.compile(r'^\s*\+(\d+)\s+(?:"(.*)"|(.+))\s*$')
_SMELL_RE = re.compile(r'^\s*(\S+)(?:\s+"(.*)")?\s*$')
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


class ParseError(ValueError):
    """Raised when the augmented diff is malformed."""


@dataclass
class _Directive:
    name: str
    value: str
    lineno: int  # 1-indexed original-file line for error messages


def _directives(annotation_lines: list[tuple[int, str]]) -> list[_Directive]:
    """Coalesce `#scr:` / `#scr>` lines into (name, value) pairs.

    `annotation_lines` is a list of (1-indexed lineno, raw_line) pairs, in
    original order, containing only annotation lines.
    """
    out: list[_Directive] = []
    cur: _Directive | None = None
    for lineno, line in annotation_lines:
        if line.startswith("#scr: "):
            if cur is not None:
                cur.value = cur.value.rstrip()
                out.append(cur)
            rest = line[len("#scr: ") :]
            if ":" in rest:
                name, _, value = rest.partition(":")
                cur = _Directive(name=name.strip(), value=value.strip(), lineno=lineno)
            else:
                cur = _Directive(name=rest.strip(), value="", lineno=lineno)
        elif line == "#scr:":
            raise ParseError(f"line {lineno}: bare '#scr:' with no directive")
        elif line.startswith("#scr>") or line == "#scr>":
            if cur is None:
                raise ParseError(f"line {lineno}: '#scr>' continuation without a preceding '#scr:' directive")
            cont = line[len("#scr>") :].strip()
            cur.value = (cur.value + " " + cont).strip() if cur.value else cont
        else:
            raise ParseError(f"line {lineno}: expected '#scr:' or '#scr>', got {line!r}")
    if cur is not None:
        cur.value = cur.value.rstrip()
        out.append(cur)
    return out


def _is_annotation(line: str) -> bool:
    return line.startswith("#scr:") or line.startswith("#scr>")


def _is_body_line(line: str) -> bool:
    # Hunk body lines are ' ', '+', '-', or '\'. Empty line counts as a
    # context line with no trailing char (many diffs emit bare blank lines).
    if line == "":
        return True
    return line[0] in " +-\\"


def _parse_smell(value: str, lineno: int) -> Smell:
    m = _SMELL_RE.match(value)
    if not m:
        raise ParseError(f"line {lineno}: malformed smell value {value!r}")
    tag, note = m.group(1), (m.group(2) or "")
    return Smell(tag=tag, note=note)


def _parse_line_note(value: str, lineno: int) -> LineNote:
    m = _LINE_NOTE_RE.match(value)
    if not m:
        raise ParseError(f"line {lineno}: malformed scr-line value {value!r}")
    line = int(m.group(1))
    body = m.group(2) if m.group(2) is not None else (m.group(3) or "")
    return LineNote(line=line, body=body)


def _parse_fold(value: str, lineno: int) -> FoldDescription:
    m = _FOLD_RE.match(value)
    if not m:
        raise ParseError(f"line {lineno}: malformed scr-fold value {value!r}")
    start = int(m.group(1))
    end = int(m.group(2))
    summary = m.group(3) if m.group(3) is not None else (m.group(4) or "")
    if end < start:
        raise ParseError(f"line {lineno}: fold end +{end} before start +{start}")
    return FoldDescription(new_start=start, new_count=end - start + 1, summary=summary)


def _parse_refs(value: str, lineno: int) -> list[Ref]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as e:
        raise ParseError(f"line {lineno}: invalid JSON in refs: {e}") from e
    return [Ref(**r) for r in data]


def _apply_preamble(directives: list[_Directive]) -> tuple[PRInfo, Overview | None, int]:
    pr_url = base_sha = head_sha = ""
    model = ""
    version = 1
    overview: Overview | None = None
    for d in directives:
        if d.name == "scr-version":
            version = int(d.value)
        elif d.name == "scr-pr":
            pr_url = d.value
        elif d.name == "scr-base":
            base_sha = d.value
        elif d.name == "scr-head":
            head_sha = d.value
        elif d.name == "scr-model":
            model = d.value
        elif d.name == "scr-overview":
            try:
                data = json.loads(d.value)
            except json.JSONDecodeError as e:
                raise ParseError(f"line {d.lineno}: invalid JSON in scr-overview: {e}") from e
            overview = _build_overview(data)
        else:
            raise ParseError(f"line {d.lineno}: unknown preamble directive {d.name!r}")
    pr = PRInfo(pr_url=pr_url, base_sha=base_sha, head_sha=head_sha, model=model)
    return pr, overview, version


def _build_overview(data: dict) -> Overview:
    return Overview(
        summary=data.get("summary", ""),
        symbols_added=[OverviewSymbol(**s) for s in data.get("symbols_added", [])],
        symbols_modified=[OverviewSymbol(**s) for s in data.get("symbols_modified", [])],
        symbols_removed=[OverviewSymbol(**s) for s in data.get("symbols_removed", [])],
        callgraph_edges=[OverviewEdge.model_validate(e) for e in data.get("callgraph_edges", [])],
        themes=list(data.get("themes", [])),
    )


def _apply_file_header(fp: FilePatch, directives: list[_Directive]) -> None:
    for d in directives:
        if d.name == "scr-file-summary":
            fp.summary = d.value
        elif d.name == "scr-file-role":
            fp.role = FileRole(d.value)
        elif d.name == "scr-file-lang":
            fp.lang = d.value
        elif d.name == "scr-file-symbols":
            try:
                data = json.loads(d.value)
            except json.JSONDecodeError as e:
                raise ParseError(f"line {d.lineno}: invalid JSON in scr-file-symbols: {e}") from e
            fp.symbols = FileSymbols(**data)
        else:
            raise ParseError(f"line {d.lineno}: unknown file-header directive {d.name!r}")


def _apply_hunk_trailer(hunk: Hunk, directives: list[_Directive]) -> None:
    it = iter(directives)
    for d in it:
        if d.name == "scr-hunk-intent":
            hunk.intent = d.value
        elif d.name == "scr-hunk-smell":
            hunk.smells.append(_parse_smell(d.value, d.lineno))
        elif d.name == "scr-hunk-context":
            hunk.context = d.value
        elif d.name == "scr-hunk-refs":
            hunk.refs = _parse_refs(d.value, d.lineno)
        elif d.name == "scr-hunk-confidence":
            hunk.confidence = int(d.value)
        elif d.name == "scr-line":
            hunk.line_notes.append(_parse_line_note(d.value, d.lineno))
        elif d.name == "scr-fold":
            hunk.fold_descriptions.append(_parse_fold(d.value, d.lineno))
        elif d.name == "scr-segment-begin":
            hunk.segments.append(_consume_segment(d, it, hunk))
        elif d.name in {"scr-segment-intent", "scr-segment-smell", "scr-segment-context",
                        "scr-segment-refs", "scr-segment-end"}:
            raise ParseError(f"line {d.lineno}: {d.name} outside of a scr-segment-begin/end block")
        else:
            raise ParseError(f"line {d.lineno}: unknown hunk directive {d.name!r}")

    # Validate segment non-overlap and ordering.
    last_end = -1
    for s in hunk.segments:
        start, end = s.new_start, s.new_start + s.new_count - 1
        if start <= last_end:
            raise ParseError(f"segment +{start}..+{end} overlaps previous segment (ends +{last_end})")
        last_end = end


def _consume_segment(begin: _Directive, it: Iterator[_Directive], hunk: Hunk) -> Segment:
    m = _SEGMENT_RANGE_RE.match(begin.value)
    if not m:
        raise ParseError(f"line {begin.lineno}: malformed segment range {begin.value!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if end < start:
        raise ParseError(f"line {begin.lineno}: segment end +{end} before start +{start}")
    hunk_new_end = hunk.new_start + hunk.new_count - 1
    if start < hunk.new_start or end > hunk_new_end:
        raise ParseError(
            f"line {begin.lineno}: segment +{start}..+{end} outside hunk range "
            f"+{hunk.new_start}..+{hunk_new_end}"
        )
    seg = Segment(new_start=start, new_count=end - start + 1)
    for d in it:
        if d.name == "scr-segment-end":
            return seg
        if d.name == "scr-segment-begin":
            raise ParseError(f"line {d.lineno}: nested scr-segment-begin")
        if d.name == "scr-segment-intent":
            seg.intent = d.value
        elif d.name == "scr-segment-smell":
            seg.smells.append(_parse_smell(d.value, d.lineno))
        elif d.name == "scr-segment-context":
            seg.context = d.value
        elif d.name == "scr-segment-refs":
            seg.refs = _parse_refs(d.value, d.lineno)
        else:
            raise ParseError(f"line {d.lineno}: directive {d.name!r} not allowed inside a segment block")
    raise ParseError(f"line {begin.lineno}: scr-segment-begin without matching scr-segment-end")


def _parse_hunk_header(line: str, lineno: int) -> tuple[int, int, int, int]:
    m = _HUNK_HEADER_RE.match(line)
    if not m:
        raise ParseError(f"line {lineno}: malformed hunk header {line!r}")
    os = int(m.group("os"))
    oc = int(m.group("oc")) if m.group("oc") is not None else 1
    ns = int(m.group("ns"))
    nc = int(m.group("nc")) if m.group("nc") is not None else 1
    return os, oc, ns, nc


def parse_augmented_diff(text: str) -> AugmentedDiff:
    lines = text.split("\n")
    # Strip a trailing empty string if text ended with newline, for convenience.
    if lines and lines[-1] == "":
        lines = lines[:-1]

    i = 0
    n = len(lines)

    # --- Preamble -----------------------------------------------------------
    pre_annos: list[tuple[int, str]] = []
    while i < n and not lines[i].startswith("diff --git "):
        line = lines[i]
        if _is_annotation(line):
            pre_annos.append((i + 1, line))
        elif line.strip() == "":
            pass  # blank line allowed in preamble
        else:
            raise ParseError(f"line {i + 1}: unexpected content before first diff: {line!r}")
        i += 1

    pr, overview, _version = _apply_preamble(_directives(pre_annos))

    # --- Files --------------------------------------------------------------
    files: list[FilePatch] = []
    while i < n:
        if not lines[i].startswith("diff --git "):
            # After a hunk body, trailing blank lines or unexpected noise — skip blanks.
            if lines[i].strip() == "":
                i += 1
                continue
            raise ParseError(f"line {i + 1}: expected 'diff --git', got {lines[i]!r}")

        fp, i = _parse_file(lines, i)
        files.append(fp)

    return AugmentedDiff(version=1, pr=pr, overview=overview, files=files)


def _parse_file(lines: list[str], i: int) -> tuple[FilePatch, int]:
    n = len(lines)
    diff_git = lines[i]
    m = _DIFF_GIT_RE.match(diff_git)
    if not m:
        raise ParseError(f"line {i + 1}: malformed 'diff --git' line: {diff_git!r}")
    old_path, new_path = m.group(1), m.group(2)
    fp = FilePatch(
        path=new_path,
        old_path=old_path if old_path != new_path else None,
        diff_git_line=diff_git,
    )
    i += 1

    header_annos: list[tuple[int, str]] = []
    while i < n:
        line = lines[i]
        if line.startswith("@@ "):
            break
        if line.startswith("diff --git "):
            break
        if _is_annotation(line):
            header_annos.append((i + 1, line))
        elif line.startswith("--- "):
            fp.old_file_marker = line
        elif line.startswith("+++ "):
            fp.new_file_marker = line
        else:
            fp.extra_header_lines.append(line)
        i += 1

    _apply_file_header(fp, _directives(header_annos))

    while i < n and lines[i].startswith("@@ "):
        hunk, i = _parse_hunk(lines, i)
        fp.hunks.append(hunk)

    return fp, i


def _parse_hunk(lines: list[str], i: int) -> tuple[Hunk, int]:
    n = len(lines)
    header = lines[i]
    os, oc, ns, nc = _parse_hunk_header(header, i + 1)
    hunk = Hunk(header=header, old_start=os, old_count=oc, new_start=ns, new_count=nc)
    i += 1

    # Consume body lines until a transition trigger.
    body: list[str] = []
    old_consumed = 0
    new_consumed = 0
    while i < n:
        line = lines[i]
        if line.startswith("@@ ") or line.startswith("diff --git "):
            break
        if _is_annotation(line):
            break
        if not _is_body_line(line):
            raise ParseError(
                f"line {i + 1}: unexpected hunk body line {line!r} "
                f"(must start with ' ', '+', '-', or '\\')"
            )
        body.append(line)
        c = line[:1]
        if c == " ":
            old_consumed += 1
            new_consumed += 1
        elif c == "-":
            old_consumed += 1
        elif c == "+":
            new_consumed += 1
        # '\' is the no-newline marker; doesn't count.
        i += 1

    if old_consumed != oc or new_consumed != nc:
        raise ParseError(
            f"hunk at line {i}: counts do not match header "
            f"(got old={old_consumed}, new={new_consumed}; expected old={oc}, new={nc})"
        )

    hunk.body = "\n".join(body) + ("\n" if body else "")

    # Trailer: annotations until next @@ / diff --git / EOF.
    trailer_annos: list[tuple[int, str]] = []
    while i < n:
        line = lines[i]
        if line.startswith("@@ ") or line.startswith("diff --git "):
            break
        if _is_annotation(line):
            trailer_annos.append((i + 1, line))
            i += 1
            continue
        if line.strip() == "":
            i += 1
            continue
        raise ParseError(f"line {i + 1}: unexpected content in hunk trailer: {line!r}")

    _apply_hunk_trailer(hunk, _directives(trailer_annos))
    return hunk, i
