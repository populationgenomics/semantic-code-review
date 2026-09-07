"""Viewer preferences: the reader's settings that outlive one run.

The review server binds an ephemeral port, so every run is a new browser
origin and `localStorage` starts empty. What the viewer should remember
across runs — the span gutter's fold, the divider widths — therefore
lives here, in `<config root>/viewer-prefs.json`
(`paths.default_viewer_prefs_path`), served by `GET /prefs` and merged
by `PATCH /prefs`. Per-run view state (the selected pill, the open
section) is not a preference and stays in the browser, per tab.

The file is one flat JSON object of scalars keyed by preference name.
Which keys exist is the viewer's business; the store only guarantees the
shape.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from .. import errors, paths

log = logging.getLogger(__name__)

Scalar = str | int | float | bool


class PrefsError(errors.ScrError):
    """A `PATCH /prefs` body that isn't a flat object of scalars."""

    status = 400


class PrefsStore:
    """The preference file, read and merged through one object.

    Every call re-reads the file rather than caching it: a read happens
    once per viewer boot and a write once per gesture, the file is a few
    hundred bytes, and two review servers can run at once against the
    same file — a process-local cache would serve one server's stale
    copy after the other wrote. `update` is read-merge-write under a
    lock, so writes within one server never clobber each other; two
    servers writing in the same instant can still lose one, which for a
    preference is acceptable.

    A file that isn't a flat object of scalars is reported once per read
    (an error naming the path) and read as empty; the next write
    replaces it. A review is not worth failing over a preference file,
    but the log line is the loud part.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, Scalar]:
        """The preferences on disk; empty when the file is absent."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            log.error("viewer prefs: %s unreadable (%s); serving no preferences", self.path, e)
            return {}
        try:
            return _coerce_file(json.loads(raw))
        except ValueError as e:
            log.error("viewer prefs: %s is malformed (%s); serving no preferences", self.path, e)
            return {}

    def update(self, patch: object) -> dict[str, Scalar]:
        """Merge `patch` into the file and return the result.

        A key mapped to `None` is removed. The write is atomic — a temp
        file in the same directory, then `os.replace` — so a reader never
        sees a half-written file.

        Raises:
            PrefsError: `patch` is not an object, or a key is not a
                non-empty string, or a value is not a scalar (or None).
        """
        changes = _validate_patch(patch)
        with self._lock:
            merged = self.read()
            for key, value in changes.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            self._write(merged)
        return merged

    def _write(self, prefs: Mapping[str, Scalar]) -> None:
        paths.ensure_private_dir(self.path.parent)
        text = json.dumps(prefs, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".viewer-prefs-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise


def _is_scalar(value: object) -> bool:
    return isinstance(value, str | int | float | bool)


def _coerce_file(data: object) -> dict[str, Scalar]:
    """Check a decoded file is a flat object of scalars.

    Raises:
        ValueError: naming the offending key, for `read` to report.
    """
    if not isinstance(data, dict):
        raise ValueError("top level is not an object")
    for key, value in data.items():
        if not _is_scalar(value):
            raise ValueError(f"value of {key!r} is not a scalar")
    return data


def _validate_patch(patch: object) -> dict[str, Scalar | None]:
    if not isinstance(patch, dict):
        raise PrefsError("prefs patch must be a JSON object")
    for key, value in patch.items():
        if not isinstance(key, str) or not key:
            raise PrefsError(f"prefs key must be a non-empty string, got {key!r}")
        if value is not None and not _is_scalar(value):
            raise PrefsError(f"prefs value for {key!r} must be a string, number, boolean or null")
    return patch
