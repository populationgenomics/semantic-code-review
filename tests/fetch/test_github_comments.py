"""fetch.github_comments: PR review-comment ingest from gh api."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from semantic_code_review.fetch import PRRef
from semantic_code_review.fetch.github_comments import (
    decorate_with_head_anchors,
    fetch_comment_commits,
    fetch_pr_review_comments,
    fetch_review_thread_resolution,
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
        "line": None,  # outdated comment
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
            args=args[0] if args else [],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
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
        Comment(id="gh-2", file="b.py", side="new", line=1, body="b", source="github", author="a"),
        Comment(id="gh-1", file="a.py", side="new", line=2, body="a", source="github", author="a"),
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


# ---------------------------------------------------------------------------
# Thread resolution (GraphQL)
# ---------------------------------------------------------------------------


_GRAPHQL_OK = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "isResolved": True,
                            "comments": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {"id": "PRRC_node11", "databaseId": 11},
                                    {"id": "PRRC_node12", "databaseId": 12},
                                ],
                            },
                        },
                        {
                            "isResolved": False,
                            "comments": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"id": "PRRC_node99", "databaseId": 99}],
                            },
                        },
                    ],
                },
            },
        },
    },
}


def test_fetch_resolution_maps_databaseid_to_thread_flag() -> None:
    fake = _fake_gh_run(stdout=json.dumps(_GRAPHQL_OK))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        m = fetch_review_thread_resolution(_ref())
    assert m == {11: True, 12: True, 99: False}


def test_fetch_metadata_maps_databaseid_to_resolution_and_node_id() -> None:
    """The richer metadata path returns both fields per comment so the
    GraphQL post path has the node id it needs for reply mutations."""
    from semantic_code_review.fetch.github_comments import fetch_review_thread_metadata

    fake = _fake_gh_run(stdout=json.dumps(_GRAPHQL_OK))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        m = fetch_review_thread_metadata(_ref())
    assert set(m.keys()) == {11, 12, 99}
    assert m[11].thread_resolved is True and m[11].node_id == "PRRC_node11"
    assert m[12].thread_resolved is True and m[12].node_id == "PRRC_node12"
    assert m[99].thread_resolved is False and m[99].node_id == "PRRC_node99"


def test_fetch_resolution_propagates_graphql_errors() -> None:
    fake = _fake_gh_run(stdout=json.dumps({"errors": [{"message": "rate limited"}]}))
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake):
        with pytest.raises(GhError, match="rate limited"):
            fetch_review_thread_resolution(_ref())


def test_fetch_comments_decorates_with_thread_resolved_and_node_id() -> None:
    """fetch_pr_review_comments fires two gh subprocesses: the REST
    comments call and the GraphQL metadata call. Each ingested comment
    is decorated with the thread flag and the GraphQL node_id matching
    its databaseId."""
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append(list(argv))
        # Order matters: REST first, GraphQL second — see
        # fetch_pr_review_comments.
        is_graphql = "graphql" in argv
        body = _GRAPHQL_OK if is_graphql else _SAMPLE
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps(body),
            stderr="",
        )

    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=runner):
        comments = fetch_pr_review_comments(_ref())

    by_id = {c.id: c for c in comments}
    assert by_id["gh-11"].thread_resolved is True
    assert by_id["gh-11"].node_id == "PRRC_node11"
    assert by_id["gh-12"].thread_resolved is True
    assert by_id["gh-12"].node_id == "PRRC_node12"
    assert len(calls) == 2


def test_fetch_comment_commits_batches_unique_ids(tmp_path: Path) -> None:
    """One git-fetch with every distinct commit_id, deduped, sorted."""
    cs = [
        Comment(id="gh-1", file="a.py", side="new", line=1, body="x", source="github", commit_id="aaa"),
        Comment(id="gh-2", file="a.py", side="new", line=2, body="y", source="github", commit_id="aaa"),  # duplicate
        Comment(id="gh-3", file="a.py", side="new", line=3, body="z", source="github", commit_id="bbb"),
        # Skipped: missing commit_id, not github-sourced.
        Comment(id="gh-4", file="a.py", side="new", line=4, body="w", source="github"),
        Comment(id="local-1", file="a.py", side="new", line=5, body="local", source="local", commit_id="ccc"),
    ]
    fake = _fake_gh_run(stdout="", returncode=0)
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=fake) as run_mock:
        fetched = fetch_comment_commits(tmp_path, cs)
    assert fetched == {"aaa", "bbb"}
    # Single batched fetch, deduped + sorted.
    cmds = [c[0][0] for c in run_mock.call_args_list]
    fetches = [c for c in cmds if "fetch" in c]
    assert len(fetches) == 1
    assert fetches[0][-2:] == ["aaa", "bbb"]


def test_fetch_comment_commits_falls_back_per_sha_on_failure(tmp_path: Path) -> None:
    """A 404 on one commit (force-push >90d ago) doesn't sink the rest:
    the batch fails, then we retry one-by-one and return the survivors."""
    cs = [
        Comment(id="gh-1", file="a.py", side="new", line=1, body="x", source="github", commit_id="good1"),
        Comment(id="gh-2", file="a.py", side="new", line=2, body="y", source="github", commit_id="bad"),
        Comment(id="gh-3", file="a.py", side="new", line=3, body="z", source="github", commit_id="good2"),
    ]

    def runner(argv, *args, **kwargs):
        # Batch call has all three SHAs at the end of argv. Fail it.
        if (
            argv[-3:] == ["bad", "good1", "good2"]
            or argv[-3:] == ["good1", "bad", "good2"]
            or argv[-3:] == ["good1", "good2", "bad"]
            or ("bad" in argv and "good1" in argv and "good2" in argv)
        ):
            raise subprocess.CalledProcessError(1, argv, stderr="HTTP 404")
        # Per-SHA retry: fail only the bad one.
        if argv[-1] == "bad":
            raise subprocess.CalledProcessError(1, argv, stderr="HTTP 404")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=runner):
        fetched = fetch_comment_commits(tmp_path, cs)
    assert fetched == {"good1", "good2"}


def test_fetch_comment_commits_skips_when_no_remote_commits(tmp_path: Path) -> None:
    """No github-sourced commit_ids → no fetch at all (avoid pointless
    network call for a runs that only ever held local comments)."""
    cs = [
        Comment(id="local-1", file="a.py", side="new", line=1, body="x", source="local"),
    ]
    with patch("semantic_code_review.git_ops.subprocess.run") as run_mock:
        fetched = fetch_comment_commits(tmp_path, cs)
    assert fetched == set()
    assert run_mock.call_count == 0


def test_decorate_with_head_anchors_short_circuits_at_head(tmp_path: Path) -> None:
    """A comment whose commit_id equals head_sha is already at head;
    head_line copies line and the propagator is never called."""
    cs = [
        Comment(id="gh-1", file="a.py", side="new", line=10, body="x", source="github", commit_id="HEAD"),
    ]
    with patch("semantic_code_review.git_ops.subprocess.run") as run_mock:
        decorate_with_head_anchors(tmp_path, "HEAD", cs)
    assert cs[0].head_line == 10
    assert cs[0].anchor_status == "anchored"
    assert run_mock.call_count == 0


def test_decorate_skips_local_and_old_side_comments(tmp_path: Path) -> None:
    """side=old comments live on the PR's base (stable) — skipped.
    Local comments are authored at head — skipped."""
    cs = [
        Comment(id="gh-1", file="a.py", side="old", line=1, body="x", source="github", commit_id="other"),
        Comment(id="local-1", file="a.py", side="new", line=2, body="y", source="local"),
    ]
    with patch("semantic_code_review.git_ops.subprocess.run") as run_mock:
        decorate_with_head_anchors(tmp_path, "HEAD", cs)
    # Both untouched.
    assert cs[0].head_line is None
    assert cs[0].anchor_status is None
    assert cs[1].head_line is None
    assert cs[1].anchor_status is None
    assert run_mock.call_count == 0


def test_decorate_caches_diff_per_commit_path_pair(tmp_path: Path) -> None:
    """Three comments on the same (commit_id, path) → exactly one
    `git diff` call, not three. Two commits checked once each."""
    cs = [
        Comment(id="gh-1", file="a.py", side="new", line=20, body="x", source="github", commit_id="OLD"),
        Comment(id="gh-2", file="a.py", side="new", line=30, body="y", source="github", commit_id="OLD"),
        Comment(id="gh-3", file="a.py", side="new", line=40, body="z", source="github", commit_id="OLD"),
    ]
    diff = "@@ -10,3 +10,2 @@\n-a\n-b\n-c\n+A\n+B\n"

    def runner(argv, *args, **kwargs):
        if "cat-file" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        if "diff" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=diff, stderr="")
        raise AssertionError(f"unexpected git call: {argv}")

    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=runner) as run_mock:
        decorate_with_head_anchors(tmp_path, "HEAD", cs)
    diff_calls = [c for c in run_mock.call_args_list if "diff" in c[0][0]]
    assert len(diff_calls) == 1
    # All three comments got propagated through the same diff.
    statuses = [c.anchor_status for c in cs]
    # Lines 20, 30, 40 are all below the hunk (10..12). Net -1 shift each.
    assert statuses == ["shifted", "shifted", "shifted"]
    assert [c.head_line for c in cs] == [19, 29, 39]


def test_decorate_writes_orphaned_for_lines_inside_removal_hunk(tmp_path: Path) -> None:
    cs = [
        Comment(id="gh-1", file="a.py", side="new", line=11, body="x", source="github", commit_id="OLD"),
    ]
    diff = "@@ -10,3 +10,0 @@\n-a\n-b\n-c\n"

    def runner(argv, *args, **kwargs):
        if "cat-file" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=diff, stderr="")

    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=runner):
        decorate_with_head_anchors(tmp_path, "HEAD", cs)
    assert cs[0].anchor_status == "orphaned"
    # First surviving line after the deletion = 10 (new_start + new_count = 9+0+1 actually wait)
    # @@ -10,3 +10,0 @@ means 3 lines deleted starting at old-10; new_start is 10, new_count 0.
    # Wait — git's convention for pure deletion: `@@ -10,3 +9,0 @@`. Let me re-check the test data.
    # The test diff is `@@ -10,3 +10,0 @@`. new_start + new_count = 10. Anchor at 10.
    assert cs[0].head_line == 10


def test_decorate_propagates_file_gone_sentinel(tmp_path: Path) -> None:
    cs = [
        Comment(id="gh-1", file="a.py", side="new", line=5, body="x", source="github", commit_id="OLD"),
    ]

    def runner(argv, *args, **kwargs):
        if "cat-file" in argv:
            # commit exists; file at head does not.
            if argv[-1].endswith(":a.py"):
                return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        raise AssertionError("should not reach diff")

    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=runner):
        decorate_with_head_anchors(tmp_path, "HEAD", cs)
    assert cs[0].anchor_status == "file_gone"
    assert cs[0].head_line is None


def test_fetch_comments_soft_fails_on_resolution_error() -> None:
    """An error from the GraphQL resolution call must not drop the
    comments — every entry just lands with thread_resolved=False."""

    def runner(argv, *args, **kwargs):
        if "graphql" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=1,
                stdout="",
                stderr="HTTP 500",
            )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps(_SAMPLE),
            stderr="",
        )

    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=runner):
        comments = fetch_pr_review_comments(_ref())
    assert len(comments) == 2
    assert all(c.thread_resolved is False for c in comments)
