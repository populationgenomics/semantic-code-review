"""fetch.anchor: diff-based comment anchor propagation.

Two layers of coverage:

- ``_parse_hunk_headers`` + ``_propagate_through_hunks`` are the pure
  algorithm; we exercise them with synthetic diff strings + hunk lists
  to cover every status outcome.
- ``propagate_anchor`` is the git-plumbing wrapper; one mocked-subprocess
  test per branch confirms the wiring (file_gone, commit_unavailable,
  successful diff).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from semantic_code_review.fetch.anchor import (
    AnchorResult,
    propagate_anchor,
)
from semantic_code_review.fetch.anchor import (
    _parse_hunk_headers,
    _propagate_through_hunks,
)


# ---------------------------------------------------------------------------
# Hunk header parsing
# ---------------------------------------------------------------------------


def test_parse_hunks_basic() -> None:
    diff = """\
diff --git a/x b/x
--- a/x
+++ b/x
@@ -10,3 +10,2 @@
-a
-b
-c
+a'
+b'
@@ -50,0 +49 @@
+inserted
"""
    hs = _parse_hunk_headers(diff)
    assert len(hs) == 2
    assert (hs[0].old_start, hs[0].old_count, hs[0].new_start, hs[0].new_count) == (10, 3, 10, 2)
    # The second hunk omits new_count → defaults to 1.
    assert (hs[1].old_start, hs[1].old_count, hs[1].new_start, hs[1].new_count) == (50, 0, 49, 1)


def test_parse_hunks_default_counts_when_omitted() -> None:
    """Single-line hunks emit `@@ -10 +10 @@` without explicit counts;
    each count defaults to 1."""
    hs = _parse_hunk_headers("@@ -10 +10 @@\n-x\n+X\n")
    assert (hs[0].old_count, hs[0].new_count) == (1, 1)


def test_parse_hunks_skips_non_header_lines() -> None:
    """Only @@ headers contribute. Removed/added body lines are ignored."""
    diff = """\
