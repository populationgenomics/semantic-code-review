"""Viewer JSON + HTML: structural correctness on the fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_code_review import paths
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    FileAnnotations,
    FileRole,
    FoldDescription,
    PRInfo,
    lift_file,
)
from semantic_code_review.format.parse import parse_augmented_diff, parse_raw_diff
from semantic_code_review.viewer import build_json
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
    # Spans ride flat, outermost first; nesting is containment, read off the
    # ranges. Ids name the hunk and the range.
    assert [(s["id"], s["start"], s["end"]) for s in h["spans"]] == [
        ("H0_0:span:1-3", 1, 3),
        ("H0_0:span:5-7", 5, 7),
        ("H0_0:span:5-5", 5, 5),
    ]
    assert h["spans"][0]["smells"][0]["tag"] == "string-sql"
    assert "segments" not in h and "line_notes" not in h

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


# --- fold_regions: whole-file, both sides, on the FileBlock ------------------


def _meta(title: str) -> str:
    return json.dumps({"title": title, "author": {"login": "t"}, "url": "", "baseRefOid": "aaa", "headRefOid": "bbb"})


def test_fold_regions_cover_the_whole_file_on_the_file_block(run_dir: paths.RunDir) -> None:
    """Regions are the file's definitions from both parses, addressed by
    line range, on `FileBlock.fold_regions`; a hunk carries none. `Foo.bar`
    is outside the hunk's changed lines but inside the diff's context; it
    folds as unchanged. No per-side span lists ride along."""
    run_dir.raw_diff.write_text(_NESTED_DIFF, encoding="utf-8")
    run_dir.meta.write_text(_meta("Add Foo.baz"), encoding="utf-8")
    base = run_dir.base
    head = run_dir.head
    base.mkdir()
    head.mkdir()
    (base / "a.py").write_text("class Foo:\n    def bar(self):\n        return 1\n", encoding="utf-8")
    (head / "a.py").write_text(
        "class Foo:\n    def bar(self):\n        return 1\n\n    def baz(self):\n        return 2\n",
        encoding="utf-8",
    )

    data = build_pending_viewer_json(run_dir)

    f = data["files"][0]
    assert "fold_symbols" not in f
    assert "fold_regions" not in f["hunks"][0]
    regions = f["fold_regions"]
    assert [(r["qualified_name"], r["kind"], r["context"], r["right_start"], r["right_end"]) for r in regions] == [
        ("Foo", "class", "both", 1, 6),
        ("Foo.bar", "function", "right", 2, 3),
        ("Foo.baz", "function", "right", 5, 6),
    ]
    foo, bar, baz = f["fold_regions"]
    assert (foo["left_start"], foo["left_end"], foo["has_changes"]) == (1, 3, True)
    assert (bar["left_start"], bar["has_changes"]) == (None, False)
    assert baz["has_changes"] and baz["summary"] == ""


def test_fold_regions_exist_for_lines_outside_every_hunk(run_dir: paths.RunDir) -> None:
    """A hunk at the bottom of the file does not bound folding: the
    functions above it, which the diff never carried, are regions too, so
    they fold the moment a chip discloses them."""
    raw = (
        "diff --git a/a.py b/a.py\n"
        "index 0123456..89abcde 100644\n"
        "--- a/a.py\n+++ b/a.py\n"
        "@@ -9,2 +9,2 @@\n def c():\n-    return 0\n+    return 3\n"
    )
    run_dir.raw_diff.write_text(raw, encoding="utf-8")
    run_dir.meta.write_text(_meta("Fix c"), encoding="utf-8")
    run_dir.base.mkdir()
    run_dir.head.mkdir()
    head = "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n"
    (run_dir.head / "a.py").write_text(head, encoding="utf-8")
    (run_dir.base / "a.py").write_text(head.replace("return 3", "return 0"), encoding="utf-8")

    data = build_pending_viewer_json(run_dir)

    regions = data["files"][0]["fold_regions"]
    assert [(r["qualified_name"], r["right_start"], r["right_end"], r["has_changes"]) for r in regions] == [
        ("a", 1, 2, False),
        ("b", 5, 6, False),
        ("c", 9, 10, True),
    ]


def test_fold_regions_fall_back_to_indentation_without_a_grammar(run_dir: paths.RunDir) -> None:
    raw = (
        "diff --git a/conf.yaml b/conf.yaml\n"
        "index 0123456..89abcde 100644\n"
        "--- a/conf.yaml\n+++ b/conf.yaml\n"
        "@@ -3 +3 @@\n-    leaf: 1\n+    leaf: 2\n"
    )
    run_dir.raw_diff.write_text(raw, encoding="utf-8")
    run_dir.meta.write_text(_meta("Edit conf"), encoding="utf-8")
    run_dir.base.mkdir()
    run_dir.head.mkdir()
    (run_dir.base / "conf.yaml").write_text("top:\n  mid:\n    leaf: 1\n  other: 2\nflat: 3\n", encoding="utf-8")
    (run_dir.head / "conf.yaml").write_text("top:\n  mid:\n    leaf: 2\n  other: 2\nflat: 3\n", encoding="utf-8")

    data = build_pending_viewer_json(run_dir)

    regions = data["files"][0]["fold_regions"]
    assert [(r["context"], r["right_start"], r["right_end"], r["qualified_name"]) for r in regions] == [
        ("both", 1, 4, None),
        ("both", 2, 3, None),
    ]


def test_fold_summary_lands_on_a_region_a_positioned_run_opens(run_dir: paths.RunDir) -> None:
    """The differ draws the new function's run mid-body of `old_fn`;
    `position_runs` slides it to the `def`. The region's address is the
    AST's line range either way, so a summary persisted at it lands."""
    raw = (
        "diff --git a/a.py b/a.py\n"
        "index 0123456..89abcde 100644\n"
        "--- a/a.py\n+++ b/a.py\n"
        "@@ -1,6 +1,10 @@\n"
        " def old_fn():\n     setup()\n+    return cleanup()\n+\n+def new_fn():\n+    other()\n"
        "     return cleanup()\n \n def third():\n     pass\n"
    )
    run_dir.raw_diff.write_text(raw, encoding="utf-8")
    run_dir.meta.write_text(_meta("Add new_fn"), encoding="utf-8")
    run_dir.base.mkdir()
    run_dir.head.mkdir()
    old_fn = "def old_fn():\n    setup()\n    return cleanup()\n\n"
    new_fn = "def new_fn():\n    other()\n    return cleanup()\n\n"
    third = "def third():\n    pass\n"
    (run_dir.head / "a.py").write_text(old_fn + new_fn + third, encoding="utf-8")
    (run_dir.base / "a.py").write_text(old_fn + third, encoding="utf-8")
    diff = parse_raw_diff(run_dir.raw_diff.read_text(encoding="utf-8"))
    annotated = AnnotatedDiff(
        pr=PRInfo(pr_url="", base_sha="aaa", head_sha="bbb"),
        files=[
            lift_file(
                diff.files[0],
                ann=FileAnnotations(
                    fold_descriptions=[
                        FoldDescription(context="right", right_start=5, right_end=7, summary="adds new_fn"),
                    ]
                ),
            )
        ],
    )

    data = build_viewer_json(annotated, {}, head_dir=run_dir.head, base_dir=run_dir.base)

    f = data["files"][0]
    kinds = [r["kind"] for r in f["hunks"][0]["rows"]]
    assert kinds == ["ctx", "ctx", "ctx", "ctx", "ins", "ins", "ins", "ins", "ctx", "ctx"]  # the run moved
    by_name = {r["qualified_name"]: r for r in f["fold_regions"]}
    assert by_name["new_fn"]["summary"] == "adds new_fn"
    assert by_name["old_fn"]["has_changes"] is False


