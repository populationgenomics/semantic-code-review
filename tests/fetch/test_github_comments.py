"""fetch.github_comments: PR review-comment ingest from gh api."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from semantic_code_review.fetch import PRRef
from semantic_code_review.fetch.github_comments import (
    fetch_pr_review_comments,
    materialize_pr_comments,
    write_comments_file,
)
from semantic_code_review.git_ops import GhError
from semantic_code_review.review.comments import Comment


# A representative payload covering the cases we map: live anchor,
# outdated (line=null), threaded reply, and a record we discard
# (missing required fields).
_SAMPLE = [
    {
        "id": 11,
        "path": "src/foo.py",
        "side": "RIGHT",
        "line": 42,
        "original_line": 40,
        "body": "Nit: use `Path`.",
        "body_html": "<p>Nit: use <code>Path</code>.</p>",
        "commit_id": "deadbeef",
        "original_commit_id": "beefdead",
        "user": {"login": "alice", "avatar_url": "https://example/alice.png"},
        "in_reply_to_id": None,
        "html_url": "https://github.com/o/r/pull/1#discussion_r11",
        "created_at": "2025-06-01T10:15:00Z",
        "updated_at": "2025-06-01T10:20:00Z",
    },
    {
        "id": 12,
        "path": "src/foo.py",
        "side": "RIGHT",
        "line": None,            # outdated comment
        "original_line": 40,
        "body": "Reply!",
        "body_html": "<p>Reply!</p>",
        "commit_id": "deadbeef",
        "user": {"login": "bob", "avatar_url": None},
        "in_reply_to_id": 11,
        "html_url": "https://github.com/o/r/pull/1#discussion_r12",
        "created_at": "2025-06-01T11:00:00Z",
        "updated_at": "2025-06-01T11:00:00Z",
    },
    {
        # Discarded: no usable line anchor at all.
        "id": 13,
        "path": "src/foo.py",
        "side": "RIGHT",
        "line": None,
        "original_line": None,
        "body": "??",
        "user": {"login": "carol"},
        "created_at": "2025-06-01T12:00:00Z",
        "updated_at": "2025-06-01T12:00:00Z",
    },
    {
        # Discarded: schema-broken record (no path).
        "id": 14,
        "side": "RIGHT",
        "line": 1,
        "body": "?",
        "user": {"login": "dave"},
        "created_at": "2025-06-01T13:00:00Z",
        "updated_at": "2025-06-01T13:00:00Z",
    },
]


def _fake_gh_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=returncode,
            stdout=stdout, stderr=stderr,
        )
    return runner


def _ref() -> PRRef:
    return PRRef(owner="o", repo="r", number=1)


def test_maps_live_anchored_comment() -> None:
    fake = _fake_gh_run(stdout=json.dumps(_SAMPLE))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        comments = fetch_pr_review_comments(_ref())

    by_id = {c.id: c for c in comments}
    c = by_id["gh-11"]
    assert c.file == "src/foo.py"
    assert c.side == "new"
    assert c.line == 42
    assert c.author == "alice"
    assert c.author_avatar_url == "https://example/alice.png"
    assert c.body == "Nit: use `Path`."
    assert c.body_html == "<p>Nit: use <code>Path</code>.</p>"
    assert c.commit_id == "deadbeef"
    assert c.html_url == "https://github.com/o/r/pull/1#discussion_r11"
    assert c.in_reply_to_id is None
    assert c.source == "github"


def test_falls_back_to_original_line_when_outdated() -> None:
    fake = _fake_gh_run(stdout=json.dumps(_SAMPLE))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        comments = fetch_pr_review_comments(_ref())

    by_id = {c.id: c for c in comments}
    assert by_id["gh-12"].line == 40
    assert by_id["gh-12"].in_reply_to_id == "gh-11"


def test_drops_unanchorable_records() -> None:
    fake = _fake_gh_run(stdout=json.dumps(_SAMPLE))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        comments = fetch_pr_review_comments(_ref())
    ids = {c.id for c in comments}
    assert ids == {"gh-11", "gh-12"}


def test_empty_response_returns_no_comments() -> None:
    fake = _fake_gh_run(stdout="[]")
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        assert fetch_pr_review_comments(_ref()) == []


def test_blank_response_returns_no_comments() -> None:
    fake = _fake_gh_run(stdout="")
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        assert fetch_pr_review_comments(_ref()) == []


def test_subprocess_failure_raises_gherror() -> None:
    fake = _fake_gh_run(stderr="HTTP 404", returncode=1)
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        with pytest.raises(GhError, match="404"):
            fetch_pr_review_comments(_ref())


def test_unparseable_json_raises_gherror() -> None:
    fake = _fake_gh_run(stdout="not json")
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        with pytest.raises(GhError, match="unparseable"):
            fetch_pr_review_comments(_ref())


def test_write_comments_file_matches_store_layout(tmp_path: Path) -> None:
    cs = [
        Comment(id="gh-2", file="b.py", side="new", line=1, body="b",
                source="github", author="a"),
        Comment(id="gh-1", file="a.py", side="new", line=2, body="a",
                source="github", author="a"),
    ]
    write_comments_file(tmp_path / "comments.json", cs)
    data = json.loads((tmp_path / "comments.json").read_text())
    # Sorted by (file, line, created_at) — a.py before b.py.
    assert [c["id"] for c in data["comments"]] == ["gh-1", "gh-2"]
    # All ingested fields round-trip.
    assert data["comments"][0]["source"] == "github"
    assert data["comments"][0]["author"] == "a"


def test_materialize_skips_when_comments_file_exists(tmp_path: Path) -> None:
    """Second-run idempotency: a pre-existing comments.json (e.g.
    session-local reviewer notes) is left alone."""
    target = tmp_path / "comments.json"
    target.write_text(json.dumps({"comments": []}))
    # gh shouldn't even be called.
    with patch("semantic_code_review.git_ops.subprocess.run") as run_mock:
        n = materialize_pr_comments(tmp_path, _ref())
    assert n == 0
    assert run_mock.call_count == 0


def test_materialize_soft_fails_on_gh_error(tmp_path: Path) -> None:
    fake = _fake_gh_run(stderr="boom", returncode=1)
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        n = materialize_pr_comments(tmp_path, _ref())
    assert n == 0
    # File NOT created — failure left the run dir untouched so a later
    # retry (e.g. a re-materialise) has a clean shot.
    assert not (tmp_path / "comments.json").exists()


def test_materialize_writes_comments_json(tmp_path: Path) -> None:
    fake = _fake_gh_run(stdout=json.dumps(_SAMPLE))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        n = materialize_pr_comments(tmp_path, _ref())
    assert n == 2
    data = json.loads((tmp_path / "comments.json").read_text())
    ids = {c["id"] for c in data["comments"]}
    assert ids == {"gh-11", "gh-12"}
