"""Splitting a batched per-hunk payload back into per-hunk annotations.

The failure mode these guard is silent loss: a batch that answers for
five of six hunks must surface the sixth, not quietly drop it.
"""

from __future__ import annotations

import pytest

from semantic_code_review.augment.hunks import split_batch_annotations
from semantic_code_review.augment.schemas import BatchAnnotations


def _entry(idx: int, intent: str = "x") -> dict:
    return {"hunk_index": idx, "intent": intent}


def test_splits_by_hunk_index() -> None:
    by_index, missing = split_batch_annotations(
        {"annotations": [_entry(0, "first"), _entry(2, "third")]},
        [0, 1, 2],
    )
    assert by_index[0]["intent"] == "first"
    assert by_index[2]["intent"] == "third"
    assert missing == [1]


def test_full_coverage_reports_nothing_missing() -> None:
    by_index, missing = split_batch_annotations(
        {"annotations": [_entry(0), _entry(1)]},
        [0, 1],
    )
    assert set(by_index) == {0, 1}
    assert missing == []


def test_empty_payload_reports_every_hunk_missing() -> None:
    """A batch that returns nothing must retry every hunk, not silently pass."""
    by_index, missing = split_batch_annotations({"annotations": []}, [0, 1, 2])
    assert by_index == {}
    assert missing == [0, 1, 2]


def test_entry_for_a_hunk_not_in_the_batch_is_dropped() -> None:
    """The model occasionally answers for a neighbouring hunk it can't see."""
    by_index, missing = split_batch_annotations(
        {"annotations": [_entry(0), _entry(7)]},
        [0, 1],
    )
    assert set(by_index) == {0}
    assert missing == [1]


def test_duplicate_index_keeps_the_first() -> None:
    by_index, missing = split_batch_annotations(
        {"annotations": [_entry(0, "first"), _entry(0, "second")]},
        [0],
    )
    assert by_index[0]["intent"] == "first"
    assert missing == []


@pytest.mark.parametrize("bad", [{"intent": "no index"}, {"hunk_index": None, "intent": "x"}, {"hunk_index": "a"}])
def test_malformed_index_is_dropped_and_reported_missing(bad: dict) -> None:
    by_index, missing = split_batch_annotations({"annotations": [bad, _entry(1)]}, [0, 1])
    assert set(by_index) == {1}
    assert missing == [0]


def test_absent_annotations_key_is_total_loss_not_silent_success() -> None:
    by_index, missing = split_batch_annotations({}, [0, 1])
    assert by_index == {}
    assert missing == [0, 1]


def test_batch_schema_requires_an_index_per_entry() -> None:
    """`hunk_index` is what makes an entry addressable — it can't default."""
    with pytest.raises(ValueError):
        BatchAnnotations.model_validate({"annotations": [{"intent": "x"}]})


def test_batch_schema_round_trips_a_full_entry() -> None:
    batch = BatchAnnotations.model_validate(
        {"annotations": [{"hunk_index": 3, "intent": "bumps the tag", "confidence": 90}]}
    )
    assert batch.annotations[0].hunk_index == 3
    assert batch.annotations[0].intent == "bumps the tag"


# --- prompt assembly -------------------------------------------------------


def _file(summary: str = "does things", outline: str = "def f()  (1-2)"):
    from semantic_code_review.augment.schemas import AnnotatedFile, FileAnnotations

    return AnnotatedFile(
        path="pkg/mod.py",
        diff_git_line="diff --git a/pkg/mod.py b/pkg/mod.py",
        ann=FileAnnotations(summary=summary),
        hunks=[],
    ), outline


def _hunk(index: int, body: str = "-x\n+y"):
    from semantic_code_review.augment.schemas import AnnotatedHunk, HunkAnnotations, ParsedHunk

    return index, AnnotatedHunk(
        parsed=ParsedHunk(
            header=f"@@ -{index + 1},1 +{index + 1},1 @@",
            body=body,
            old_start=index + 1,
            old_count=1,
            new_start=index + 1,
            new_count=1,
        ),
        ann=HunkAnnotations(intent=""),
    )


def test_only_run_invariant_text_is_in_the_system_prompt() -> None:
    """The system prompt caches because it is identical on every call.

    Per-file content is constant within a batch but not across the run —
    putting it here turns one shared entry into one entry per file that
    nothing reads back (measured: 243k -> 517k billed on commit 7a232f9).
    """
    from semantic_code_review.augment.hunks import format_batch_prompt, format_batch_system

    fp, outline = _file()
    system = format_batch_system('{"summary":"s"}')
    user = format_batch_prompt(fp, [_hunk(0), _hunk(1)], "does things", outline)[0]

    # Section headers, not bare substrings: the system prompt legitimately
    # *mentions* `# File outline` when telling the model where to look.
    assert "\n# PR overview\n" in system
    assert user.startswith("# File\npath:")
    for header in ("# File\npath:", "\n# File summary\n", "\n# File outline"):
        assert header not in system
        assert header in user


def test_batch_system_is_identical_across_files() -> None:
    """Byte-identity across calls is the whole reason it caches."""
    from semantic_code_review.augment.hunks import format_batch_system

    assert format_batch_system('{"summary":"s"}') == format_batch_system('{"summary":"s"}')


def test_batch_prompt_labels_each_hunk_with_its_file_index() -> None:
    from semantic_code_review.augment.hunks import format_batch_prompt

    fp, outline = _file()
    user = format_batch_prompt(fp, [_hunk(0), _hunk(3), _hunk(7)], "s", outline)[0]
    assert "# Hunk 0" in user
    assert "# Hunk 3" in user
    assert "# Hunk 7" in user
    assert user.count("# Hunk ") == 3


def test_batch_prompt_omits_absent_sections() -> None:
    """An unsupported language has no outline; a bare heading reads as
    'there is nothing here' rather than 'this was not computed'."""
    from semantic_code_review.augment.hunks import format_batch_prompt

    fp, _ = _file(summary="")
    user = format_batch_prompt(fp, [_hunk(0)], "", "")[0]
    assert "# File outline" not in user
    assert "# File summary" not in user
    assert "# File\npath:" in user


def test_batch_system_contains_the_one_entry_per_hunk_contract() -> None:
    from semantic_code_review.augment.hunks import format_batch_system

    system = format_batch_system("{}")
    assert "EXACTLY ONE entry per block" in system
    assert "hunk_index" in system
