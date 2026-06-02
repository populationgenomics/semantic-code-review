"""Validate an augmented diff.

Checks:
1. The text parses without errors.
2. `emit(parse(text)) == text` (text is already in canonical form).
3. All smell tags reference the closed vocabulary.
4. If a sidecar is supplied, it matches the parsed diff byte-for-byte
   when re-serialized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..augment.schemas import SMELL_TAGS, AnnotatedDiff
from .emit import emit_augmented_diff
from .parse import parse_augmented_diff
from .sidecar import load_sidecar


@dataclass
class LintResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def lint_text(text: str, sidecar_path: Path | None = None) -> LintResult:
    result = LintResult(ok=True)

    try:
        diff = parse_augmented_diff(text)
    except Exception as e:  # noqa: BLE001 — parser raises many flavors
        result.ok = False
        result.errors.append(f"parse error: {e}")
        return result

    _check_smell_tags(diff, result)

    round_tripped = emit_augmented_diff(diff)
    if round_tripped != text:
        result.ok = False
        result.errors.append("not in canonical form: emit(parse(text)) != text (run 'scr fmt' to fix)")

    if sidecar_path is not None:
        try:
            side = load_sidecar(sidecar_path)
        except Exception as e:  # noqa: BLE001
            result.ok = False
            result.errors.append(f"sidecar load error: {e}")
            return result
        if side.model_dump() != diff.model_dump():
            result.ok = False
            result.errors.append("sidecar does not match inline annotations")

    return result


def _check_smell_tags(diff: AnnotatedDiff, result: LintResult) -> None:
    for f in diff.files:
        for h in f.hunks:
            for s in h.ann.smells:
                if s.tag not in SMELL_TAGS:
                    result.ok = False
                    result.errors.append(f"unknown smell tag {s.tag!r} on hunk {h.parsed.header}")
            for seg in h.ann.segments:
                for s in seg.smells:
                    if s.tag not in SMELL_TAGS:
                        result.ok = False
                        result.errors.append(
                            f"unknown smell tag {s.tag!r} on segment "
                            f"+{seg.new_start}..+{seg.new_start + seg.new_count - 1}"
                        )
