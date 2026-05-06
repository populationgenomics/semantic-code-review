"""Repo tools: read_file, grep, list_dir, git operations against a tmp repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from semantic_code_review.augment.repo_tool_fns import mcp_dispatch
from semantic_code_review.augment.tools import (
    TOOL_RESULT_CAP_BYTES,
    RepoTools,
)


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> RepoTools:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    return 1\n")
    (root / "b.py").write_text("x = 'foo'\n")
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("y = 2\n")
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return RepoTools(
        head_worktree=root,
        repo_git=root,
        base_sha=head_sha,
        head_sha=head_sha,
    )


def test_read_file(repo: RepoTools) -> None:
    text = repo.read_file("a.py")
    assert "def foo" in text


def test_read_file_line_range(repo: RepoTools) -> None:
    text = repo.read_file("a.py", start_line=2, end_line=2)
    assert text.strip() == "return 1"


def test_read_file_missing(repo: RepoTools) -> None:
    assert "error" in repo.read_file("does-not-exist")


def test_read_file_escape_blocked(repo: RepoTools) -> None:
    assert "error" in repo.read_file("../escape")


def test_read_file_at(repo: RepoTools) -> None:
    text = repo.read_file_at(repo.head_sha, "a.py")
    assert "def foo" in text


def test_grep(repo: RepoTools) -> None:
    out = repo.grep("foo")
    assert "a.py" in out and "b.py" in out


def test_grep_with_glob(repo: RepoTools) -> None:
    out = repo.grep("foo", path_glob="b.py")
    assert "b.py" in out
    assert "a.py" not in out


def test_grep_fallback_to_git_grep(repo: RepoTools, monkeypatch) -> None:
    """When rg isn't on PATH, grep still works via git grep."""
    from semantic_code_review.augment import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_HAS_RIPGREP", False)
    out = repo.grep("foo")
    assert "a.py" in out and "b.py" in out


def test_grep_fallback_with_glob(repo: RepoTools, monkeypatch) -> None:
    from semantic_code_review.augment import tools as tools_mod

    monkeypatch.setattr(tools_mod, "_HAS_RIPGREP", False)
    out = repo.grep("foo", path_glob="b.py")
    assert "b.py" in out
    assert "a.py" not in out


def test_list_dir(repo: RepoTools) -> None:
    out = repo.list_dir("")
    assert "a.py" in out and "sub/" in out


def test_list_dir_sub(repo: RepoTools) -> None:
    out = repo.list_dir("sub")
    assert "c.py" in out


def test_git_log(repo: RepoTools) -> None:
    out = repo.git_log("a.py", limit=5)
    assert "init" in out


def test_truncation() -> None:
    from semantic_code_review.augment.tools import _cap

    big = "x" * (TOOL_RESULT_CAP_BYTES + 10)
    out = _cap(big)
    assert "truncated" in out
    assert len(out.encode("utf-8")) <= TOOL_RESULT_CAP_BYTES + 200


def test_mcp_dispatch(repo: RepoTools) -> None:
    out = mcp_dispatch(repo, "read_file", {"path": "a.py"})
    assert "def foo" in out
    assert "unknown tool" in mcp_dispatch(repo, "nonexistent", {})
