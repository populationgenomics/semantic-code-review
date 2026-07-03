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

Two entry points:

- `parse_augmented_diff(text) -> AnnotatedDiff` — the universal parser.
  Handles raw or annotated input; missing annotations are left at empty
  defaults, with the diff-level `overview` defaulting to `SkippedOverview`.
- `parse_raw_diff(text) -> ParsedDiff` — strict raw form. Errors if any
  `#scr:` annotation directives are present beyond the bare PR-info
  preamble.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    FileAnnotations,
    FileRole,
    FileSymbols,
    FoldDescription,
    HunkAnnotations,
    LineNote,
    Overview,
    OverviewEdge,
    OverviewSymbol,
    ParsedDiff,
    ParsedFile,
    ParsedHunk,
    PRInfo,
    Ref,
    Segment,
    SkippedOverview,
    Smell,
    lift_file,
)

_HUNK_HEADER_RE = re.compile(r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@")
_SEGMENT_RANGE_RE = re.compile(r"^\s*\+(\d+)\.\.\+(\d+)\s*$")
_FOLD_RIGHT_RE = re.compile(r'^\s*right\s+(\d+)\.\.(\d+)\s+(?:"(.*)"|(.+))\s*$')
_FOLD_LEFT_RE = re.compile(r'^\s*left\s+(\d+)\.\.(\d+)\s+(?:"(.*)"|(.+))\s*$')
_FOLD_BOTH_RE = re.compile(r'^\s*both\s+R(\d+)\.\.(\d+)\s+L(\d+)\.\.(\d+)\s+(?:"(.*)"|(.+))\s*$')
_LINE_NOTE_RE = re.compile(r'^\s*\+(\d+)\s+(?:"(.*)"|(.+))\s*$')
_SMELL_RE = re.compile(r'^\s*(\S+)(?:\s+"(.*)")?\s*$')
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


# Directives that supply PRInfo. They are always allowed by `parse_raw_diff`
# because the augment pipeline writes them onto the raw diff before the
# annotation passes run.
_PR_PREAMBLE_DIRECTIVES = frozenset({"scr-version", "scr-pr", "scr-base", "scr-head", "scr-model"})


class ParseError(ValueError):
    """Raised when the augmented diff is malformed."""


@dataclass
class _Directive:
    name: str
    value: str
    lineno: int  # 1-indexed original-file line for error messages


@dataclass
class _ParsedFileWithAnno:
    """Raw parser output for one file: the structural ParsedFile plus the
    raw directives observed in its header and per-hunk trailers, retained
    in lineno order so we can either fold them into annotations or reject
    them (parse_raw_diff).
    """

    parsed: ParsedFile
    header_directives: list[_Directive] = field(default_factory=list)
    hunk_directives: list[list[_Directive]] = field(default_factory=list)


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
    return line.startswith(("#scr:", "#scr>"))


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
    """Parse a `#scr:hunk-fold` directive value.

    Three forms — one per context — to keep the syntax unambiguous:

        right 10..25 "summary text"     (post-image only)
        left 8..22 "summary text"       (pre-image deletion fold)
        both R10..25 L8..22 "summary"   (fold straddles changed content)
    """
    m = _FOLD_RIGHT_RE.match(value)
    if m:
        start = int(m.group(1))
        end = int(m.group(2))
        summary = m.group(3) if m.group(3) is not None else (m.group(4) or "")
        if end < start:
            raise ParseError(f"line {lineno}: fold end {end} before start {start}")
        return FoldDescription(
            context="right",
            right_start=start,
            right_end=end,
            summary=summary,
        )
    m = _FOLD_LEFT_RE.match(value)
    if m:
        start = int(m.group(1))
        end = int(m.group(2))
        summary = m.group(3) if m.group(3) is not None else (m.group(4) or "")
        if end < start:
            raise ParseError(f"line {lineno}: fold end {end} before start {start}")
        return FoldDescription(
            context="left",
            left_start=start,
            left_end=end,
            summary=summary,
        )
    m = _FOLD_BOTH_RE.match(value)
    if m:
        rs = int(m.group(1))
        re_ = int(m.group(2))
        ls = int(m.group(3))
        le = int(m.group(4))
        summary = m.group(5) if m.group(5) is not None else (m.group(6) or "")
        if re_ < rs or le < ls:
            raise ParseError(f"line {lineno}: fold has end before start")
        return FoldDescription(
            context="both",
            right_start=rs,
            right_end=re_,
            left_start=ls,
            left_end=le,
            summary=summary,
        )
    raise ParseError(f"line {lineno}: malformed scr-fold value {value!r}")


def _parse_refs(value: str, lineno: int) -> list[Ref]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as e:
        raise ParseError(f"line {lineno}: invalid JSON in refs: {e}") from e
    return [Ref(**r) for r in data]


@dataclass
class _PreambleResult:
    pr: PRInfo
    overview: Overview | None
    version: int


def _apply_preamble(directives: list[_Directive]) -> _PreambleResult:
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
    return _PreambleResult(pr=pr, overview=overview, version=version)


def _build_overview(data: dict) -> Overview:
    return Overview(
        summary=data.get("summary", ""),
        symbols_added=[OverviewSymbol(**s) for s in data.get("symbols_added", [])],
        symbols_modified=[OverviewSymbol(**s) for s in data.get("symbols_modified", [])],
        symbols_removed=[OverviewSymbol(**s) for s in data.get("symbols_removed", [])],
        callgraph_edges=[OverviewEdge.model_validate(e) for e in data.get("callgraph_edges", [])],
        themes=list(data.get("themes", [])),
    )


def _file_annotations(directives: list[_Directive]) -> FileAnnotations:
    ann = FileAnnotations()
    for d in directives:
        if d.name == "scr-file-summary":
            ann.summary = d.value
        elif d.name == "scr-file-role":
            ann.role = FileRole(d.value)
        elif d.name == "scr-file-lang":
            ann.lang = d.value
        elif d.name == "scr-file-symbols":
            try:
                data = json.loads(d.value)
            except json.JSONDecodeError as e:
                raise ParseError(f"line {d.lineno}: invalid JSON in scr-file-symbols: {e}") from e
            ann.symbols = FileSymbols(**data)
        else:
            raise ParseError(f"line {d.lineno}: unknown file-header directive {d.name!r}")
    return ann


def _hunk_annotations(parsed_hunk: ParsedHunk, directives: list[_Directive]) -> HunkAnnotations:
    ann = HunkAnnotations(intent="")
    it = iter(directives)
    for d in it:
        if d.name == "scr-hunk-intent":
            ann.intent = d.value
        elif d.name == "scr-hunk-smell":
            ann.smells.append(_parse_smell(d.value, d.lineno))
        elif d.name == "scr-hunk-context":
            ann.context = d.value
        elif d.name == "scr-hunk-refs":
            ann.refs = _parse_refs(d.value, d.lineno)
        elif d.name == "scr-hunk-confidence":
            ann.confidence = int(d.value)
        elif d.name == "scr-line":
            ann.line_notes.append(_parse_line_note(d.value, d.lineno))
        elif d.name == "scr-fold":
            ann.fold_descriptions.append(_parse_fold(d.value, d.lineno))
        elif d.name == "scr-segment-begin":
            ann.segments.append(_consume_segment(d, it, parsed_hunk))
        elif d.name in {
            "scr-segment-intent",
            "scr-segment-smell",
            "scr-segment-context",
            "scr-segment-refs",
            "scr-segment-end",
        }:
            raise ParseError(f"line {d.lineno}: {d.name} outside of a scr-segment-begin/end block")
        else:
            raise ParseError(f"line {d.lineno}: unknown hunk directive {d.name!r}")

    # Validate segment non-overlap and ordering.
    last_end = -1
    for s in ann.segments:
        start, end = s.new_start, s.new_start + s.new_count - 1
        if start <= last_end:
            raise ParseError(f"segment +{start}..+{end} overlaps previous segment (ends +{last_end})")
        last_end = end
    return ann


def _consume_segment(begin: _Directive, it: Iterator[_Directive], parsed_hunk: ParsedHunk) -> Segment:
    m = _SEGMENT_RANGE_RE.match(begin.value)
    if not m:
        raise ParseError(f"line {begin.lineno}: malformed segment range {begin.value!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if end < start:
        raise ParseError(f"line {begin.lineno}: segment end +{end} before start +{start}")
    hunk_new_end = parsed_hunk.new_start + parsed_hunk.new_count - 1
    if start < parsed_hunk.new_start or end > hunk_new_end:
        raise ParseError(
            f"line {begin.lineno}: segment +{start}..+{end} outside hunk range "
            f"+{parsed_hunk.new_start}..+{hunk_new_end}"
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


@dataclass
class _RawParse:
    """Output of the line-level scanner, before annotations are interpreted."""

    preamble: _PreambleResult
    files: list[_ParsedFileWithAnno]


def _scan(text: str) -> _RawParse:
    lines = text.split("\n")
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

    preamble = _apply_preamble(_directives(pre_annos))

    # --- Files --------------------------------------------------------------
    files: list[_ParsedFileWithAnno] = []
    while i < n:
        if not lines[i].startswith("diff --git "):
            if lines[i].strip() == "":
                i += 1
                continue
            raise ParseError(f"line {i + 1}: expected 'diff --git', got {lines[i]!r}")
        entry, i = _scan_file(lines, i)
        files.append(entry)

    return _RawParse(preamble=preamble, files=files)


def _scan_file(lines: list[str], i: int) -> tuple[_ParsedFileWithAnno, int]:
    n = len(lines)
    diff_git = lines[i]
    m = _DIFF_GIT_RE.match(diff_git)
    if not m:
        raise ParseError(f"line {i + 1}: malformed 'diff --git' line: {diff_git!r}")
    old_path, new_path = m.group(1), m.group(2)
    parsed = ParsedFile(
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
            parsed.old_file_marker = line
        elif line.startswith("+++ "):
            parsed.new_file_marker = line
        else:
            parsed.extra_header_lines.append(line)
        i += 1

    header_directives = _directives(header_annos)

    hunk_directives: list[list[_Directive]] = []
    while i < n and lines[i].startswith("@@ "):
        ph, ds, i = _scan_hunk(lines, i)
        parsed.hunks.append(ph)
        hunk_directives.append(ds)

    return _ParsedFileWithAnno(
        parsed=parsed,
        header_directives=header_directives,
        hunk_directives=hunk_directives,
    ), i


def _scan_hunk(lines: list[str], i: int) -> tuple[ParsedHunk, list[_Directive], int]:
    n = len(lines)
    header = lines[i]
    os, oc, ns, nc = _parse_hunk_header(header, i + 1)
    i += 1

    body: list[str] = []
    old_consumed = 0
    new_consumed = 0
    while i < n:
        line = lines[i]
        if line.startswith(("@@ ", "diff --git ")):
            break
        if _is_annotation(line):
            break
        if not _is_body_line(line):
            raise ParseError(
                f"line {i + 1}: unexpected hunk body line {line!r} (must start with ' ', '+', '-', or '\\')"
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

    parsed_hunk = ParsedHunk(
        header=header,
        old_start=os,
        old_count=oc,
        new_start=ns,
        new_count=nc,
        body="\n".join(body) + ("\n" if body else ""),
    )

    trailer_annos: list[tuple[int, str]] = []
    while i < n:
        line = lines[i]
        if line.startswith(("@@ ", "diff --git ")):
            break
        if _is_annotation(line):
            trailer_annos.append((i + 1, line))
            i += 1
            continue
        if line.strip() == "":
            i += 1
            continue
        raise ParseError(f"line {i + 1}: unexpected content in hunk trailer: {line!r}")

    return parsed_hunk, _directives(trailer_annos), i


def parse_augmented_diff(text: str) -> AnnotatedDiff:
    """Parse augmented or raw diff text into an `AnnotatedDiff`.

    Missing annotations are left at empty defaults; if the input has no
    `scr-overview` directive, `overview` is `SkippedOverview()`.
    """
    raw = _scan(text)

    files: list[AnnotatedFile] = []
    for entry in raw.files:
        file_ann = _file_annotations(entry.header_directives)
        hunks = [
            AnnotatedHunk(parsed=ph, ann=_hunk_annotations(ph, ds))
            for ph, ds in zip(entry.parsed.hunks, entry.hunk_directives, strict=True)
        ]
        files.append(lift_file(entry.parsed, ann=file_ann, hunks=hunks))

    overview = raw.preamble.overview if raw.preamble.overview is not None else SkippedOverview()
    return AnnotatedDiff(
        version=raw.preamble.version,
        pr=raw.preamble.pr,
        overview=overview,
        files=files,
    )


def parse_raw_diff(text: str) -> ParsedDiff:
    """Parse a raw git diff into a `ParsedDiff`.

    The PR-info preamble (`scr-pr` / `scr-base` / `scr-head` / `scr-version`
    / `scr-model`) is allowed and consumed because the augment pipeline
    writes it onto the raw diff before the annotation passes run. Any
    other `#scr:` directive is rejected — call `parse_augmented_diff`
    for inputs carrying annotations.
    """
    raw = _scan(text)
    if raw.preamble.overview is not None:
        raise ParseError("parse_raw_diff: scr-overview directive present (use parse_augmented_diff)")
    for entry in raw.files:
        for d in entry.header_directives:
            raise ParseError(f"line {d.lineno}: unexpected file-level annotation {d.name!r} in raw diff")
        for hunk_dirs in entry.hunk_directives:
            for d in hunk_dirs:
                raise ParseError(f"line {d.lineno}: unexpected hunk-level annotation {d.name!r} in raw diff")
    return ParsedDiff(
        version=raw.preamble.version,
        pr=raw.preamble.pr,
        files=[entry.parsed for entry in raw.files],
    )
