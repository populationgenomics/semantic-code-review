"""Sidecar JSON: the structured mirror of an augmented diff.

The sidecar is the HTML renderer's primary input. It must be equivalent
to the inline augmented diff — `scr lint` enforces that.
"""

from __future__ import annotations

from pathlib import Path

from ..augment.schemas import AugmentedDiff


def dump_sidecar(diff: AugmentedDiff, path: Path) -> None:
    path.write_text(diff.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")


def load_sidecar(path: Path) -> AugmentedDiff:
    return AugmentedDiff.model_validate_json(path.read_text(encoding="utf-8"))
