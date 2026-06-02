"""Unit tests for `apply_fold_summary_to_run` — sidecar I/O + schema mutation.

These exercise the function directly, without HTTP. The LLM call
(`summarise_fold`) is stubbed at the module level so we test
resolution + mutation + persistence in isolation; the LLM side has
its own coverage via the agent tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.fold_summary import (
    FoldSummaryFileIndexError,
    FoldSummaryNotReady,
    apply_fold_summary_to_run,
)
from semantic_code_review.format.parse import parse_augmented_diff
from semantic_code_review.format.sidecar import dump_sidecar, load_sidecar


FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


def _populate_run_dir(tmp_path: Path) -> Path:
    """Lay out a minimal run dir with augmented.scr.json + head/<path>.

    Returns the sidecar path so callers can reload it after the call.
    """
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    sidecar = tmp_path / "augmented.scr.json"
    dump_sidecar(diff, sidecar)
    (tmp_path / "augmented.diff").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8",
    )
    head_file = tmp_path / "head" / diff.files[0].path
    head_file.parent.mkdir(parents=True, exist_ok=True)
    head_file.write_text("noop\n", encoding="utf-8")
    return sidecar


@pytest.fixture
def stub_summarise(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace `summarise_fold` with a deterministic stub.

    The returned dict captures the kwargs the stub last saw, so a test
    can assert on what got forwarded (context, ranges, file_path
    resolution).
    """
    seen: dict = {}

    async def _stub(  # noqa: PLR0913 — matches summarise_fold's keyword surface
        client, *,
        run_dir, file_path, file_summary, overview_json,
        context, right_range, left_range,
        model, cache=None, trace_dir=None,
    ) -> str:
        seen.update(
            run_dir=run_dir, file_path=file_path, file_summary=file_summary,
            context=context, right_range=right_range, left_range=left_range,
            model=model,
        )
        return f"stub-summary-{context}"

    import semantic_code_review.augment.fold_summary as mod
    monkeypatch.setattr(mod, "summarise_fold", _stub)
    return seen


def _client() -> Client:
    # The stub never dereferences `client.model`; any placeholder works.
    return Client(model="stub")


# ---------------------------------------------------------------------------
# Happy path — right / left / both contexts
# ---------------------------------------------------------------------------

async def test_right_context_persists_and_returns_payload(
    tmp_path: Path, stub_summarise: dict,
) -> None:
    sidecar = _populate_run_dir(tmp_path)
    result = await apply_fold_summary_to_run(
        _client(),
        run_dir=tmp_path, file_idx=0, context="right",
        right_range=(1, 3), left_range=None, model="x",
    )
    assert result == {
        "file_idx": 0, "context": "right",
        "right_start": 1, "right_end": 3,
        "left_start": 0, "left_end": 0,
        "summary": "stub-summary-right",
    }
    # Stub saw the file_path the function resolved from the sidecar.
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    assert stub_summarise["file_path"] == diff.files[0].path
    assert stub_summarise["right_range"] == (1, 3)
    assert stub_summarise["left_range"] is None

    # Sidecar now carries the FoldDescription.
    reloaded = load_sidecar(sidecar)
    folds = reloaded.files[0].hunks[0].ann.fold_descriptions
    assert any(
        fd.context == "right" and fd.right_start == 1 and fd.right_end == 3
        and fd.summary == "stub-summary-right"
        for fd in folds
    )


async def test_left_context_persists(
    tmp_path: Path, stub_summarise: dict,
) -> None:
    sidecar = _populate_run_dir(tmp_path)
    result = await apply_fold_summary_to_run(
        _client(),
        run_dir=tmp_path, file_idx=0, context="left",
        right_range=None, left_range=(12, 14), model="x",
    )
    assert result["context"] == "left"
    assert result["left_start"] == 12 and result["left_end"] == 14
    reloaded = load_sidecar(sidecar)
    folds = reloaded.files[0].hunks[0].ann.fold_descriptions
    assert any(
        fd.context == "left" and fd.left_start == 12 and fd.left_end == 14
        for fd in folds
    )


async def test_replaces_existing_fold_description_with_same_key(
    tmp_path: Path, stub_summarise: dict,
) -> None:
    """Two calls with the same (context, ranges) end up with one entry,
    not two — so a re-summarise overwrites rather than accumulating."""
    sidecar = _populate_run_dir(tmp_path)
    await apply_fold_summary_to_run(
        _client(), run_dir=tmp_path, file_idx=0, context="right",
        right_range=(1, 3), left_range=None, model="x",
    )
    # Second call with the same key — stub returns the same string.
    await apply_fold_summary_to_run(
        _client(), run_dir=tmp_path, file_idx=0, context="right",
        right_range=(1, 3), left_range=None, model="x",
    )
    reloaded = load_sidecar(sidecar)
    folds = reloaded.files[0].hunks[0].ann.fold_descriptions
    matching = [
        fd for fd in folds
        if fd.context == "right" and fd.right_start == 1 and fd.right_end == 3
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

async def test_raises_not_ready_when_sidecar_missing(
    tmp_path: Path, stub_summarise: dict,
) -> None:
    # No sidecar laid down.
    with pytest.raises(FoldSummaryNotReady):
        await apply_fold_summary_to_run(
            _client(),
            run_dir=tmp_path, file_idx=0, context="right",
            right_range=(1, 3), left_range=None, model="x",
        )


async def test_raises_file_index_error_when_out_of_range(
    tmp_path: Path, stub_summarise: dict,
) -> None:
    _populate_run_dir(tmp_path)
    with pytest.raises(FoldSummaryFileIndexError):
        await apply_fold_summary_to_run(
            _client(),
            run_dir=tmp_path, file_idx=999, context="right",
            right_range=(1, 3), left_range=None, model="x",
        )
