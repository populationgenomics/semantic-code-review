"""PR URL parser: well-formed URLs accepted, garbage rejected."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from semantic_code_review.fetch.gh import (
    GhFetchError, PRRef, fetch_pr_meta, parse_pr_url,
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


def test_old_gh_unknown_field_translates_to_clear_message() -> None:
    """Pre-2.21 gh rejects baseRefOid; we translate to a clear upgrade hint."""
    ref = PRRef(owner="acme", repo="widgets", number=42)
    fake = _fake_run(
        stderr=(
            'Unknown JSON field: "baseRefOid"\n'
            'Available fields:\n  additions\n  assignees\n  ...\n'
        ),
        returncode=1,
    )
    with patch("semantic_code_review.fetch.gh.subprocess.run", side_effect=fake):
        with pytest.raises(GhFetchError, match="gh is too old"):
            fetch_pr_meta(ref)


def test_other_gh_failures_pass_through() -> None:
    """Non-version errors (auth, network) keep their original stderr."""
    ref = PRRef(owner="acme", repo="widgets", number=42)
    fake = _fake_run(
        stderr="HTTP 401: Bad credentials",
        returncode=1,
    )
    with patch("semantic_code_review.fetch.gh.subprocess.run", side_effect=fake):
        with pytest.raises(GhFetchError, match="Bad credentials"):
            fetch_pr_meta(ref)
