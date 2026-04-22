"""PR URL parser: well-formed URLs accepted, garbage rejected."""

from __future__ import annotations

import pytest

from semantic_code_review.fetch.gh import parse_pr_url


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
