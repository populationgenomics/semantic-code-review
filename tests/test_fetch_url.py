"""PR URL parser: well-formed URLs accepted, garbage rejected."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from semantic_code_review.fetch.gh import (
    GhFetchError, PRRef, fetch_pr_meta, parse_pr_url, preflight_gh,
)


def test_parses_standard_url() -> None:
    ref = parse_pr_url("https://github.com/acme/widgets/pull/482")
    assert ref.owner == "acme"
    assert ref.repo == "widgets"
    assert ref.number == 482
    assert ref.slug == "acme/widgets"
    assert ref.url == "https://github.com/acme/widgets/pull/482"
    assert ref.clone_url == "https://github.com/acme/widgets.git"


def test_parses_with_trailing_slash() -> None:
    ref = parse_pr_url("https://github.com/acme/widgets/pull/482/")
    assert ref.number == 482


def test_parses_http_scheme() -> None:
    ref = parse_pr_url("http://github.com/a/b/pull/1")
    assert ref.owner == "a" and ref.repo == "b" and ref.number == 1


def test_rejects_non_pr_url() -> None:
    with pytest.raises(ValueError, match="not a GitHub PR URL"):
        parse_pr_url("https://github.com/acme/widgets/issues/482")


def test_rejects_non_github_url() -> None:
    with pytest.raises(ValueError, match="not a GitHub PR URL"):
        parse_pr_url("https://gitlab.com/acme/widgets/merge_requests/1")


def test_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_pr_url("not a url")


# ---------------------------------------------------------------------------
# fetch_pr_meta error translation
# ---------------------------------------------------------------------------


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=returncode,
            stdout=stdout, stderr=stderr,
        )
    return runner


def test_other_gh_failures_pass_through() -> None:
    """Non-version errors (auth, network) keep their original stderr."""
    ref = PRRef(owner="acme", repo="widgets", number=42)
    fake = _fake_run(
        stderr="HTTP 401: Bad credentials",
        returncode=1,
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        with pytest.raises(GhFetchError, match="Bad credentials"):
            fetch_pr_meta(ref)


# ---------------------------------------------------------------------------
# preflight_gh: PATH + version
# ---------------------------------------------------------------------------


def test_preflight_missing_gh_raises() -> None:
    with patch("semantic_code_review.git_ops.shutil.which", return_value=None):
        with pytest.raises(GhFetchError, match="not found on PATH"):
            preflight_gh()


def test_preflight_too_old_raises() -> None:
    fake = _fake_run(stdout="gh version 2.10.0 (2022-08-22)\n", returncode=0)
    with patch("semantic_code_review.git_ops.shutil.which", return_value="/u/bin/gh"):
        with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
            with pytest.raises(GhFetchError, match="2.10.0 is too old"):
                preflight_gh()


def test_preflight_recent_passes() -> None:
    fake = _fake_run(stdout="gh version 2.40.1 (2024-01-08)\n", returncode=0)
    with patch("semantic_code_review.git_ops.shutil.which", return_value="/u/bin/gh"):
        with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
            assert preflight_gh() == "/u/bin/gh"


def test_preflight_unparseable_version_does_not_block() -> None:
    """Don't break working setups whose --version output we can't parse."""
    fake = _fake_run(stdout="some other gh fork v9001\n", returncode=0)
    with patch("semantic_code_review.git_ops.shutil.which", return_value="/u/bin/gh"):
        with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
            assert preflight_gh() == "/u/bin/gh"


def test_preflight_at_minimum_version_passes() -> None:
    fake = _fake_run(stdout="gh version 2.21.0 (2023-01-19)\n", returncode=0)
    with patch("semantic_code_review.git_ops.shutil.which", return_value="/u/bin/gh"):
        with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
            assert preflight_gh() == "/u/bin/gh"