def test_binary_file_has_no_fold_regions(run_dir: paths.RunDir) -> None:
    run_dir.raw_diff.write_text(
        "diff --git a/img.bin b/img.bin\nindex 0123456..89abcde 100644\nBinary files a/img.bin and b/img.bin differ\n",
        encoding="utf-8",
    )
    run_dir.meta.write_text(_meta("Binary"), encoding="utf-8")
    run_dir.base.mkdir()
    run_dir.head.mkdir()
    (run_dir.head / "img.bin").write_bytes(b"\x00\x01\n  \x02\n")
    (run_dir.base / "img.bin").write_bytes(b"\x00\n")
    diff = parse_raw_diff(run_dir.raw_diff.read_text(encoding="utf-8"))
    annotated = AnnotatedDiff(
        pr=PRInfo(pr_url="", base_sha="aaa", head_sha="bbb"),
        files=[lift_file(diff.files[0], ann=FileAnnotations(role=FileRole.BINARY))],
    )

    data = build_viewer_json(annotated, {}, head_dir=run_dir.head, base_dir=run_dir.base)

    assert data["files"][0]["fold_regions"] == []


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


# --- lazy disclosure (ADR 0008 slice 1) --------------------------------------
# /data.json carries no file text: a region between hunks fetches it from
# /file-text when expanded. What it does carry is the post-image's line
# count, which bounds the region below the last hunk.


