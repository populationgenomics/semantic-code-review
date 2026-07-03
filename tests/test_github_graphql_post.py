"""Unit tests for the GraphQL post pipeline."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from semantic_code_review.review import github as gh_rest
from semantic_code_review.review import github_graphql as gh_gql
from semantic_code_review.review.comments import Comment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
    )


class _GhSequence:
    """Dispatches a list of ``gh api graphql`` calls in order, matching
    each call to the queued response by GraphQL operation name.

    Tests register expectations like ``q("query") -> {...}`` and
    ``q("addPullRequestReviewThread") -> {...}``; the dispatcher then
    plays them back as each call comes in, capturing the variables so
    assertions can inspect them.
    """

    def __init__(self) -> None:
        self.responses: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    def expect(self, op: str, response: dict) -> None:
        self.responses.setdefault(op, []).append(response)

    def __call__(self, argv, *args, **kwargs):
        # Extract the query string ("-f query=...") and variables.
        query = ""
        variables: dict[str, str] = {}
        for i, a in enumerate(argv):
            if a == "-f" and i + 1 < len(argv) and argv[i + 1].startswith("query="):
                query = argv[i + 1][len("query=") :]
            elif a in ("-f", "-F") and i + 1 < len(argv):
                kv = argv[i + 1]
                if "=" in kv and not kv.startswith("query="):
                    k, v = kv.split("=", 1)
                    variables[k] = v
        # Pick the operation by looking for known mutation/query names.
        op = "query"
        for name in (
            "addPullRequestReviewThread",
            "addPullRequestReviewComment",
            "addPullRequestReview",
            "submitPullRequestReview",
        ):
            if name in query and ("mutation" in query or "Mutation" in query):
                op = name
                break
        self.calls.append((op, variables))
        bucket = self.responses.get(op, [])
        if not bucket:
            raise AssertionError(f"unexpected gh graphql call: {op} (no response queued)")
        response = bucket.pop(0)
        return _proc(stdout=json.dumps(response))


# ---------------------------------------------------------------------------
# query_pr_review_state
# ---------------------------------------------------------------------------


def test_query_pr_review_state_returns_pending_review_when_present(monkeypatch) -> None:
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "alice"},
                "repository": {
                    "pullRequest": {
                        "id": "PR_kw1",
                        "reviews": {
                            "nodes": [
                                {"id": "PRR_pending", "author": {"login": "alice"}},
                                {"id": "PRR_other", "author": {"login": "bob"}},
                            ]
                        },
                    },
                },
            },
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        state = gh_gql.query_pr_review_state("o/r", 1)
    assert state.pr_node_id == "PR_kw1"
    assert state.viewer_login == "alice"
    assert state.pending_review_id == "PRR_pending"


def test_query_pr_review_state_returns_none_when_no_viewer_pending(monkeypatch) -> None:
    """A pending review owned by someone else is NOT ours to reuse."""
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "alice"},
                "repository": {
                    "pullRequest": {
                        "id": "PR_kw1",
                        "reviews": {
                            "nodes": [
                                {"id": "PRR_someone_else", "author": {"login": "bob"}},
                            ]
                        },
                    },
                },
            },
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        state = gh_gql.query_pr_review_state("o/r", 1)
    assert state.pending_review_id is None


def test_query_pr_review_state_raises_when_pr_missing(monkeypatch) -> None:
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {"viewer": {"login": "alice"}, "repository": {"pullRequest": None}},
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        with pytest.raises(gh_rest.GhError, match="not found"):
            gh_gql.query_pr_review_state("o/r", 1)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_create_pending_review_returns_review_id(monkeypatch) -> None:
    seq = _GhSequence()
    seq.expect(
        "addPullRequestReview",
        {
            "data": {"addPullRequestReview": {"pullRequestReview": {"id": "PRR_new"}}},
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        rid = gh_gql.create_pending_review("PR_kw1", body="hi")
    assert rid == "PRR_new"
    assert seq.calls[0][1]["pr"] == "PR_kw1"
    assert seq.calls[0][1]["body"] == "hi"


def test_add_review_thread_sends_typed_line(monkeypatch) -> None:
    seq = _GhSequence()
    seq.expect(
        "addPullRequestReviewThread",
        {
            "data": {"addPullRequestReviewThread": {"thread": {"id": "TH1"}}},
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq) as run_mock:
        tid = gh_gql.add_review_thread("PRR1", "a.py", 42, "RIGHT", "nit")
    assert tid == "TH1"
    # `-F line=42` makes gh emit a JSON number; verify the flag was used.
    argv = run_mock.call_args.args[0]
    assert "-F" in argv
    assert "line=42" in argv


def test_add_review_comment_reply_sends_parent_node_id(monkeypatch) -> None:
    seq = _GhSequence()
    seq.expect(
        "addPullRequestReviewComment",
        {
            "data": {"addPullRequestReviewComment": {"comment": {"id": "C1"}}},
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        cid = gh_gql.add_review_comment_reply("PRR1", "PRRC_parent", "ack")
    assert cid == "C1"
    assert seq.calls[0][1]["reply_to"] == "PRRC_parent"


def test_submit_review_returns_url_and_databaseId(monkeypatch) -> None:
    seq = _GhSequence()
    seq.expect(
        "submitPullRequestReview",
        {
            "data": {
                "submitPullRequestReview": {
                    "pullRequestReview": {
                        "id": "PRR1",
                        "databaseId": 999,
                        "url": "https://github.com/o/r/pull/1#pullrequestreview-999",
                    },
                },
            },
        },
    )
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        result = gh_gql.submit_review("PRR1")
    assert result["databaseId"] == 999
    assert "pullrequestreview-999" in result["url"]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _local(**kw) -> Comment:
    base = dict(id="local-1", file="a.py", side="new", line=10, body="hi", source="local")
    base.update(kw)
    return Comment(**base)


def _ingested(**kw) -> Comment:
    base = dict(
        id="gh-1",
        file="a.py",
        side="new",
        line=1,
        body="upstream",
        source="github",
        author="alice",
        node_id="PRRC_parent",
    )
    base.update(kw)
    return Comment(**base)


def test_post_creates_review_then_adds_threads_then_submits(monkeypatch) -> None:
    """No pending review exists → create one, add each thread, submit."""
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "alice"},
                "repository": {
                    "pullRequest": {
                        "id": "PR_kw1",
                        "reviews": {"nodes": []},  # no pending review
                    }
                },
            }
        },
    )
    seq.expect(
        "addPullRequestReview",
        {
            "data": {"addPullRequestReview": {"pullRequestReview": {"id": "PRR_new"}}},
        },
    )
    seq.expect(
        "addPullRequestReviewThread",
        {
            "data": {"addPullRequestReviewThread": {"thread": {"id": "TH1"}}},
        },
    )
    seq.expect(
        "addPullRequestReviewThread",
        {
            "data": {"addPullRequestReviewThread": {"thread": {"id": "TH2"}}},
        },
    )
    seq.expect(
        "submitPullRequestReview",
        {
            "data": {
                "submitPullRequestReview": {
                    "pullRequestReview": {"databaseId": 7, "url": "u"},
                }
            }
        },
    )

    cs = [
        _local(id="local-1", line=5, body="thread one"),
        _local(id="local-2", line=12, body="thread two"),
    ]
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        result = gh_gql.post_review_via_graphql("o/r", 1, cs)

    # Five gh calls in order.
    ops = [op for op, _ in seq.calls]
    assert ops == [
        "query",
        "addPullRequestReview",
        "addPullRequestReviewThread",
        "addPullRequestReviewThread",
        "submitPullRequestReview",
    ]
    assert result.posted == 2
    assert result.review_id == 7


def test_post_reuses_existing_pending_review_and_skips_create(monkeypatch) -> None:
    """A pending review owned by the viewer is reused — no create
    mutation fires."""
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "alice"},
                "repository": {
                    "pullRequest": {
                        "id": "PR_kw1",
                        "reviews": {"nodes": [{"id": "PRR_pending", "author": {"login": "alice"}}]},
                    }
                },
            }
        },
    )
    seq.expect(
        "addPullRequestReviewThread",
        {
            "data": {"addPullRequestReviewThread": {"thread": {"id": "TH1"}}},
        },
    )
    seq.expect(
        "submitPullRequestReview",
        {
            "data": {
                "submitPullRequestReview": {
                    "pullRequestReview": {"databaseId": 1, "url": ""},
                }
            }
        },
    )

    cs = [_local(body="nit")]
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        gh_gql.post_review_via_graphql("o/r", 1, cs)

    ops = [op for op, _ in seq.calls]
    assert "addPullRequestReview" not in ops
    assert ops == ["query", "addPullRequestReviewThread", "submitPullRequestReview"]
    # The thread mutation targeted the existing pending review id.
    thread_vars = next(v for op, v in seq.calls if op == "addPullRequestReviewThread")
    assert thread_vars["rid"] == "PRR_pending"


def test_post_routes_replies_to_reply_mutation(monkeypatch) -> None:
    """A local reply to an ingested parent (carrying node_id) becomes
    an addPullRequestReviewComment mutation against the parent's node id."""
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "alice"},
                "repository": {
                    "pullRequest": {
                        "id": "PR_kw1",
                        "reviews": {"nodes": []},
                    }
                },
            }
        },
    )
    seq.expect(
        "addPullRequestReview",
        {
            "data": {"addPullRequestReview": {"pullRequestReview": {"id": "PRR_new"}}},
        },
    )
    seq.expect(
        "addPullRequestReviewComment",
        {
            "data": {"addPullRequestReviewComment": {"comment": {"id": "C1"}}},
        },
    )
    seq.expect(
        "submitPullRequestReview",
        {
            "data": {
                "submitPullRequestReview": {
                    "pullRequestReview": {"databaseId": 9, "url": ""},
                }
            }
        },
    )

    cs = [
        _ingested(),  # gh-1 with node_id="PRRC_parent" (filtered)
        _local(body="ack", in_reply_to_id="gh-1"),
    ]
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        gh_gql.post_review_via_graphql("o/r", 1, cs)

    reply_vars = next(v for op, v in seq.calls if op == "addPullRequestReviewComment")
    assert reply_vars["reply_to"] == "PRRC_parent"
    assert reply_vars["body"] == "ack"
    # No addPullRequestReviewThread call — replies don't go through it.
    assert not any(op == "addPullRequestReviewThread" for op, _ in seq.calls)


