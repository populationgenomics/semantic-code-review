"""SourceCache memo + its wiring into RepoTools (ADR 0003 Slice 1)."""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from semantic_code_review.augment import source_cache
from semantic_code_review.augment import tools as tools_mod
from semantic_code_review.augment.tools import RepoTools

# --- SourceCache unit ------------------------------------------------------


def test_source_computes_once_per_key() -> None:
    cache = source_cache.SourceCache()
    calls: list[str] = []

    def compute() -> str:
        calls.append("a")
        return "content"

    assert cache.source("sha", "a.py", compute) == "content"
    assert cache.source("sha", "a.py", compute) == "content"
    assert calls == ["a"]


def test_source_caches_none() -> None:
    """A miss is keyed by presence, not truthiness — None is not recomputed."""
    cache = source_cache.SourceCache()
    calls: list[int] = []

    def compute() -> None:
        calls.append(1)

    assert cache.source(None, "gone.py", compute) is None
    assert cache.source(None, "gone.py", compute) is None
    assert len(calls) == 1


def test_source_keys_distinctly() -> None:
    cache = source_cache.SourceCache()
    assert cache.source("base", "a.py", lambda: "base-a") == "base-a"
    assert cache.source(None, "a.py", lambda: "head-a") == "head-a"
    assert cache.source("base", "b.py", lambda: "base-b") == "base-b"
    # Re-reads hit the three distinct entries, not each other.
    assert cache.source("base", "a.py", lambda: "WRONG") == "base-a"
    assert cache.source(None, "a.py", lambda: "WRONG") == "head-a"


def test_outline_computes_once_per_key() -> None:
    cache = source_cache.SourceCache()
    calls: list[int] = []

    def compute() -> list:
        calls.append(1)
        return []

    assert cache.outline("sha", "a.py", compute) == []
    assert cache.outline("sha", "a.py", compute) == []
    assert len(calls) == 1


def test_source_computes_once_under_concurrency() -> None:
    """8 threads racing on one key compute it exactly once (ADR 0003 Slice 3)."""
    cache = source_cache.SourceCache()
    calls: list[int] = []
    calls_lock = threading.Lock()

    def compute() -> str:
        with calls_lock:
            calls.append(1)
        time.sleep(0.05)  # widen the race window
        return "content"

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: cache.source("sha", "a.py", compute), range(8)))

    assert results == ["content"] * 8
    assert len(calls) == 1


def test_distinct_keys_compute_concurrently() -> None:
    """Different keys don't serialise on each other — the per-key lock,
    not a global one, guards compute()."""
    cache = source_cache.SourceCache()
    barrier = threading.Barrier(4, timeout=2.0)

    def compute() -> str:
        # If computes serialised, the 4th thread would never reach the
        # barrier and this would time out.
        barrier.wait()
        return "x"

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda i: cache.source(None, f"f{i}.py", compute), range(4)))
    assert results == ["x"] * 4


# --- RepoTools wiring ------------------------------------------------------


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    return 1\n")
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return root


def _head_sha(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from semantic_code_review import structural

    calls: list[int] = []
    real = structural.outline_symbols

    def spy(source, lang):
        calls.append(1)
        return real(source, lang)

    monkeypatch.setattr(structural, "outline_symbols", spy)
    return calls


def test_repo_tools_parses_once_with_cache(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parses = _count_parses(monkeypatch)
    rt = RepoTools(
        head_worktree=repo,
        repo_git=repo,
        base_sha=_head_sha(repo),
        head_sha=_head_sha(repo),
        cache=source_cache.SourceCache(),
    )
    rt.outline("a.py")
    rt.symbol_at("a.py", 2)
    rt.outline("a.py")
    assert len(parses) == 1


def test_repo_tools_reparses_without_cache(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parses = _count_parses(monkeypatch)
    rt = RepoTools(head_worktree=repo, repo_git=repo, base_sha=_head_sha(repo), head_sha=_head_sha(repo))
    rt.outline("a.py")
    rt.outline("a.py")
    assert len(parses) == 2


def test_head_worktree_and_head_sha_key_separately(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sha=None` (worktree read) and an explicit head SHA (`git show`) are
    distinct keys — same content, two parses, no cross-contamination."""
    parses = _count_parses(monkeypatch)
    rt = RepoTools(
        head_worktree=repo,
        repo_git=repo,
        base_sha=_head_sha(repo),
        head_sha=_head_sha(repo),
        cache=source_cache.SourceCache(),
    )
    rt.outline("a.py")  # (None, a.py)
    rt.outline("a.py", sha=_head_sha(repo))  # (sha, a.py)
    assert len(parses) == 2


def test_source_read_memoised(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated reads at a revision run `git show` once."""
    from semantic_code_review import git_ops

    calls: list[int] = []
    real = git_ops.git_capture

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(tools_mod.git_ops, "git_capture", spy)
    sha = _head_sha(repo)
    rt = RepoTools(head_worktree=repo, repo_git=repo, base_sha=sha, head_sha=sha, cache=source_cache.SourceCache())
    rt.outline("a.py", sha=sha)
    rt.symbol_at("a.py", 1, sha=sha)
    assert len(calls) == 1
