"""Viewer JSON + HTML: structural correctness on the fixture."""

from __future__ import annotations

import json
from pathlib import Path

from semantic_code_review.format.parse import parse_augmented_diff
from semantic_code_review.viewer.build_json import (
    build_pending_viewer_json, build_viewer_json,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


def _data():
    text = FIXTURE.read_text(encoding="utf-8")
    diff = parse_augmented_diff(text)
    return build_viewer_json(diff, {
        "title": "Introduce pagination",
        "body": "",
        "author": {"login": "octocat"},
        "url": "https://github.com/acme/widgets/pull/482",
    })


def test_viewer_json_shape() -> None:
    d = _data()
    assert d["version"] == "1"
    assert d["pr"]["title"] == "Introduce pagination"
    assert d["pr"]["number"] == 482
    assert d["pr"]["repo"] == "acme/widgets"
    assert d["pr"]["base_sha"] == "7c3a2b1"
    assert d["pr"]["summary"].startswith("Introduces pagination")
    assert "string-sql" in d["smells_catalogue"]
    assert d["smells_catalogue"]["string-sql"]["severity"] == "major"


def test_viewer_json_files_and_hunks() -> None:
    d = _data()
    assert len(d["files"]) == 1
    f = d["files"][0]
    assert f["id"] == "F0"
    assert f["path"] == "src/users.py"
    assert f["language"] == "python"
    assert f["adds"] == 7 and f["dels"] == 2
    assert len(f["hunks"]) == 1

    h = f["hunks"][0]
    assert h["id"] == "H0_0"
    assert h["intent"].startswith("Pagination")
    assert h["confidence"] == 85
    assert len(h["segments"]) == 2
    assert h["segments"][0]["id"] == "H0_0_S0"
    assert h["segments"][0]["new_start"] == 1 and h["segments"][0]["new_count"] == 3
    assert h["segments"][0]["smells"][0]["tag"] == "string-sql"
    assert h["segments"][1]["new_start"] == 5 and h["segments"][1]["new_count"] == 3

    # rows carry the side-by-side structure: two pairs + five ins rows
    # (hunk replaces 2 old lines with 7 new, so 2 are paired and 5 are inserts).
    rows = h["rows"]
    kinds = [r["kind"] for r in rows]
    assert kinds.count("pair") == 2
    assert kinds.count("ins") == 5
    # First row is the pair (list_users → paginate).
    assert rows[0]["kind"] == "pair"
    assert rows[0]["old_text"].startswith("def list_users(db):")
    assert rows[0]["new_text"].startswith("def paginate(")
    # Line numbers advance correctly.
    assert rows[0]["old_line"] == 1 and rows[0]["new_line"] == 1
    assert rows[-1]["new_line"] == 7 and rows[-1]["old_line"] is None


_RAW_DIFF = """diff --git a/foo.py b/foo.py
index 0123456..89abcde 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
"""


def test_build_pending_viewer_json_emits_skeleton_with_pending_flag(tmp_path: Path) -> None:
    """Pre-augment JSON carries file/hunk structure but no annotations
    and is tagged `pending: true` so the viewer JS shows a spinner
    placeholder instead of the failure copy."""
    (tmp_path / "raw.diff").write_text(_RAW_DIFF, encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "title": "Bump return value",
        "author": {"login": "tester"},
        "url": "",
        "baseRefOid": "aaa",
        "headRefOid": "bbb",
    }), encoding="utf-8")

    data = build_pending_viewer_json(tmp_path)

    assert data["pending"] is True
    assert data["pr"]["title"] == "Bump return value"
    assert data["pr"]["base_sha"] == "aaa"
    assert data["pr"]["head_sha"] == "bbb"
    # Structure is present even though annotations are empty.
    assert len(data["files"]) == 1
    f = data["files"][0]
    assert f["path"] == "foo.py"
    assert f["adds"] == 1 and f["dels"] == 1
    assert len(f["hunks"]) == 1
    h = f["hunks"][0]
    assert h["id"] == "H0_0"
    assert h["intent"] == ""
    assert h["smells"] == []
    # No overview yet → no themes / groups.
    assert data["pr"]["themes"] == []
    assert data["groups"] == []


_SYMBOL_DIFF = """diff --git a/a.py b/a.py
index 0123456..89abcde 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,6 @@
 def foo():
     return 1
+
+
+def bar():
+    return 2
"""


def test_symbol_blocks_map_changed_symbols_to_hunks(tmp_path: Path) -> None:
    """The deterministic Symbols axis: each changed symbol becomes a
    flat block carrying the hunk ids its live-side range overlaps."""
    (tmp_path / "raw.diff").write_text(_SYMBOL_DIFF, encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "title": "Add bar", "author": {"login": "t"}, "url": "",
        "baseRefOid": "aaa", "headRefOid": "bbb",
    }), encoding="utf-8")
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    (base / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (head / "a.py").write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n", encoding="utf-8",
    )

    data = build_pending_viewer_json(tmp_path)

    syms = data["symbols"]
    # foo is unchanged (same range both sides) → only bar, the added fn.
    assert len(syms) == 1
    block = syms[0]
    assert block["id"] == "SY0"
    assert block["title"] == "bar"
    assert "added" in block["rationale"] and "a.py" in block["rationale"]
    # bar (head lines 5-6) overlaps the single hunk H0_0 (new lines 1-6).
    assert block["hunk_ids"] == ["H0_0"]


_NESTED_DIFF = """diff --git a/a.py b/a.py
index 0123456..89abcde 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,6 @@
 class Foo:
     def bar(self):
         return 1
+
+    def baz(self):
+        return 2
"""


def test_symbol_blocks_nest_methods_under_their_class(tmp_path: Path) -> None:
    """Slice 5: a changed method renders under its (possibly unchanged)
    class. Adding `Foo.baz` grows `Foo`'s span (so the class is itself a
    changed node); `baz` hangs off it as a child, and the parent's
    hunk_ids is the subtree union."""
    (tmp_path / "raw.diff").write_text(_NESTED_DIFF, encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "title": "Add Foo.baz", "author": {"login": "t"}, "url": "",
        "baseRefOid": "aaa", "headRefOid": "bbb",
    }), encoding="utf-8")
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    (base / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n", encoding="utf-8",
    )
    (head / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n\n"
        "    def baz(self):\n        return 2\n", encoding="utf-8",
    )

    data = build_pending_viewer_json(tmp_path)

    syms = data["symbols"]
    # One root: the class. bar is untouched (identical span) → no pill.
    assert len(syms) == 1
    foo = syms[0]
    assert foo["id"] == "SY0"
    assert foo["title"] == "Foo"
    assert "modified" in foo["rationale"]
    assert foo["hunk_ids"] == ["H0_0"]      # subtree union
    # baz nests under Foo as the only child.
    children = foo["children"]
    assert len(children) == 1
    baz = children[0]
    assert baz["id"] == "SY1"
    assert baz["title"] == "baz"
    assert "added" in baz["rationale"]
    assert baz["hunk_ids"] == ["H0_0"]
    assert "children" not in baz           # leaf carries no children key


def test_symbol_blocks_absent_without_worktrees(tmp_path: Path) -> None:
    """No base/head worktree available ⇒ empty Symbols axis, no raise."""
    (tmp_path / "raw.diff").write_text(_SYMBOL_DIFF, encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "title": "Add bar", "author": {"login": "t"}, "url": "",
        "baseRefOid": "aaa", "headRefOid": "bbb",
    }), encoding="utf-8")

    data = build_pending_viewer_json(tmp_path)

    assert data["symbols"] == []
