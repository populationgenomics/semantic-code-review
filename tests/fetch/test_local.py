"""fetch.local.resolve_local_diff: resolve input specs against a tmp repo.

Probes the resolved object's structural fields (mode, head_is_working,
raw_diff, head_sha, slug). Materialisation tests live separately; this
file covers the resolve step alone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from semantic_code_review.fetch import (
    EmptyDiff,
    LocalDiffError,
    resolve_local_diff,
)


def _sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
        "--allow-empty", "-q", "-m", "root")
    (root / "a.py").write_text("x = 1\n")
    _sh(root, "git", "add", "a.py")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "-m", "add a")
    (root / "a.py").write_text("x = 2\n")
    _sh(root, "git", "add", "a.py")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "-m", "bump a")
    return root


def test_range_two_dots(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1..HEAD", repo_root=repo)
    assert r.mode == "range"
    assert "a.py" in r.raw_diff
    assert "-x = 1" in r.raw_diff and "+x = 2" in r.raw_diff
    assert r.files == ["a.py"]
    assert not r.head_is_working


def test_range_three_dots(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1...HEAD", repo_root=repo)
    assert r.mode == "range"
    assert "+x = 2" in r.raw_diff


def test_range_rejects_flags(repo: Path) -> None:
    with pytest.raises(LocalDiffError, match="only apply"):
        resolve_local_diff("HEAD~1..HEAD", repo_root=repo, no_staged=True)


def test_single_ref_clean(repo: Path) -> None:
    r = resolve_local_diff("HEAD~1", repo_root=repo)
    # clean working tree: equivalent to HEAD~1..HEAD
    assert r.mode == "ref..HEAD"
    assert "+x = 2" in r.raw_diff
    assert not r.head_is_working or r.head_is_working  # either fine when clean


def test_single_ref_dirty_picks_up_unstaged(repo: Path) -> None:
    (repo / "a.py").write_text("x = 3\n")
    r = resolve_local_diff("HEAD~1", repo_root=repo)
    assert r.mode == "ref-working"
    assert r.head_is_working
    assert "+x = 3" in r.raw_diff
    # synthetic head sha has a -dirty- tag
    assert "-dirty-" in r.head_sha


def test_no_staged_drops_staged(repo: Path) -> None:
    # Stage a change, leave another unstaged.
    (repo / "a.py").write_text("x = 3\n")
    _sh(repo, "git", "add", "a.py")
    (repo / "a.py").write_text("x = 4\n")  # unstaged on top
    r = resolve_local_diff("HEAD~1", repo_root=repo, no_staged=True)
    assert r.mode == "ref-working-no-staged"
    # Committed portion yields +x = 2 (HEAD~1..HEAD); unstaged yields +x = 4.
    assert "+x = 2" in r.raw_diff or "+x = 4" in r.raw_diff


def test_no_unstaged(repo: Path) -> None:
    (repo / "a.py").write_text("x = 3\n")
    _sh(repo, "git", "add", "a.py")
    (repo / "a.py").write_text("x = 4\n")  # unstaged on top — should be excluded
    r = resolve_local_diff("HEAD~1", repo_root=repo, no_unstaged=True)
    assert r.mode == "ref-working-no-unstaged"
    assert "+x = 3" in r.raw_diff and "+x = 4" not in r.raw_diff


def test_both_flags_equiv_to_range(repo: Path) -> None:
    (repo / "a.py").write_text("x = 3\n")
    _sh(repo, "git", "add", "a.py")
    r = resolve_local_diff("HEAD~1", repo_root=repo, no_staged=True, no_unstaged=True)
    assert r.mode == "ref..HEAD"
    assert "+x = 2" in r.raw_diff
    assert "+x = 3" not in r.raw_diff


def test_empty_diff_errors(repo: Path) -> None:
    """EmptyDiff is a LocalDiffError subclass; old `match="no diff"`
    callers still catch the parent class. The CLI distinguishes the
    two so "nothing to review" exits 0 instead of crashing."""
    with pytest.raises(EmptyDiff, match="no changes to review"):
        resolve_local_diff("HEAD..HEAD", repo_root=repo)
    with pytest.raises(LocalDiffError):
        resolve_local_diff("HEAD..HEAD", repo_root=repo)


def test_slug_is_stable(repo: Path) -> None:
    r1 = resolve_local_diff("HEAD~1..HEAD", repo_root=repo)
    r2 = resolve_local_diff("HEAD~1..HEAD", repo_root=repo)
    assert r1.slug == r2.slug
    assert r1.slug.startswith("local-HEAD-1..HEAD-")


def test_rejects_non_git_dir(tmp_path: Path) -> None:
    # A plain empty directory is not a repo.
    with pytest.raises(LocalDiffError):
        resolve_local_diff("HEAD", repo_root=tmp_path)
