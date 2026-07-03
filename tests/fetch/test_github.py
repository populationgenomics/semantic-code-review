"""fetch.github: URL parsing, resolve_github_pr error mapping, preflight."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from semantic_code_review.fetch import (
    GhFetchError,
    parse_pr_url,
    preflight_gh,
    resolve_github_pr,
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
# resolve_github_pr error mapping
# ---------------------------------------------------------------------------


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return runner


def test_gh_failure_surfaces_stderr() -> None:
    """Non-zero gh exits map to GhFetchError with the stderr preserved."""
    fake = _fake_run(stderr="HTTP 401: Bad credentials", returncode=1)
    url = "https://github.com/acme/widgets/pull/42"
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        with pytest.raises(GhFetchError, match="Bad credentials"):
            resolve_github_pr(url)


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
