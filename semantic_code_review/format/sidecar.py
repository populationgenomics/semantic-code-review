"""Sidecar JSON: the structured mirror of an augmented diff.

The sidecar is the HTML renderer's primary input. It must be equivalent
to the inline augmented diff — `scr lint` enforces that.
"""

from __future__ import annotations

from pathlib import Path

from ..augment.schemas import AnnotatedDiff


def dump_sidecar(diff: AnnotatedDiff, path: Path) -> None:
    path.write_text(diff.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")


def load_sidecar(path: Path) -> AnnotatedDiff:
    return AnnotatedDiff.model_validate_json(path.read_text(encoding="utf-8"))
