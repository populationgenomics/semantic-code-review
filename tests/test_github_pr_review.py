"""Unit tests for the GitHub PR-review helpers in `review/github.py`."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from semantic_code_review.review import github as gh
from semantic_code_review.review.comments import Comment

# ---------------------------------------------------------------------------
# side mapping
# ---------------------------------------------------------------------------


def test_map_side_old_to_left() -> None:
    assert gh.map_side("old") == "LEFT"


def test_map_side_new_to_right() -> None:
    assert gh.map_side("new") == "RIGHT"


def test_map_side_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        gh.map_side("BOTH")


# ---------------------------------------------------------------------------
# comments_to_github
# ---------------------------------------------------------------------------


def _comment(**kw) -> Comment:
    base = dict(id="c1", file="a.py", side="new", line=10, body="hi")
    base.update(kw)
    return Comment(**base)


def test_comments_to_github_maps_basic_fields() -> None:
    cs = [_comment(side="old", line=5, body="left side"), _comment(id="c2", side="new", line=12, body="right side")]
    posted = gh.comments_to_github(cs)
    assert [(p.path, p.line, p.side, p.body) for p in posted] == [
        ("a.py", 5, "LEFT", "left side"),
        ("a.py", 12, "RIGHT", "right side"),
    ]


def test_comments_to_github_drops_malformed_dict() -> None:
    # Dict with no body should be silently dropped, not raise.
    bad = {"file": "a.py", "side": "new", "line": 1, "body": ""}
    good = {"file": "a.py", "side": "new", "line": 2, "body": "ok"}
    out = gh.comments_to_github([bad, good])
    assert len(out) == 1
    assert out[0].body == "ok"


def test_comments_to_github_filters_ingested_comments() -> None:
    """Ingested github comments are already upstream — re-posting would
    duplicate them. They must drop out of the mapper."""
    cs = [
        _comment(id="gh-123", source="github", author="alice", body="upstream"),
        _comment(id="local-1", source="local", body="new local"),
    ]
    out = gh.comments_to_github(cs)
    assert [p.body for p in out] == ["new local"]


def test_comments_to_github_emits_reply_using_parent_node_id() -> None:
    """A local comment with in_reply_to_id pointing at an ingested
    parent becomes a reply payload that carries the *node id* (opaque
    string), not the integer databaseId — that's what GraphQL's
    addPullRequestReviewComment mutation wants."""
    cs = [
        _comment(
            id="gh-3331909762",
            source="github",
            body="upstream",
            node_id="PRRC_kw1234",
        ),
        _comment(
            id="local-1",
            source="local",
            body="reply text",
            in_reply_to_id="gh-3331909762",
        ),
    ]
    out = gh.comments_to_github(cs)
    # Ingested parent filtered out; only the local reply remains.
    assert len(out) == 1
    p = out[0]
    assert p.is_reply
    assert p.in_reply_to_node_id == "PRRC_kw1234"
    assert p.body == "reply text"
    # Reply payload doesn't carry anchor fields.
    assert p.path is None and p.line is None and p.side is None


def test_comments_to_github_skips_reply_whose_parent_has_no_node_id() -> None:
    """A local reply to a parent that doesn't carry a node_id (a local
    draft, or an old ingest from before the node_id field landed) gets
    dropped with a warning — there's nothing upstream to thread to in
    a single submission."""
    cs = [
        _comment(id="local-root", source="local", body="root"),
        _comment(
            id="local-reply",
            source="local",
            body="reply",
            in_reply_to_id="local-root",
        ),
    ]
    out = gh.comments_to_github(cs)
    assert len(out) == 1
    assert out[0].body == "root"
    assert out[0].is_reply is False


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# list_review_requested_prs
# ---------------------------------------------------------------------------


def test_list_review_requested_prs_parses_json(monkeypatch) -> None:
    payload = [
        {
            "number": 42,
            "title": "Add subgrid",
            "author": {"login": "alice"},
            "headRefName": "feat/subgrid",
            "baseRefName": "main",
            "updatedAt": "2026-04-20T10:00:00Z",
            "url": "https://github.com/o/r/pull/42",
        }
    ]
    captured: dict = {}

    def fake_run(cmd, *, capture_output=False, text=False, check=False, **kw):
        captured["cmd"] = cmd
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    prs = gh.list_review_requested_prs("o/r")

    assert "is:open review-requested:@me" in captured["cmd"]
    assert len(prs) == 1
    assert prs[0].number == 42
    assert prs[0].author == "alice"
    assert prs[0].head_ref == "feat/subgrid"
    assert prs[0].base_ref == "main"


# ---------------------------------------------------------------------------
# pick_pr_interactive
# ---------------------------------------------------------------------------


def _prs() -> list[gh.OpenPR]:
    return [
        gh.OpenPR(number=42, title="A", author="alice", head_ref="x", base_ref="main", updated_at="", url=""),
        gh.OpenPR(number=45, title="B", author="bob", head_ref="y", base_ref="main", updated_at="", url=""),
        gh.OpenPR(number=50, title="C", author="carol", head_ref="z", base_ref="main", updated_at="", url=""),
    ]


def test_picker_selects_by_number() -> None:
    out = io.StringIO()
    in_ = io.StringIO("2\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen == 45


def test_picker_quit_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("q\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None


def test_picker_eof_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None


def test_picker_out_of_range_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("9\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None
    assert "invalid selection" in out.getvalue()


def test_picker_garbage_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("not-a-number\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None


# ---------------------------------------------------------------------------
# task wiring — the PR flow must build the same console/fold tasks as the
# local review flow, or /console/ask 409s on PR reviews (regression).
# ---------------------------------------------------------------------------


def _pr_opts(tmp_path, *, augment: bool):
    from semantic_code_review.augment.agents import Client
    from semantic_code_review.review.pr_flow import PrFlowOptions

    return PrFlowOptions(
        repo="o/r",
        number=1,
        runs_root=tmp_path,
        augment=augment,
        model="claude-opus-4-7",
        concurrency=4,
        no_cache=True,
        cache_dir=None,
        open_browser=False,
        port=0,
        timeout=1,
        extra_review_prompt=None,
        client=Client(model="anthropic:claude-opus-4-7"),
        yes=True,
    )


def test_build_tasks_wires_console_when_augmenting(tmp_path) -> None:
    from semantic_code_review.review.pr_flow import _build_tasks

    augment, fold, console = _build_tasks(_pr_opts(tmp_path, augment=True), tmp_path)
    assert augment is not None
    assert fold is not None
    assert console is not None  # the console callback the server installs


def test_build_tasks_returns_none_triple_without_augment(tmp_path) -> None:
    from semantic_code_review.review.pr_flow import _build_tasks

    assert _build_tasks(_pr_opts(tmp_path, augment=False), tmp_path) == (None, None, None)
