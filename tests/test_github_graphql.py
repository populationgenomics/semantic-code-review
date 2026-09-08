"""The GraphQL primitives PR mode drives the pending review with, against
a fake `gh api graphql` (the `gh` fixture)."""

from __future__ import annotations

import pytest

from semantic_code_review import errors
from semantic_code_review.review import github as gh_rest
from semantic_code_review.review import github_graphql as gql
from tests.conftest import GhFailure, GhSequence

# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


def _state(*, pending: list[tuple[str, str]] = (), viewer: str = "alice", pr_id: str | None = "PR_kw1") -> dict:
    """A `query_pr_review_state` response with the pending reviews given
    as `(review id, author login)` pairs."""
    pr = None
    if pr_id is not None:
        pr = {"id": pr_id, "reviews": {"nodes": [{"id": rid, "author": {"login": who}} for rid, who in pending]}}
    return {"data": {"viewer": {"login": viewer}, "repository": {"pullRequest": pr}}}


def _thread(thread_id: str, comment_id: str) -> dict:
    return {
        "data": {
            "addPullRequestReviewThread": {"thread": {"id": thread_id, "comments": {"nodes": [{"id": comment_id}]}}}
        }
    }


def _pending_comments(nodes: list[dict], *, more: bool = False) -> dict:
    return {"data": {"node": {"comments": {"pageInfo": {"hasNextPage": more}, "nodes": nodes}}}}