_DISCLOSURE_DIFF = """diff --git a/big.py b/big.py
index 0123456..89abcde 100644
--- a/big.py
+++ b/big.py
@@ -99,3 +99,3 @@
 line 99
-line 100
+LINE 100
 line 101
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 1111111..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
diff --git a/uv.lock b/uv.lock
index 3333333..4444444 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,1 +1,1 @@
-version = "0.1"
+version = "0.2"
"""


def _disclosure_run(run_dir: paths.RunDir, *, worktrees: bool) -> dict:
    run_dir.raw_diff.write_text(_DISCLOSURE_DIFF, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps({"title": "t", "author": {"login": "u"}, "url": "", "baseRefOid": "a", "headRefOid": "b"}),
        encoding="utf-8",
    )
    if worktrees:
        run_dir.base.mkdir()
        run_dir.head.mkdir()
        big = "".join(f"line {n}\n" for n in range(1, 6001))
        (run_dir.base / "big.py").write_text(big, encoding="utf-8")
        (run_dir.head / "big.py").write_text(big.replace("line 100\n", "LINE 100\n"), encoding="utf-8")
        (run_dir.base / "gone.py").write_text("one\ntwo\n", encoding="utf-8")
        (run_dir.base / "uv.lock").write_text('version = "0.1"\n', encoding="utf-8")
        (run_dir.head / "uv.lock").write_text('version = "0.2"', encoding="utf-8")  # no final newline
    return build_pending_viewer_json(run_dir)


def test_viewer_json_carries_no_file_text(run_dir: paths.RunDir) -> None:
    """The payload is the diff plus metadata; the rest of a file is the
    lazy route's. A 6,000-line file adds nothing beyond its hunk."""
    data = _disclosure_run(run_dir, worktrees=True)

    for f in data["files"]:
        assert "head_lines" not in f
    big = next(f for f in data["files"] if f["path"] == "big.py")
    assert "line 5000" not in json.dumps(big)
    assert len(json.dumps(big)) < 5_000


def test_head_line_count_is_the_post_image_length_for_every_role(run_dir: paths.RunDir) -> None:
    """Counted for every file with a post-image — over the old bundle cap,
    and skipped (generated) files alike — and null for a deleted file.
    A missing final newline does not add a line."""
    data = _disclosure_run(run_dir, worktrees=True)

    counts = {f["path"]: f["head_line_count"] for f in data["files"]}
    status = {f["path"]: f["status"] for f in data["files"]}
    assert counts == {"big.py": 6000, "gone.py": None, "uv.lock": 1}
    assert status["uv.lock"] == "generated"


def test_head_line_count_is_null_without_a_worktree(run_dir: paths.RunDir) -> None:
    data = _disclosure_run(run_dir, worktrees=False)

    assert {f["head_line_count"] for f in data["files"]} == {None}


@pytest.mark.parametrize(
    ("text", "count"),
    [
        (None, None),
        ("", 0),
        ("\n", 1),
        ("a", 1),
        ("a\n", 1),
        ("a\nb", 2),
        ("a\nb\n", 2),
        ("a\n\nb\n", 3),
    ],
)
def test_split_lines_numbers_lines_as_the_diff_does(text: str | None, count: int | None) -> None:
    lines = build_json._split_lines(text)
    assert (None if lines is None else len(lines)) == count
