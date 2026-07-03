"""Diff-based anchor propagation for ingested PR comments.

A reviewer comment is left at ``(commit_id, path, line)``. When the PR
advances past ``commit_id``, the line may still be present, may have
shifted up or down, or may have been removed entirely. This module
propagates the original anchor through the diff between ``commit_id``
and ``head_sha`` to surface one of five outcomes:

- ``anchored``: line is identical at head — same number, same content
  (no hunk touched it).
- ``shifted``: line still exists at head but at a different line number
  (hunks above it adjusted the offset).
- ``orphaned``: line was removed at head. The anchor moves to the
  first surviving line *below* the lost one — by the diff math, that's
  the first line after the hunk that removed it.
- ``file_gone``: ``path`` no longer exists at head_sha (deleted, or
  renamed without ``-M`` finding it — rename detection is a later
  slice).
- ``commit_unavailable``: ``commit_id`` isn't in the local repo (we
  failed to fetch it, e.g. force-push >90d ago). The caller can't
  propagate; the comment stays pinned at its original anchor.

The propagator runs ``git diff --unified=0`` so it sees only ``-``
removal and ``+`` addition lines — no context — which means every line
inside a hunk's old range was removed, and every line outside any hunk
survives at ``head_sha`` with a constant offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import git_ops

AnchorStatus = Literal[
    "anchored", "shifted", "orphaned", "file_gone", "commit_unavailable",
]


@dataclass(frozen=True)
class AnchorResult:
    status: AnchorStatus
    head_line: int | None


@dataclass(frozen=True)
class _HunkHeader:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_hunk_headers(diff: str) -> list[_HunkHeader]:
    """Pull just the @@ headers out of a unified diff.

    With ``--unified=0`` everything between the headers is removal /
    addition lines we don't need — the offsets in the header are enough
    to walk the line-shift math. Headers without a count default to 1
    per git's convention (``@@ -10 +10 @@`` == ``@@ -10,1 +10,1 @@``).
    """
    out: list[_HunkHeader] = []
    for line in diff.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        os, oc, ns, nc = m.groups()
        out.append(_HunkHeader(
            old_start=int(os),
            old_count=int(oc) if oc is not None else 1,
            new_start=int(ns),
            new_count=int(nc) if nc is not None else 1,
        ))
    return out


def _propagate_through_hunks(hunks: list[_HunkHeader], line: int) -> AnchorResult:
    """The pure algorithm — walk a list of hunk headers and decide
    where ``line`` lands at head.

    Hunks are ordered by ``old_start``. We carry a cumulative offset
    forward; each hunk above ``line`` contributes ``new_count - old_count``.
    Pure-insertion hunks (``old_count == 0``) sit between two old
    lines, at position ``old_start`` per git's convention, and shift
    everything strictly below.
    """
    offset = 0
    for h in hunks:
        if h.old_count == 0:
            # Pure insertion between old lines `old_start` and `old_start + 1`.
            # Lines strictly after `old_start` get pushed down.
            if line > h.old_start:
                offset += h.new_count
            continue
        old_end = h.old_start + h.old_count - 1
        if line < h.old_start:
            # Hunk is below us; nothing further can shift `line`.
            break
        if h.old_start <= line <= old_end:
            # Position-pair `-` lines with `+` lines inside the same
            # hunk: the i-th removed line maps to the i-th added line
            # if one exists. This is the common edit-in-place case —
            # a `@@ -53,2 +55,2 @@` paragraph rewrite leaves both old
            # lines paired with the rewritten versions rather than
            # orphaned at the bottom of the hunk. Only the *tail* of
            # an asymmetric shrink (`@@ -10,5 +10,3 @@` lines 13–14
            # have no `+` partner) truly orphans.
            #
            # Note: pairing is positional, not content-based. A hunk
            # that reorders or replaces wholesale will pair against
            # the wrong `+` line; the chip still says "was line N" so
            # the reviewer can see the original anchor and judge.
            pos = line - h.old_start
            if pos < h.new_count:
                new_line = h.new_start + pos
                return AnchorResult(
                    "anchored" if new_line == line else "shifted",
                    new_line,
                )
            # No paired addition — anchor at first surviving line
            # below the hunk. Clamp to 1 for deletions at top of file
            # (`@@ -1,k +0,0 @@` would otherwise yield 0).
            return AnchorResult("orphaned", max(1, h.new_start + h.new_count))
        # line is below the hunk's old range — apply the shift and
        # keep walking.
        offset += h.new_count - h.old_count

    new_line = line + offset
    return AnchorResult(
        "anchored" if offset == 0 else "shifted",
        new_line,
    )


def _commit_exists(repo_git: Path, sha: str) -> bool:
    rc, _, _ = git_ops.git_capture(repo_git, "cat-file", "-e", f"{sha}^{{commit}}")
    return rc == 0


def _path_exists_at(repo_git: Path, sha: str, path: str) -> bool:
    rc, _, _ = git_ops.git_capture(repo_git, "cat-file", "-e", f"{sha}:{path}")
    return rc == 0


@dataclass(frozen=True)
class _PathDiff:
    """Result of loading the diff for one (commit_id, path) pair.

    ``hunks`` is the parsed list; ``sentinel`` is set instead when the
    diff couldn't be computed at all (``file_gone`` / ``commit_unavailable``).
    Either ``hunks`` is populated and ``sentinel`` is None, or vice
    versa. A successful diff with no hunks is just an empty list — the
    file is unchanged between the two commits.
    """
    hunks: list[_HunkHeader] | None
    sentinel: AnchorStatus | None


def load_path_diff(
    repo_git: Path, commit_id: str, head_sha: str, path: str,
) -> _PathDiff:
    """Run ``git diff`` for one ``(commit_id, path)`` and return parsed
    hunks (or the sentinel that explains why we can't).

    Pulled out of :func:`propagate_anchor` so the batched ingest path
    can cache one ``_PathDiff`` per pair — every comment in the same
    PR push shares a ``commit_id``, so without this cache we re-parse
    the same diff once per comment on the file.
    """
    if not _commit_exists(repo_git, commit_id):
        return _PathDiff(None, "commit_unavailable")
    if not _path_exists_at(repo_git, head_sha, path):
        return _PathDiff(None, "file_gone")
    rc, diff_out, _ = git_ops.git_capture(
        repo_git,
        "diff", "--unified=0", "--no-color",
        f"{commit_id}..{head_sha}", "--", path,
    )
    if rc != 0:
        # Both endpoints exist locally but the diff still failed — treat
        # as unavailable rather than guess. Rare; surfaces in the chip.
        return _PathDiff(None, "commit_unavailable")
    return _PathDiff(_parse_hunk_headers(diff_out), None)


def apply_path_diff(diff: _PathDiff, line: int) -> AnchorResult:
    """Resolve one ``line`` against a loaded :class:`_PathDiff`."""
    if diff.sentinel is not None:
        return AnchorResult(diff.sentinel, None)
    assert diff.hunks is not None  # invariant from load_path_diff
    if not diff.hunks:
        return AnchorResult("anchored", line)
    return _propagate_through_hunks(diff.hunks, line)


def propagate_anchor(
    repo_git: Path,
    commit_id: str,
    head_sha: str,
    path: str,
    line: int,
) -> AnchorResult:
    """Compute the head-side anchor for a comment originally left on
    ``(commit_id, path, line)``.

    Returns an :class:`AnchorResult` describing where the comment
    should pin at ``head_sha``, plus a status for the viewer to chip.
    Convenience wrapper over :func:`load_path_diff` + :func:`apply_path_diff`
    for one-off lookups; batch callers should hold the ``_PathDiff``
    so they don't re-run ``git diff`` per comment.
    """
    if commit_id == head_sha:
        return AnchorResult("anchored", line)
    diff = load_path_diff(repo_git, commit_id, head_sha, path)
    return apply_path_diff(diff, line)


__all__ = [
    "AnchorResult",
    "AnchorStatus",
    "apply_path_diff",
    "load_path_diff",
    "propagate_anchor",
]