@@ -1,2 +1,1 @@
-first
-second
+only
"""
    hs = _parse_hunk_headers(diff)
    assert len(hs) == 1


# ---------------------------------------------------------------------------
# Propagation algorithm
# ---------------------------------------------------------------------------


def _h(old_start: int, old_count: int, new_start: int, new_count: int):
    from semantic_code_review.fetch.anchor import _HunkHeader
    return _HunkHeader(old_start, old_count, new_start, new_count)


def test_line_above_all_hunks_is_anchored_at_same_number() -> None:
    # Hunk @ line 50; we ask about line 10.
    hs = [_h(50, 3, 50, 2)]
    assert _propagate_through_hunks(hs, 10) == AnchorResult("anchored", 10)


def test_line_below_all_hunks_shifts_by_cumulative_offset() -> None:
    # @@ -10,3 +10,5 @@: net +2.
    # @@ -50,1 +52,0 @@: net -1.
    # Comment at old line 100 → 100 + 2 - 1 = 101 at head.
    hs = [_h(10, 3, 10, 5), _h(50, 1, 52, 0)]
    assert _propagate_through_hunks(hs, 100) == AnchorResult("shifted", 101)


def test_line_in_modification_hunk_is_orphaned_at_post_hunk_position() -> None:
    # Lines 10-12 replaced with 10-11. Old line 11 is removed.
    # Anchor falls to first surviving line after the hunk:
    # new_start + new_count = 10 + 2 = 12.
    hs = [_h(10, 3, 10, 2)]
    assert _propagate_through_hunks(hs, 11) == AnchorResult("orphaned", 12)


def test_line_in_pure_deletion_hunk_is_orphaned() -> None:
    # Delete old lines 10..12 entirely. new_count = 0 → anchor at line 10.
    hs = [_h(10, 3, 9, 0)]
    assert _propagate_through_hunks(hs, 11) == AnchorResult("orphaned", 9)


def test_deletion_at_top_of_file_clamps_orphan_anchor_to_line_1() -> None:
    # `@@ -1,3 +0,0 @@` — formula gives 0; we clamp to 1.
    hs = [_h(1, 3, 0, 0)]
    assert _propagate_through_hunks(hs, 2) == AnchorResult("orphaned", 1)


def test_pure_insertion_above_line_shifts_it_down() -> None:
    # `@@ -10,0 +10,3 @@` — insertion of 3 lines after old line 10.
    # Old line 20 is below the insertion → shifts to 23.
    hs = [_h(10, 0, 10, 3)]
    assert _propagate_through_hunks(hs, 20) == AnchorResult("shifted", 23)


def test_pure_insertion_below_line_leaves_it_anchored() -> None:
    # Insertion at old line 50; we ask about line 10. Untouched.
    hs = [_h(50, 0, 50, 3)]
    assert _propagate_through_hunks(hs, 10) == AnchorResult("anchored", 10)


def test_pure_insertion_at_exactly_our_line_does_not_shift() -> None:
    """Git's convention: `@@ -10,0 +11,3 @@` means the insert sits between
    old lines 10 and 11. A comment on old line 10 stays at old line 10;
    only lines strictly after old_start move."""
    hs = [_h(10, 0, 11, 3)]
    assert _propagate_through_hunks(hs, 10) == AnchorResult("anchored", 10)
    # And line 11 (the next one) IS pushed down by 3.
    assert _propagate_through_hunks(hs, 11) == AnchorResult("shifted", 14)


def test_multiple_hunks_compound_their_offsets() -> None:
    # Hunk 1: +2 net. Hunk 2: -1 net. Hunk 3: +5 net. Cumulative below: +6.
    hs = [_h(5, 1, 5, 3), _h(20, 2, 22, 1), _h(40, 0, 40, 5)]
    assert _propagate_through_hunks(hs, 100) == AnchorResult("shifted", 106)


def test_orphan_in_first_hunk_returns_without_inspecting_later_hunks() -> None:
    # Removal hunk first, big insertion later. Orphan anchor uses ONLY
    # the first hunk's post-position — we don't accumulate later hunks
    # into an orphan's anchor.
    hs = [_h(10, 3, 10, 0), _h(20, 0, 20, 100)]
    assert _propagate_through_hunks(hs, 11) == AnchorResult("orphaned", 10)


# ---------------------------------------------------------------------------
# propagate_anchor — git plumbing wrapper
# ---------------------------------------------------------------------------


class _GitMock:
    """Stub subprocess.run that dispatches based on the git subcommand.

    Each invocation returns whatever the test queued for that
    `(subcommand, args-suffix)` key. Anything unrecognised raises so
    the test fails loudly rather than silently returning success.
    """

    def __init__(self) -> None:
        self.responses: dict[tuple, tuple[int, str, str]] = {}

    def expect(self, args_tail: tuple, *, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.responses[args_tail] = (rc, stdout, stderr)

    def __call__(self, argv, *args, **kwargs):
        # argv is the full command list; we key by the trailing
        # git-subcommand args so tests don't have to spell out the
        # leading `git`.
        try:
            git_idx = argv.index("git")
        except ValueError:
            git_idx = -1
        tail = tuple(argv[git_idx + 1:])
        for k, v in self.responses.items():
            if tail[:len(k)] == k or tail[-len(k):] == k:
                rc, stdout, stderr = v
                return subprocess.CompletedProcess(
                    args=argv, returncode=rc, stdout=stdout, stderr=stderr,
                )
        raise AssertionError(f"unexpected git call: {tail}")


@pytest.fixture
def repo_git(tmp_path: Path) -> Path:
    return tmp_path / "repo.git"


def test_propagate_anchor_short_circuits_when_commit_is_head(repo_git: Path) -> None:
    """commit_id == head_sha is the trivial case — no git calls needed."""
    with patch("semantic_code_review.git_ops.subprocess.run") as run_mock:
        result = propagate_anchor(repo_git, "deadbeef", "deadbeef", "x.py", 5)
    assert result == AnchorResult("anchored", 5)
    assert run_mock.call_count == 0


def test_propagate_anchor_returns_commit_unavailable_when_commit_missing(repo_git: Path) -> None:
    g = _GitMock()
    g.expect(("cat-file", "-e", "old^{commit}"), rc=1, stderr="missing")
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=g):
        result = propagate_anchor(repo_git, "old", "new", "x.py", 5)
    assert result == AnchorResult("commit_unavailable", None)


def test_propagate_anchor_returns_file_gone_when_path_missing_at_head(repo_git: Path) -> None:
    g = _GitMock()
    g.expect(("cat-file", "-e", "old^{commit}"), rc=0)
    g.expect(("cat-file", "-e", "new:x.py"), rc=1, stderr="missing")
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=g):
        result = propagate_anchor(repo_git, "old", "new", "x.py", 5)
    assert result == AnchorResult("file_gone", None)


def test_propagate_anchor_walks_real_diff_output(repo_git: Path) -> None:
    """End-to-end: parse-and-walk against a unified-diff string from
    a mocked git diff call."""
    diff = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -10,3 +10,2 @@
-old10
-old11
-old12
+new10
+new11
"""
    g = _GitMock()
    g.expect(("cat-file", "-e", "old^{commit}"), rc=0)
    g.expect(("cat-file", "-e", "new:x.py"), rc=0)
    g.expect(("diff", "--unified=0"), rc=0, stdout=diff)
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=g):
        # Line 11 was inside the modification — orphaned at post-hunk
        # position (new_start + new_count = 10 + 2 = 12).
        assert propagate_anchor(repo_git, "old", "new", "x.py", 11) \
            == AnchorResult("orphaned", 12)
        # Line 100 is well below the hunk; shifted by -1 (3→2 net).
        assert propagate_anchor(repo_git, "old", "new", "x.py", 100) \
            == AnchorResult("shifted", 99)


def test_propagate_anchor_returns_anchored_when_diff_is_empty(repo_git: Path) -> None:
    """File unchanged between the two commits → no hunks → line stays
    at its original number."""
    g = _GitMock()
    g.expect(("cat-file", "-e", "old^{commit}"), rc=0)
    g.expect(("cat-file", "-e", "new:x.py"), rc=0)
    g.expect(("diff", "--unified=0"), rc=0, stdout="")
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=g):
        assert propagate_anchor(repo_git, "old", "new", "x.py", 42) \
            == AnchorResult("anchored", 42)