def test_post_accepts_already_mapped_PostedComments(monkeypatch) -> None:
    """CLI calls comments_to_github once for the prompt count, then
    passes the mapped list here — orchestrator must accept either."""
    seq = _GhSequence()
    seq.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "alice"},
                "repository": {
                    "pullRequest": {
                        "id": "PR_kw1",
                        "reviews": {"nodes": []},
                    }
                },
            }
        },
    )
    seq.expect(
        "addPullRequestReview",
        {
            "data": {"addPullRequestReview": {"pullRequestReview": {"id": "PRR_new"}}},
        },
    )
    seq.expect(
        "addPullRequestReviewThread",
        {
            "data": {"addPullRequestReviewThread": {"thread": {"id": "TH"}}},
        },
    )
    seq.expect(
        "submitPullRequestReview",
        {
            "data": {
                "submitPullRequestReview": {
                    "pullRequestReview": {"databaseId": 1, "url": ""},
                }
            }
        },
    )

    posted = [gh_rest.PostedComment(body="x", path="a.py", line=1, side="RIGHT")]
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        result = gh_gql.post_review_via_graphql("o/r", 1, posted)
    assert result.posted == 1


def test_post_raises_when_no_postable_comments() -> None:
    """All-empty input never touches the network."""
    with patch("semantic_code_review.git_ops.subprocess.run") as run_mock:
        with pytest.raises(gh_rest.GhError, match="no postable"):
            gh_gql.post_review_via_graphql("o/r", 1, [])
    assert run_mock.call_count == 0
