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


# --- removed-symbol seed ---------------------------------------------------


def _removed(name: str = "mod._mcp_config_for", start: int = 159, end: int = 196):
    from semantic_code_review.structural import ChangedSymbol, SymbolRange

    return ChangedSymbol(
        path="pkg/mod.py",
        kind="function",
        name=name.rsplit(".", maxsplit=1)[-1],
        qualified_name=name,
        range=SymbolRange(start_line=start, end_line=end, start_col=0, end_col=0),
        signature=f"def {name.rsplit('.', maxsplit=1)[-1]}(self)",
    )


def _delta(removed=(), added=()):
    from semantic_code_review.structural import SymbolDelta

    return SymbolDelta(removed=list(removed), added=list(added))


def test_removed_symbols_names_what_head_cannot_show() -> None:
    """Every tool searches head, so a deleted symbol returns empty from all
    of them — indistinguishable from a bad query unless we say so."""
    from semantic_code_review.augment.hunks import format_removed_symbols

    text = format_removed_symbols(_delta(removed=[_removed()]), path="pkg/mod.py", base_sha="abc123")
    assert "NOT in the head worktree" in text
    assert "read_file_at" in text
    assert "abc123" in text  # the SHA to read at, not just the instruction
    assert "base 159-196" in text
    assert "_mcp_config_for" in text


def test_removals_from_other_files_are_named_too() -> None:
    """The hunk that motivated the seed deleted *references* to symbols
    defined elsewhere; a per-file list gives it nothing at all."""
    from semantic_code_review.augment.hunks import format_removed_symbols

    text = format_removed_symbols(_delta(removed=[_removed()]), path="pkg/other.py", base_sha="abc123")
    assert "Removed elsewhere in this change" in text
    assert "_mcp_config_for" in text
    assert "pkg/mod.py" in text


def test_a_moved_symbol_is_not_reported_as_removed() -> None:
    """A per-path set-diff calls a move a removal. Saying it is gone —
    next to a prompt telling the model not to re-search — earns a
    confidently wrong annotation on an ordinary refactor."""
    from semantic_code_review.augment.hunks import format_removed_symbols

    gone = _removed()
    arrived = gone.model_copy(update={"path": "pkg/new_home.py"})
    text = format_removed_symbols(_delta(removed=[gone], added=[arrived]), path="pkg/mod.py", base_sha="s")

    assert "Moved, not removed" in text
    assert "pkg/mod.py -> pkg/new_home.py" in text
    assert "NOT in the head worktree" not in text


def test_the_seed_is_capped() -> None:
    """It rides in every hunk prompt for the file; a wholesale deletion
    uncapped measured ~62k tokens per hunk."""
    from semantic_code_review.augment.hunks import _REMOVED_SEED_MAX, format_removed_symbols

    many = [_removed(name=f"mod.gone_{i}") for i in range(_REMOVED_SEED_MAX * 3)]
    text = format_removed_symbols(_delta(removed=many), path="pkg/mod.py", base_sha="s")

    assert "list truncated" in text
    assert sum(1 for line in text.splitlines() if line.startswith("def gone_")) == _REMOVED_SEED_MAX


def test_no_removed_symbols_emits_no_section() -> None:
    from semantic_code_review.augment.hunks import format_removed_symbols

    assert format_removed_symbols(_delta(), path="pkg/mod.py", base_sha="s") == ""
    assert format_removed_symbols(None, path="pkg/mod.py", base_sha="s") == ""


def test_removed_section_rides_with_the_file_context_in_both_forms() -> None:
    from semantic_code_review.augment.hunks import (
        format_batch_prompt,
        format_hunk_prompt,
        format_removed_symbols,
    )

    fp, outline = _file()
    removed = format_removed_symbols(_delta(removed=[_removed()]), path="pkg/mod.py", base_sha="s")
    _idx, hunk = _hunk(0)

    single = "".join(b for b in format_hunk_prompt(fp, hunk, "{}", "s", outline, removed) if isinstance(b, str))
    batch = format_batch_prompt(fp, [_hunk(0)], "s", outline, removed)[0]
    for prompt in (single, batch):
        assert "# Removed from this file" in prompt


def test_both_system_prompts_explain_head_versus_base_search() -> None:
    """The rephrase loop came from not knowing empty was the real answer."""
    from semantic_code_review.augment.prompts import HUNK_BATCH_SYSTEM, HUNK_SYSTEM

    for prompt in (HUNK_SYSTEM, HUNK_BATCH_SYSTEM):
        assert "HEAD worktree" in prompt
        assert "Do not rephrase" in prompt
        # The alternatives have to be named at the point of decision —
        # the model reaches for grep 394 times to changed_symbols' once.
        assert "grep_at" in prompt
        assert "changed_symbols" in prompt


def test_hunk_caching_stays_within_anthropics_breakpoint_budget() -> None:
    """Anthropic allows 4 per request. Over budget pydantic-ai keeps every
    system/tool breakpoint and trims message ones oldest-first, which
    evicts the overview marker — the only prefix shared across files, and
    byte-identical for the whole run."""
    from pydantic_ai import CachePoint

    from semantic_code_review.augment.hunks import _HUNK_CACHE_SETTINGS, format_hunk_prompt

    fp, outline = _file()
    _idx, hunk = _hunk(0)
    blocks = format_hunk_prompt(fp, hunk, "{}", "s", outline, "")

    in_prompt = sum(1 for b in blocks if isinstance(b, CachePoint))
    from_settings = sum(
        1
        for key in ("anthropic_cache_instructions", "anthropic_cache_tool_definitions", "anthropic_cache_messages")
        if _HUNK_CACHE_SETTINGS.get(key)
    )
    assert in_prompt + from_settings <= 4


def test_the_overview_block_carries_the_base_sha() -> None:
    """`read_file_at` and `grep_at` need it and had no way to learn it.

    Measured over one sweep: `read_file_at` errored on 31% of calls and
    `grep_at` returned empty on 95%, because the model guessed `HEAD~1`
    — which cannot resolve in a depth-1 fetch. Every call that used the
    real base SHA succeeded.
    """
    import json

    from semantic_code_review.augment.hunks import overview_to_prompt_json
    from semantic_code_review.augment.schemas import AnnotatedDiff, Overview, PRInfo

    diff = AnnotatedDiff(
        version="1",
        pr=PRInfo(pr_url="", base_sha="deadbeefcafe", head_sha="head", model="m"),
        files=[],
        overview=Overview(summary="s"),
    )

    payload = json.loads(overview_to_prompt_json(diff))

    assert payload["base_sha"] == "deadbeefcafe"


def test_both_prompts_say_relative_revisions_will_not_resolve() -> None:
    """Naming the field is not enough — the model reached for `HEAD~1`
    unprompted, so the shallow-fetch constraint has to be explicit."""
    from semantic_code_review.augment.prompts import HUNK_BATCH_SYSTEM, HUNK_SYSTEM

    for prompt in (HUNK_SYSTEM, HUNK_BATCH_SYSTEM):
        assert "base_sha" in prompt
        assert "HEAD~1" in prompt