def _pending_comment(node_id: str, dbid: int, path: str = "a.py", line: int | None = 3, **extra: object) -> dict:
    base: dict = {
        "id": node_id,
        "databaseId": dbid,
        "path": path,
        "line": line,
        "originalLine": line,
        "diffHunk": "@@ -1,3 +1,3 @@\n a\n-b\n+B",
        "body": f"body of {node_id}",
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedAt": "2026-09-01T10:05:00Z",
        "replyTo": None,
        "commit": {"oid": "c0ffee"},
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# The failure type
# ---------------------------------------------------------------------------


def test_a_gh_failure_is_a_refusal_the_session_can_answer_with(gh: GhSequence) -> None:
    """One class, two ancestries: a `ScrError` (502) for the route map,
    a `GhError` for callers that catch gh failures by that name."""
    gh.expect("query", GhFailure("HTTP 401: bad credentials"))
    with pytest.raises(gql.GitHubRefused, match="bad credentials") as excinfo:
        gql.query_pr_review_state("o/r", 1)
    assert isinstance(excinfo.value, errors.ScrError)
    assert isinstance(excinfo.value, gh_rest.GhError)
    assert excinfo.value.status == 502
    assert excinfo.value.body() == {"error": str(excinfo.value), "not_found": False}


@pytest.mark.parametrize(
    "failure",
    [
        GhFailure("GraphQL: Could not resolve to a node with the global id of 'PRRC_x' (update)"),
        {"errors": [{"type": "NOT_FOUND", "message": "gone"}]},
    ],
)
def test_github_not_knowing_a_node_is_a_not_found_refusal(gh: GhSequence, failure: object) -> None:
    """Whether gh relays it on stderr (exit 1) or the envelope carries a
    typed error, GitHub not knowing a node is the one refusal that means
    the store's view has diverged."""
    gh.expect("query", failure)  # type: ignore[arg-type]
    with pytest.raises(gql.GitHubRefused) as excinfo:
        gql.query_pr_review_state("o/r", 1)
    assert excinfo.value.not_found is True
    assert excinfo.value.body()["not_found"] is True


def test_other_refusals_are_not_not_found(gh: GhSequence) -> None:
    gh.expect("query", GhFailure("HTTP 502: bad gateway"))
    with pytest.raises(gql.GitHubRefused) as excinfo:
        gql.query_pr_review_state("o/r", 1)
    assert excinfo.value.not_found is False
    mixed = [{"type": "RATE_LIMITED", "message": "slow down"}, {"type": "NOT_FOUND", "message": "x"}]
    gh.expect("query", {"errors": mixed})
    with pytest.raises(gql.GitHubRefused) as excinfo:
        gql.query_pr_review_state("o/r", 1)
    assert excinfo.value.not_found is False


def test_graphql_errors_surface_their_messages(gh: GhSequence) -> None:
    gh.expect("query", {"errors": [{"message": "rate limited"}, {"message": "and again"}]})
    with pytest.raises(gql.GitHubRefused, match="rate limited; and again"):
        gql.query_pr_review_state("o/r", 1)


# ---------------------------------------------------------------------------
# query_pr_review_state
# ---------------------------------------------------------------------------


def test_query_pr_review_state_returns_the_viewers_pending_review(gh: GhSequence) -> None:
    gh.expect("query", _state(pending=[("PRR_other", "bob"), ("PRR_pending", "alice")]))
    state = gql.query_pr_review_state("o/r", 1)
    assert state == gql.PrReviewState(pr_node_id="PR_kw1", viewer_login="alice", pending_review_id="PRR_pending")
    assert gh.variables("query") == [{"owner": "o", "repo": "r", "number": "1"}]


def test_someone_elses_pending_review_is_not_ours(gh: GhSequence) -> None:
    gh.expect("query", _state(pending=[("PRR_someone_else", "bob")]))
    assert gql.query_pr_review_state("o/r", 1).pending_review_id is None


def test_query_pr_review_state_refuses_a_missing_pr_and_a_bad_repo(gh: GhSequence) -> None:
    gh.expect("query", _state(pr_id=None))
    with pytest.raises(gql.GitHubRefused, match="not found"):
        gql.query_pr_review_state("o/r", 1)
    with pytest.raises(gql.GitHubRefused, match="owner/name"):
        gql.query_pr_review_state("nonsense", 1)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _standing(review_id: str, state: str, url: str = "") -> dict:
    return {"pullRequestReview": {"id": review_id, "state": state, "url": url}}


def test_reconciliation_reads_each_comments_standing_and_the_pr_state(gh: GhSequence) -> None:
    """One call: the PR's pending review and where every asked-about
    comment stands. An unknown id comes back null beside a NOT_FOUND
    error, which is data, not a refusal."""
    response = _state(pending=[("PRR_p", "alice")])
    response["data"]["nodes"] = [
        {"id": "C1", **_standing("PRR_p", "PENDING")},
        None,
        {"id": "C3", **_standing("PRR_old", "APPROVED", "https://gh/o/r/pull/1#pullrequestreview-3")},
    ]
    response["errors"] = [{"type": "NOT_FOUND", "path": ["nodes", 1], "message": "Could not resolve to a node"}]
    gh.expect("query", response)

    result = gql.query_reconciliation("o/r", 1, ["C1", "C2", "C3"])

    assert result.state.pending_review_id == "PRR_p"
    assert result.standing["C1"] == gql.CommentStanding(review_id="PRR_p", review_state="PENDING", review_url="")
    assert result.standing["C1"].pending
    assert result.standing["C2"] is None
    assert result.standing["C3"] is not None and not result.standing["C3"].pending
    assert result.standing["C3"].review_url.endswith("pullrequestreview-3")
    # The ids ride inlined in the query, not as a variable.
    assert gh.variables("query") == [{"owner": "o", "repo": "r", "number": "1"}]


def test_reconciliation_survives_gh_exiting_one_on_a_partial_answer(gh: GhSequence) -> None:
    """The live shape: gh exits 1 and puts 'Could not resolve to a node'
    on stderr because the envelope carries a NOT_FOUND, while the data is
    complete. That is an answer — the comment is gone — not a refusal."""
    gh.expect(
        "query",
        {
            "data": {
                "viewer": {"login": "folded"},
                "repository": {"pullRequest": {"id": "PR_1", "reviews": {"nodes": []}}},
                "nodes": [None],
            },
            "errors": [
                {
                    "type": "NOT_FOUND",
                    "path": ["nodes", 0],
                    "locations": [{"line": 12, "column": 3}],
                    "message": "Could not resolve to a node with the global id of 'PRRC_gone'.",
                }
            ],
        },
    )
    result = gql.query_reconciliation("o/r", 1, ["PRRC_gone"])
    assert result.state.pending_review_id is None
    assert result.standing == {"PRRC_gone": None}


def test_a_not_found_the_caller_does_not_tolerate_is_still_a_refusal(gh: GhSequence) -> None:
    gh.expect(
        "deletePullRequestReviewComment",
        {
            "data": {"deletePullRequestReviewComment": None},
            "errors": [
                {
                    "type": "NOT_FOUND",
                    "path": ["deletePullRequestReviewComment"],
                    "message": "Could not resolve to a node with the global id of 'PRRC_gone'.",
                }
            ],
        },
    )
    with pytest.raises(gql.GitHubRefused) as e:
        gql.delete_review_comment("PRRC_gone")
    assert e.value.not_found


def test_reconciliation_with_nothing_to_ask_still_reads_the_pr_state(gh: GhSequence) -> None:
    response = _state()
    response["data"]["nodes"] = []
    gh.expect("query", response)
    result = gql.query_reconciliation("o/r", 1, [])
    assert result.state.pending_review_id is None and result.standing == {}


def test_reconciliation_batches_a_hundred_ids_per_call(gh: GhSequence) -> None:
    ids = [f"C{i}" for i in range(150)]
    first = _state(pending=[("PRR_p", "alice")])
    first["data"]["nodes"] = [{"id": i, **_standing("PRR_p", "PENDING")} for i in ids[:100]]
    second = _state(pending=[("PRR_p", "alice")])
    second["data"]["nodes"] = [{"id": i, **_standing("PRR_p", "PENDING")} for i in ids[100:]]
    gh.expect("query", first)
    gh.expect("query", second)
    result = gql.query_reconciliation("o/r", 1, ids)
    assert len(result.standing) == 150 and gh.ops() == ["query", "query"]


def test_reconciliation_relays_other_failures(gh: GhSequence) -> None:
    gh.expect("query", GhFailure("HTTP 401: bad credentials"))
    with pytest.raises(gql.GitHubRefused, match="bad credentials"):
        gql.query_reconciliation("o/r", 1, ["C1"])


# ---------------------------------------------------------------------------
# The pending review's comments
# ---------------------------------------------------------------------------


def test_listing_pending_comments_reads_side_off_the_hunk(gh: GhSequence) -> None:
    gh.expect(
        "pendingReviewComments",
        _pending_comments(
            [
                _pending_comment("C1", 11, diffHunk="@@ -1,3 +1,3 @@\n a\n-b"),
                _pending_comment("C2", 12, line=4, diffHunk="@@ -1,3 +1,3 @@\n a\n-b\n+B"),
                _pending_comment("C3", 13, diffHunk=""),
                _pending_comment("C4", 14, replyTo={"id": "C1"}, line=None, originalLine=3),
            ]
        ),
    )
    out = gql.list_pending_review_comments("PRR_1")
    assert [(c.node_id, c.database_id, c.side, c.line) for c in out] == [
        ("C1", 11, "LEFT", 3),
        ("C2", 12, "RIGHT", 4),
        ("C3", 13, "RIGHT", 3),
        ("C4", 14, "RIGHT", 3),
    ]
    assert out[3].in_reply_to_node_id == "C1" and out[0].in_reply_to_node_id is None
    assert out[0].commit_oid == "c0ffee" and out[0].body == "body of C1"
    assert out[0].created_at == "2026-09-01T10:00:00Z" and out[0].updated_at == "2026-09-01T10:05:00Z"
    assert gh.variables("pendingReviewComments") == [{"rid": "PRR_1"}]


def test_a_file_level_pending_comment_has_no_line(gh: GhSequence) -> None:
    gh.expect("pendingReviewComments", _pending_comments([_pending_comment("C1", 11, line=None, originalLine=None)]))
    assert gql.list_pending_review_comments("PRR_1")[0].line is None


def test_listing_pending_comments_refuses_a_non_review_and_a_torn_comment(gh: GhSequence) -> None:
    gh.expect("pendingReviewComments", {"data": {"node": None}})
    with pytest.raises(gql.GitHubRefused, match="not a pull request review"):
        gql.list_pending_review_comments("nope")
    gh.expect("pendingReviewComments", _pending_comments([{"id": "C1", "path": "a.py"}]))
    with pytest.raises(gql.GitHubRefused, match="missing id/databaseId/path"):
        gql.list_pending_review_comments("PRR_1")


def test_listing_pending_comments_warns_past_a_hundred(gh: GhSequence, caplog: pytest.LogCaptureFixture) -> None:
    gh.expect("pendingReviewComments", _pending_comments([_pending_comment("C1", 11)], more=True))
    gql.list_pending_review_comments("PRR_1")
    assert "more than 100" in caplog.text


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_create_pending_review_returns_its_id(gh: GhSequence) -> None:
    gh.expect("addPullRequestReview", {"data": {"addPullRequestReview": {"pullRequestReview": {"id": "PRR_new"}}}})
    assert gql.create_pending_review("PR_kw1") == "PRR_new"
    assert gh.variables("addPullRequestReview") == [{"pr": "PR_kw1"}]


def test_add_review_thread_returns_thread_and_comment_ids(gh: GhSequence) -> None:
    gh.expect("addPullRequestReviewThread", _thread("TH1", "C1"))
    assert gql.add_review_thread("PRR1", "a.py", 42, "RIGHT", "nit") == gql.NewThread(thread_id="TH1", comment_id="C1")
    (vars_,) = gh.variables("addPullRequestReviewThread")
    # `-F line=42` makes gh emit a JSON number; the string form is refused.
    assert vars_ == {"rid": "PRR1", "path": "a.py", "line": "42", "side": "RIGHT", "body": "nit"}


def test_add_review_thread_file_level_carries_no_line(gh: GhSequence) -> None:
    gh.expect("addPullRequestReviewThread", _thread("TH1", "C1"))
    gql.add_review_thread("PRR1", "a.py", None, None, "general")
    assert gh.variables("addPullRequestReviewThread") == [{"rid": "PRR1", "path": "a.py", "body": "general"}]


def test_a_null_thread_is_a_refusal_naming_the_line(gh: GhSequence) -> None:
    """GitHub answers a line outside the diff with 200 and a null thread."""
    gh.expect("addPullRequestReviewThread", {"data": {"addPullRequestReviewThread": {"thread": None}}})
    with pytest.raises(gql.GitHubRefused, match="a.py:500"):
        gql.add_review_thread("PRR1", "a.py", 500, "RIGHT", "x")


def test_add_review_comment_reply_targets_the_parent_node_id(gh: GhSequence) -> None:
    gh.expect("addPullRequestReviewComment", {"data": {"addPullRequestReviewComment": {"comment": {"id": "C9"}}}})
    assert gql.add_review_comment_reply("PRR1", "PRRC_parent", "ack") == "C9"
    assert gh.variables("addPullRequestReviewComment") == [{"rid": "PRR1", "reply_to": "PRRC_parent", "body": "ack"}]


def test_update_review_comment_replaces_the_body(gh: GhSequence) -> None:
    gh.expect(
        "updatePullRequestReviewComment",
        {"data": {"updatePullRequestReviewComment": {"pullRequestReviewComment": {"id": "C1"}}}},
    )
    gql.update_review_comment("C1", "edited")
    assert gh.variables("updatePullRequestReviewComment") == [{"cid": "C1", "body": "edited"}]
    gh.expect("updatePullRequestReviewComment", {"data": {"updatePullRequestReviewComment": None}})
    with pytest.raises(gql.GitHubRefused, match="no comment"):
        gql.update_review_comment("C1", "again")


def test_delete_review_comment_addresses_the_comment(gh: GhSequence) -> None:
    gh.expect("deletePullRequestReviewComment", {"data": {"deletePullRequestReviewComment": {"pullRequestReview": {}}}})
    gql.delete_review_comment("C1")
    assert gh.variables("deletePullRequestReviewComment") == [{"cid": "C1"}]
    gh.expect("deletePullRequestReviewComment", {"data": {}})
    with pytest.raises(gql.GitHubRefused, match="returned nothing"):
        gql.delete_review_comment("C1")


def test_submit_review_returns_the_published_review(gh: GhSequence) -> None:
    gh.expect(
        "submitPullRequestReview",
        {
            "data": {
                "submitPullRequestReview": {
                    "pullRequestReview": {
                        "id": "PRR1",
                        "databaseId": 999,
                        "url": "https://gh/o/r/pull/1#pullrequestreview-999",
                    }
                }
            }
        },
    )
    result = gql.submit_review("PRR1", event="APPROVE", body="lgtm")
    assert result == gql.SubmittedReview(
        node_id="PRR1", database_id=999, url="https://gh/o/r/pull/1#pullrequestreview-999"
    )
    assert gh.variables("submitPullRequestReview") == [{"rid": "PRR1", "event": "APPROVE", "body": "lgtm"}]


def test_submit_review_refuses_an_unknown_event_before_calling_gh(gh: GhSequence) -> None:
    with pytest.raises(ValueError, match="event must be one of"):
        gql.submit_review("PRR1", event="SHIP_IT")
    assert gh.calls == []


@pytest.mark.parametrize(("resolved", "op"), [(True, "resolveReviewThread"), (False, "unresolveReviewThread")])
def test_set_thread_resolved_fires_the_matching_mutation(gh: GhSequence, resolved: bool, op: str) -> None:
    gh.expect(op, {"data": {op: {"thread": {"id": "TH1", "isResolved": resolved}}}})
    gql.set_thread_resolved("TH1", resolved)
    assert gh.variables(op) == [{"tid": "TH1"}]


def test_set_thread_resolved_refuses_when_the_thread_did_not_flip(gh: GhSequence) -> None:
    gh.expect("resolveReviewThread", {"data": {"resolveReviewThread": {"thread": {"id": "TH1", "isResolved": False}}}})
    with pytest.raises(gql.GitHubRefused, match="did not change"):
        gql.set_thread_resolved("TH1", True)
