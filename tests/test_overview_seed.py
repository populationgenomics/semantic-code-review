"""Slice 3 — the deterministic structural symbol seed in the overview prompt.

Covers `format_overview_prompt`'s optional `delta` argument: the seed
section is rendered from a `SymbolDelta` and is byte-identical to the
pre-seed form when there's nothing to seed (no delta, or an empty one —
the all-unsupported-language case).
"""

from __future__ import annotations

from semantic_code_review.augment.overview import format_overview_prompt
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    ParsedHunk,
    PRInfo,
)
from semantic_code_review.structural import ChangedSymbol, SymbolDelta, SymbolRange


def _hunk(header: str) -> AnnotatedHunk:
    return AnnotatedHunk(parsed=ParsedHunk(
        header=header, body="+a\n-b\n",
        old_start=1, old_count=1, new_start=1, new_count=1,
    ))


def _make_diff() -> AnnotatedDiff:
    return AnnotatedDiff(
        pr=PRInfo(pr_url="", base_sha="a", head_sha="b"),
        files=[
            AnnotatedFile(
                path="a.py",
                diff_git_line="diff --git a/a.py b/a.py",
                hunks=[_hunk("@@ -1,1 +1,1 @@")],
            ),
        ],
    )


_META = {"title": "T", "body": ""}


def _changed(name: str, qn: str, kind: str = "function") -> ChangedSymbol:
    return ChangedSymbol(
        path="a.py", kind=kind, name=name, qualified_name=qn,
        range=SymbolRange(start_line=1, end_line=2, start_col=0, end_col=0),
    )


def test_no_delta_is_byte_identical_to_unseeded() -> None:
    diff = _make_diff()
    assert format_overview_prompt(diff, _META, None) == format_overview_prompt(diff, _META)


def test_empty_delta_is_byte_identical_to_unseeded() -> None:
    """All-unsupported-language diff ⇒ empty delta ⇒ no seed section."""
    diff = _make_diff()
    baseline = format_overview_prompt(diff, _META)
    assert format_overview_prompt(diff, _META, SymbolDelta()) == baseline


def test_non_empty_delta_appends_seed_section() -> None:
    diff = _make_diff()
    delta = SymbolDelta(
        added=[_changed("bar", "bar")],
        modified=[_changed("baz", "Foo.baz", kind="method")],
        removed=[_changed("gone", "gone")],
    )
    out = format_overview_prompt(diff, _META, delta)
    assert "# Symbols changed (deterministic" in out
    # Each entry rendered as `kind qualified_name  (path)`.
    assert "  function bar  (a.py)" in out
    assert "  method Foo.baz  (a.py)" in out
    assert "  function gone  (a.py)" in out
    # The seed extends — never replaces — the existing prompt body.
    assert "# Hunk headers" in out


def test_seed_section_is_strict_suffix_of_unseeded() -> None:
    """The seed only appends; the pre-seed prefix is unchanged."""
    diff = _make_diff()
    baseline = format_overview_prompt(diff, _META)
    delta = SymbolDelta(added=[_changed("bar", "bar")])
    out = format_overview_prompt(diff, _META, delta)
    # baseline ends in a trailing newline; the seed is inserted before it.
    assert out.startswith(baseline.rstrip("\n"))


def test_empty_bucket_renders_none() -> None:
    diff = _make_diff()
    delta = SymbolDelta(added=[_changed("bar", "bar")])
    out = format_overview_prompt(diff, _META, delta)
    assert "removed:\n  (none)" in out
    assert "modified:\n  (none)" in out
