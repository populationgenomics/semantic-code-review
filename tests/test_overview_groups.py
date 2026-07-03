"""Unit tests for Overview.groups parsing + viewer JSON translation.

Covers `_resolve_groups` in overview.py (drop-invalid-members defensive
pattern) and `_group_blocks` in build_json.py (path → fileIdx mapping
to stable H{fi}_{hi} hunk ids).
"""

from __future__ import annotations

from semantic_code_review.augment.overview import apply_overview_to_diff
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    Overview,
    ParsedHunk,
    PRInfo,
)
from semantic_code_review.viewer.build_json import build_viewer_json


def _hunk(header: str, *, old_start: int, old_count: int, new_start: int, new_count: int) -> AnnotatedHunk:
    return AnnotatedHunk(parsed=ParsedHunk(
        header=header, body="",
        old_start=old_start, old_count=old_count,
        new_start=new_start, new_count=new_count,
    ))


def _make_diff() -> AnnotatedDiff:
    return AnnotatedDiff(
        pr=PRInfo(pr_url="", base_sha="a", head_sha="b"),
        files=[
            AnnotatedFile(
                path="src/a.py",
                diff_git_line="diff --git a/src/a.py b/src/a.py",
                hunks=[
                    _hunk("@@ -1,2 +1,2 @@", old_start=1, old_count=2, new_start=1, new_count=2),
                    _hunk("@@ -10,2 +10,2 @@", old_start=10, old_count=2, new_start=10, new_count=2),
                ],
            ),
            AnnotatedFile(
                path="src/b.py",
                diff_git_line="diff --git a/src/b.py b/src/b.py",
                hunks=[
                    _hunk("@@ -1,3 +1,4 @@", old_start=1, old_count=3, new_start=1, new_count=4),
                ],
            ),
        ],
    )


def test_resolve_groups_happy_path() -> None:
    diff = _make_diff()
    diff = apply_overview_to_diff(diff, {
        "summary": "ok",
        "files": [{"path": "src/a.py"}, {"path": "src/b.py"}],
        "groups": [
            {
                "title": "alpha refactor",
                "rationale": "shared theme",
                "members": [
                    {"path": "src/a.py", "hunk_index": 0},
                    {"path": "src/b.py", "hunk_index": 0},
                ],
            },
        ],
    })
    assert isinstance(diff.overview, Overview)
    assert len(diff.overview.groups) == 1
    g = diff.overview.groups[0]
    assert g.title == "alpha refactor"
    assert g.rationale == "shared theme"
    assert [(m.path, m.hunk_index) for m in g.members] == [
        ("src/a.py", 0), ("src/b.py", 0),
    ]


def test_resolve_groups_drops_bad_members() -> None:
    diff = _make_diff()
    diff = apply_overview_to_diff(diff, {
        "summary": "", "files": [{"path": "src/a.py"}],
        "groups": [
            {
                "title": "keep me",
                "members": [
                    {"path": "src/a.py", "hunk_index": 0},      # ok
                    {"path": "src/a.py", "hunk_index": 99},     # out of range
                    {"path": "does/not/exist.py", "hunk_index": 0},  # unknown path
                    {"path": "src/a.py"},                       # missing index
                ],
            },
            {"title": "all invalid", "members": [{"path": "x.py", "hunk_index": 0}]},
            {"title": "", "members": [{"path": "src/a.py", "hunk_index": 0}]},  # no title
        ],
    })
    assert isinstance(diff.overview, Overview)
    groups = diff.overview.groups
    assert len(groups) == 1
    assert groups[0].title == "keep me"
    assert [(m.path, m.hunk_index) for m in groups[0].members] == [("src/a.py", 0)]


def test_viewer_json_group_blocks() -> None:
    diff = _make_diff()
    diff = apply_overview_to_diff(diff, {
        "summary": "",
        "files": [{"path": "src/a.py"}, {"path": "src/b.py"}],
        "groups": [
            {
                "title": "cross-file",
                "rationale": "both sides of the API",
                "members": [
                    {"path": "src/a.py", "hunk_index": 1},
                    {"path": "src/b.py", "hunk_index": 0},
                ],
            },
            {
                "title": "a only",
                "members": [{"path": "src/a.py", "hunk_index": 0}],
            },
        ],
    })
    payload = build_viewer_json(diff, meta={}, head_dir=None)
    groups = payload["groups"]
    assert [g["id"] for g in groups] == ["G0", "G1"]
    assert groups[0]["title"] == "cross-file"
    assert groups[0]["hunk_ids"] == ["H0_1", "H1_0"]
    assert groups[1]["hunk_ids"] == ["H0_0"]


def test_viewer_json_no_groups() -> None:
    diff = _make_diff()
    diff = apply_overview_to_diff(diff, {"summary": "", "files": []})
    payload = build_viewer_json(diff, meta={}, head_dir=None)
    assert payload["groups"] == []
