"""Viewer JSON + HTML: structural correctness on the fixture."""

from __future__ import annotations

import json
from pathlib import Path

from semantic_code_review import paths
from semantic_code_review.format.parse import parse_augmented_diff
from semantic_code_review.viewer.build_json import (
    build_pending_viewer_json,
    build_viewer_json,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


def _data():
    text = FIXTURE.read_text(encoding="utf-8")
    diff = parse_augmented_diff(text)
    return build_viewer_json(
        diff,
        {
            "title": "Introduce pagination",
            "body": "",
            "author": {"login": "octocat"},
            "url": "https://github.com/acme/widgets/pull/482",
        },
    )


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


def test_build_pending_viewer_json_emits_skeleton_with_pending_flag(run_dir: paths.RunDir) -> None:
    """Pre-augment JSON carries file/hunk structure but no annotations
    and is tagged `pending: true` so the viewer JS shows a spinner
    placeholder instead of the failure copy."""
    run_dir.raw_diff.write_text(_RAW_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Bump return value",
                "author": {"login": "tester"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir)

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


_MIXED_DIFF = """diff --git a/foo.py b/foo.py
index 0123456..89abcde 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
diff --git a/uv.lock b/uv.lock
index 1111111..2222222 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,1 +1,1 @@
-version = "0.1"
+version = "0.2"
@@ -10,1 +10,1 @@
-x = 1
+x = 2
diff --git a/notes.txt b/notes.txt
index 3333333..4444444 100644
--- a/notes.txt
+++ b/notes.txt
@@ -1,1 +1,1 @@
-a
+b
"""


def test_build_pending_marks_skipped_files_generated(run_dir: paths.RunDir) -> None:
    """Skipped files (lock/vendored, plus config skip_globs) are pre-marked
    GENERATED in the pending page so the progress grid excludes them — the
    pipeline never dispatches them, so left "modified" their hunks would sit
    queued forever (the uv.lock-blocks-finalising bug)."""
    run_dir.raw_diff.write_text(_MIXED_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps({"title": "t", "author": {"login": "u"}, "url": "", "baseRefOid": "a", "headRefOid": "b"}),
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir, skip_globs=("*.txt",))

    status = {f["path"]: f["status"] for f in data["files"]}
    # Real source stays analysable; uv.lock (default glob) and notes.txt
    # (config glob) are skipped → generated, so progress.ts drops them.
    assert status["foo.py"] == "modified"
    assert status["uv.lock"] == "generated"
    assert status["notes.txt"] == "generated"


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


def test_symbol_blocks_map_changed_symbols_to_hunks(run_dir: paths.RunDir) -> None:
    """The deterministic Symbols axis: each changed symbol becomes a
    flat block carrying the hunk ids its live-side range overlaps."""
    run_dir.raw_diff.write_text(_SYMBOL_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Add bar",
                "author": {"login": "t"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )
    base = run_dir.base
    head = run_dir.head
    base.mkdir()
    head.mkdir()
    (base / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (head / "a.py").write_text(
        "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir)

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


def test_symbol_blocks_nest_methods_under_their_class(run_dir: paths.RunDir) -> None:
    """Slice 5: a changed method renders under its (possibly unchanged)
    class. Adding `Foo.baz` grows `Foo`'s span (so the class is itself a
    changed node); `baz` hangs off it as a child, and the parent's
    hunk_ids is the subtree union."""
    run_dir.raw_diff.write_text(_NESTED_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Add Foo.baz",
                "author": {"login": "t"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )
    base = run_dir.base
    head = run_dir.head
    base.mkdir()
    head.mkdir()
    (base / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n",
        encoding="utf-8",
    )
    (head / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n\n    def baz(self):\n        return 2\n",
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir)

    syms = data["symbols"]
    # One root: the class. bar is untouched (identical span) → no pill.
    assert len(syms) == 1
    foo = syms[0]
    assert foo["id"] == "SY0"
    assert foo["title"] == "Foo"
    assert foo["rationale"] == "class body changed in a.py"
    assert foo["hunk_ids"] == ["H0_0"]  # subtree union
    # baz nests under Foo as the only child.
    children = foo["children"]
    assert len(children) == 1
    baz = children[0]
    assert baz["id"] == "SY1"
    assert baz["title"] == "baz"
    assert "added" in baz["rationale"]
    assert baz["hunk_ids"] == ["H0_0"]
    assert "children" not in baz  # leaf carries no children key


_MOVED_DIFF = """diff --git a/a.py b/a.py
index 0123456..89abcde 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,5 @@
+HEADER = 1
+
 def keep():
     return 1
 def other():
"""


def test_a_moved_only_symbol_is_context_not_a_pill(run_dir: paths.RunDir) -> None:
    """Byte-identical code that shifted lines is not a change, so it earns
    no pill of its own — the same treatment an unchanged ancestor gets.
    Without this the axis is mostly noise: on a measured 6-file diff, 244
    of 262 same-name-both-sides symbols had only shifted."""
    run_dir.raw_diff.write_text(_MOVED_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Add a header constant",
                "author": {"login": "t"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )
    base = run_dir.base
    head = run_dir.head
    base.mkdir()
    head.mkdir()
    body = "def keep():\n    return 1\ndef other():\n    return 2\n"
    (base / "a.py").write_text(body, encoding="utf-8")
    (head / "a.py").write_text("HEADER = 1\n\n" + body, encoding="utf-8")

    data = build_pending_viewer_json(run_dir)

    # `keep` and `other` both shifted two lines down; only HEADER is new.
    assert [b["title"] for b in data["symbols"]] == ["HEADER"]


def test_symbol_blocks_absent_without_worktrees(run_dir: paths.RunDir) -> None:
    """No base/head worktree available ⇒ empty Symbols axis, no raise."""
    run_dir.raw_diff.write_text(_SYMBOL_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Add bar",
                "author": {"login": "t"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir)

    assert data["symbols"] == []


# --- fold_symbols: per-side definition spans (slice 1) ---------------------


def test_fold_symbols_ship_per_side_definition_spans(run_dir: paths.RunDir) -> None:
    """Each supported-language file carries its head/base definition spans
    as `{start_line, end_line, kind, qualified_name, depth}`, depth-first,
    with nested defs deeper than their enclosing one."""
    run_dir.raw_diff.write_text(_NESTED_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Add Foo.baz",
                "author": {"login": "t"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )
    base = run_dir.base
    head = run_dir.head
    base.mkdir()
    head.mkdir()
    (base / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n",
        encoding="utf-8",
    )
    (head / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n\n    def baz(self):\n        return 2\n",
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir)

    fs = data["files"][0]["fold_symbols"]
    # Head: Foo (depth 0) then its two methods (depth 1), in source order.
    head_qns = [(s["qualified_name"], s["depth"]) for s in fs["head"]]
    assert head_qns == [("Foo", 0), ("Foo.bar", 1), ("Foo.baz", 1)]
    foo = fs["head"][0]
    assert foo["kind"] == "class" and foo["start_line"] == 1 and foo["end_line"] == 6
    # Base lacks baz.
    base_qns = [(s["qualified_name"], s["depth"]) for s in fs["base"]]
    assert base_qns == [("Foo", 0), ("Foo.bar", 1)]


def test_fold_symbols_empty_for_unsupported_language(run_dir: paths.RunDir) -> None:
    """An unsupported-language / unparsed file carries empty span lists,
    not a missing key — the inert degradation path."""
    raw = (
        "diff --git a/notes.txt b/notes.txt\n"
        "index 0123456..89abcde 100644\n"
        "--- a/notes.txt\n+++ b/notes.txt\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    run_dir.raw_diff.write_text(raw, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Edit notes",
                "author": {"login": "t"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )
    base = run_dir.base
    head = run_dir.head
    base.mkdir()
    head.mkdir()
    (base / "notes.txt").write_text("old\n", encoding="utf-8")
    (head / "notes.txt").write_text("new\n", encoding="utf-8")

    data = build_pending_viewer_json(run_dir)

    assert data["files"][0]["fold_symbols"] == {"head": [], "base": []}


# --- syntax-highlighting language map --------------------------------------

# Canonical languages registered in the vendored highlight.js build
# (semantic_code_review/viewer/assets/vendor/highlight.min.js). Derived by
# enumerating the build's `grmr_<name>` grammar registrations; re-run that
# enumeration after vendor/refresh.sh and update this set if it changes.
_HLJS_BUILD_LANGUAGES = frozenset(
    {
        "bash",
        "c",
        "cpp",
        "csharp",
        "css",
        "diff",
        "go",
        "graphql",
        "ini",
        "java",
        "javascript",
        "json",
        "kotlin",
        "less",
        "lua",
        "makefile",
        "markdown",
        "objectivec",
        "perl",
        "php",
        "plaintext",
        "python",
        "r",
        "ruby",
        "rust",
        "scss",
        "shell",
        "sql",
        "swift",
        "typescript",
        "vbnet",
        "wasm",
        "xml",
        "yaml",
    }
)


def test_lang_map_values_are_in_the_vendored_hljs_build() -> None:
    """Every mapped language must exist in the bundled highlight.js, else
    `hljs.highlight` throws at runtime and the cell silently falls back to
    plain text. Guards against typos / unbundled grammars."""
    from semantic_code_review.viewer.build_json import _LANG_BY_EXT

    unknown = {ext: lang for ext, lang in _LANG_BY_EXT.items() if lang not in _HLJS_BUILD_LANGUAGES}
    assert not unknown, f"languages not in the vendored hljs build: {unknown}"


def test_lang_from_path_covers_common_extensions() -> None:
    from semantic_code_review.viewer.build_json import _lang_from_path

    cases = {
        "a.py": "python",
        "a.ts": "typescript",
        "a.mts": "typescript",
        "a.jsx": "javascript",
        "a.cjs": "javascript",
        "styles.css": "css",
        "theme.scss": "scss",
        "App.swift": "swift",
        "index.php": "php",
        "schema.graphql": "graphql",
        "Config.TOML": "ini",
        "patch.diff": "diff",
    }
    for path, lang in cases.items():
        assert _lang_from_path(path) == lang, path
    # Unknown / extensionless ⇒ empty (viewer renders plain text).
    assert _lang_from_path("LICENSE") == ""
    assert _lang_from_path("data.parquet") == ""
