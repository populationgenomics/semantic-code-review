"""Per-run token accounting, derived from the trace files.

Reads `<run_dir>/trace/*.json` after a run and writes `usage.json`
beside them. Post-processing rather than accumulation threaded through
`run_pass` — the traces already hold every response's usage, so the
summary is a pure function of what's on disk and can be recomputed for
an old run.

Pass identity comes from the trace filename, which each pass owns:
`overview.json`, `hunk-*.json`, `fold-*.json`, `extra-review.json`.

The reported unit is the *call*, not the token: per-call cost is
dominated by a fixed per-spawn floor (system prompt, tool definitions,
the CLI's own scaffolding) that is re-paid on every spawn and mostly
re-read on every internal turn. `per_call` and `turns` are therefore
the two distributions worth watching when tuning.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

USAGE_FILENAME = "usage.json"


def _pass_name(relative_path: str) -> str:
    """Map a trace path, relative to the trace dir, to the pass that wrote it.

    Keys on the first path component so a trace written before the filename
    was sanitised — which landed in a `hunk-…/` subdirectory — still
    attributes to its pass instead of falling through to `other`.
    """
    head = relative_path.split("/", 1)[0].removesuffix(".json")
    for prefix in ("hunk", "fold", "extra-review", "overview", "explainer"):
        if head == prefix or head.startswith(f"{prefix}-"):
            return prefix
    return "other"


@dataclasses.dataclass
class _PassAccumulator:
    """Running totals for one pass across a run's trace files."""

    calls: int = 0
    cache_hits: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    per_call_totals: list[int] = dataclasses.field(default_factory=list)
    turns: list[int] = dataclasses.field(default_factory=list)

    def add_trace(self, trace: dict[str, Any]) -> None:
        if trace.get("cache_hit"):
            self.cache_hits += 1
            return
        self.calls += 1
        if trace.get("error"):
            self.failed += 1
        call_total = 0
        for iteration in trace.get("iterations") or []:
            response = iteration.get("response") or {}
            usage = response.get("usage") or {}
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
            self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            self.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
            # Same convention as `total_tokens`: `input_tokens` already
            # includes both cached portions, so adding them again would
            # report a per-call figure roughly double the run total on
            # the very line that prints both.
            call_total += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            # Only CLI backends report turns; SDK per-turn detail is the
            # iteration list itself.
            turns = (response.get("provider_details") or {}).get("num_turns")
            if isinstance(turns, int):
                self.turns.append(turns)
        self.per_call_totals.append(call_total)

    @property
    def total_tokens(self) -> int:
        """Tokens the request actually carried.

        `input_tokens` already includes both cached portions, so adding
        them again counts every cached token twice — which reported SDK
        backends at roughly 2x their real usage while leaving the CLI
        (whose driver passed Anthropic's cache-exclusive figure straight
        through) correct, and made the two look incomparable.
        """
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        """Input billed at the full rate — cache reads and writes are cheaper."""
        return max(0, self.input_tokens - self.cache_read_tokens - self.cache_write_tokens)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failed": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "per_call": _distribution(self.per_call_totals),
        }
        # Absent for SDK backends: "not reported" is distinct from "one turn".
        if self.turns:
            out["turns"] = _distribution(self.turns)
        return out


def _distribution(values: Iterable[int]) -> dict[str, int]:
    ordered = sorted(values)
    if not ordered:
        return {}
    return {
        "n": len(ordered),
        "median": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "max": ordered[-1],
    }


def _percentile(ordered: list[int], fraction: float) -> int:
    """Nearest-rank percentile of an already-sorted list."""
    idx = min(len(ordered) - 1, int(fraction * len(ordered)))
    return ordered[idx]


def summarize_trace_dir(trace_dir: Path) -> dict[str, Any]:
    """Aggregate every trace under `trace_dir` into a usage summary.

    Args:
        trace_dir: The run's `trace/` directory.

    Returns:
        A JSON-shaped summary with `totals` across all passes and a
        `passes` breakdown. Unreadable or non-trace JSON files are
        skipped with a warning — a malformed trace must not cost the
        caller its accounting for the rest of the run.
    """
    passes: dict[str, _PassAccumulator] = {}
    # Recursive: a trace whose name once carried a path separator landed in a
    # subdirectory, and a flat scan silently dropped its tokens from the
    # totals. The name is sanitised now, but accounting must not depend on
    # that — an unreadable layout should never read as "spent nothing".
    for path in sorted(trace_dir.rglob("*.json")):
        if path.name == USAGE_FILENAME:
            continue
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("usage: skipping unreadable trace %s", path.name, exc_info=True)
            continue
        if not isinstance(trace, dict):
            log.warning("usage: skipping trace %s — not a JSON object", path.name)
            continue
        passes.setdefault(_pass_name(path.relative_to(trace_dir).as_posix()), _PassAccumulator()).add_trace(trace)

    totals = _PassAccumulator()
    for acc in passes.values():
        totals.calls += acc.calls
        totals.cache_hits += acc.cache_hits
        totals.failed += acc.failed
        totals.input_tokens += acc.input_tokens
        totals.output_tokens += acc.output_tokens
        totals.cache_read_tokens += acc.cache_read_tokens
        totals.cache_write_tokens += acc.cache_write_tokens

    return {
        "totals": {
            "calls": totals.calls,
            "cache_hits": totals.cache_hits,
            "failed": totals.failed,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_tokens": totals.cache_read_tokens,
            "cache_write_tokens": totals.cache_write_tokens,
            "total_tokens": totals.total_tokens,
        },
        "passes": {name: acc.summary() for name, acc in sorted(passes.items())},
    }


def write_usage_summary(run_dir: Path) -> dict[str, Any] | None:
    """Write `<run_dir>/usage.json` from the run's traces.

    Returns the summary it wrote (so the caller can render it without
    re-reading), or `None` when the run has no trace directory — which
    is the case for tests that stub the pipeline.
    """
    trace_dir = run_dir / "trace"
    if not trace_dir.is_dir():
        return None
    summary = summarize_trace_dir(trace_dir)
    (run_dir / USAGE_FILENAME).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def format_summary_line(summary: dict[str, Any]) -> str:
    """One-line stderr rendering of a usage summary.

    Cache reads and writes are split out because they bill differently
    (reads at a fraction of fresh input, writes at a premium), so the
    single total hides which lever moved.
    """
    totals = summary["totals"]
    hunks = summary.get("passes", {}).get("hunk", {})
    per_call = hunks.get("per_call") or {}
    parts = [
        f"tokens={totals['total_tokens']:,}",
        f"(in={totals['input_tokens']:,}",
        f"out={totals['output_tokens']:,}",
        f"cache_r={totals['cache_read_tokens']:,}",
        f"cache_w={totals['cache_write_tokens']:,})",
    ]
    if per_call:
        parts.append(f"per-hunk median={per_call['median']:,} p90={per_call['p90']:,}")
    turns = hunks.get("turns")
    if turns:
        parts.append(f"turns median={turns['median']} max={turns['max']}")
    return " ".join(parts)


__all__ = [
    "USAGE_FILENAME",
    "format_summary_line",
    "summarize_trace_dir",
    "write_usage_summary",
]
