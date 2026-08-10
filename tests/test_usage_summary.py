"""Per-run token accounting derived from trace files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from semantic_code_review.augment import usage


def _trace(
    *,
    usages: list[dict[str, int]],
    turns: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    iterations = []
    for u in usages:
        response: dict[str, Any] = {"usage": u, "content": []}
        if turns is not None:
            response["provider_details"] = {"num_turns": turns}
        iterations.append({"messages_sent": [], "response": response})
    trace: dict[str, Any] = {"model": "m", "system": "s", "iterations": iterations}
    if error is not None:
        trace["error"] = error
    return trace


def _write(trace_dir: Path, name: str, trace: dict[str, Any]) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / name).write_text(json.dumps(trace), encoding="utf-8")


def _usage(inp: int = 0, out: int = 0, read: int = 0, write: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }


def test_totals_sum_every_pass(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    _write(trace_dir, "overview.json", _trace(usages=[_usage(inp=100, out=50)]))
    _write(trace_dir, "hunk-a.py-1.json", _trace(usages=[_usage(inp=10, read=200, write=30)]))
    _write(trace_dir, "hunk-b.py-2.json", _trace(usages=[_usage(inp=20, read=400, write=30)]))

    summary = usage.summarize_trace_dir(trace_dir)

    assert summary["totals"] == {
        "calls": 3,
        "cache_hits": 0,
        "failed": 0,
        "input_tokens": 130,
        "output_tokens": 50,
        "cache_read_tokens": 600,
        "cache_write_tokens": 60,
        "total_tokens": 840,
    }
    assert summary["passes"]["hunk"]["calls"] == 2
    assert summary["passes"]["overview"]["calls"] == 1


def test_multi_iteration_call_counts_once_but_sums_tokens(tmp_path: Path) -> None:
    """An SDK tool round-trip is several iterations of one call."""
    trace_dir = tmp_path / "trace"
    _write(trace_dir, "hunk-a.py-1.json", _trace(usages=[_usage(inp=100), _usage(inp=150, out=20)]))

    summary = usage.summarize_trace_dir(trace_dir)

    assert summary["passes"]["hunk"]["calls"] == 1
    assert summary["passes"]["hunk"]["input_tokens"] == 250
    assert summary["passes"]["hunk"]["per_call"]["median"] == 270


def test_cache_hits_counted_separately_from_calls(tmp_path: Path) -> None:
    """A cached pass spent no tokens; counting it as a call would hide that."""
    trace_dir = tmp_path / "trace"
    _write(trace_dir, "hunk-a.py-1.json", {"cache_hit": True, "pass": "hunk", "response": {}})
    _write(trace_dir, "hunk-b.py-2.json", _trace(usages=[_usage(inp=10)]))

    summary = usage.summarize_trace_dir(trace_dir)

    assert summary["passes"]["hunk"] == {
        **summary["passes"]["hunk"],
        "calls": 1,
        "cache_hits": 1,
        "input_tokens": 10,
    }


def test_turns_reported_only_when_the_backend_supplies_them(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    _write(trace_dir, "hunk-a.py-1.json", _trace(usages=[_usage(inp=1)], turns=3))
    _write(trace_dir, "hunk-b.py-2.json", _trace(usages=[_usage(inp=1)], turns=9))
    _write(trace_dir, "overview.json", _trace(usages=[_usage(inp=1)]))

    summary = usage.summarize_trace_dir(trace_dir)

    assert summary["passes"]["hunk"]["turns"] == {"n": 2, "median": 9, "p90": 9, "max": 9}
    assert "turns" not in summary["passes"]["overview"]


def test_failed_calls_are_counted(tmp_path: Path) -> None:
    """#33's failure mode: tokens spent on hunks that produced nothing."""
    trace_dir = tmp_path / "trace"
    _write(trace_dir, "hunk-a.py-1.json", _trace(usages=[_usage(inp=500)], error="UsageLimitExceeded"))

    summary = usage.summarize_trace_dir(trace_dir)

    assert summary["passes"]["hunk"]["failed"] == 1
    assert summary["passes"]["hunk"]["input_tokens"] == 500


def test_malformed_trace_skipped_without_losing_the_rest(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    _write(trace_dir, "hunk-a.py-1.json", _trace(usages=[_usage(inp=7)]))
    (trace_dir / "hunk-broken.json").write_text("{not json", encoding="utf-8")

    summary = usage.summarize_trace_dir(trace_dir)

    assert summary["totals"]["input_tokens"] == 7


def test_write_usage_summary_returns_none_without_a_trace_dir(tmp_path: Path) -> None:
    assert usage.write_usage_summary(tmp_path) is None
    assert not (tmp_path / usage.USAGE_FILENAME).exists()


def test_write_usage_summary_persists_and_returns_the_summary(tmp_path: Path) -> None:
    _write(tmp_path / "trace", "hunk-a.py-1.json", _trace(usages=[_usage(inp=3, out=4)]))

    summary = usage.write_usage_summary(tmp_path)

    assert summary is not None
    written = json.loads((tmp_path / usage.USAGE_FILENAME).read_text(encoding="utf-8"))
    assert written == summary
    assert written["totals"]["total_tokens"] == 7


def test_summary_line_splits_cache_reads_from_writes() -> None:
    """The split is the point: reads and writes bill differently."""
    summary = {
        "totals": {
            "total_tokens": 1000,
            "input_tokens": 10,
            "output_tokens": 90,
            "cache_read_tokens": 600,
            "cache_write_tokens": 300,
        },
        "passes": {"hunk": {"per_call": {"median": 500, "p90": 900}, "turns": {"median": 3, "max": 8}}},
    }

    line = usage.format_summary_line(summary)

    assert "cache_r=600" in line
    assert "cache_w=300" in line
    assert "per-hunk median=500" in line
    assert "turns median=3 max=8" in line
