"""Overview pass: one call per PR producing the PR-level summary.

Input: PR metadata + diffstat + per-file hunk headers (bodies omitted
to save tokens). Output: the `Overview` object plus per-file summary
text and optional `lang` override that populate `FilePatch` fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..augment.schemas import (
    AugmentedDiff, FilePatch, FileSymbols, Overview, OverviewEdge, OverviewSymbol,
)
from ..cache.store import CacheStore
from .prompts import OVERVIEW_SYSTEM, PROMPT_VERSION, overview_tools
from .runner import ClaudeClient, run_agentic


def format_overview_prompt(diff: AugmentedDiff, meta: dict[str, Any]) -> str:
    """Produce the user-message text for the overview call."""
    parts: list[str] = []
    title = meta.get("title", "")
    body = (meta.get("body") or "").strip()
    parts.append(f"# PR\ntitle: {title}\n")
    if body:
        # Trim body — overview doesn't need the full novel.
        if len(body) > 4000:
            body = body[:4000] + "\n... [PR body truncated for brevity] ..."
        parts.append(f"body:\n{body}\n")

    parts.append("# Diffstat")
    for f in diff.files:
        adds = sum(1 for ln in f.hunks[0].body.splitlines() if ln.startswith("+")) if f.hunks else 0
        dels = sum(1 for ln in f.hunks[0].body.splitlines() if ln.startswith("-")) if f.hunks else 0
        # more accurate: sum across hunks
        adds = sum(sum(1 for ln in h.body.splitlines() if ln.startswith("+")) for h in f.hunks)
        dels = sum(sum(1 for ln in h.body.splitlines() if ln.startswith("-")) for h in f.hunks)
        parts.append(f"  {f.path}  +{adds} -{dels}  ({len(f.hunks)} hunks)")

    parts.append("\n# Hunk headers")
    for f in diff.files:
        parts.append(f"{f.path}")
        for h in f.hunks:
            parts.append(f"  {h.header}")

    return "\n".join(parts) + "\n"


async def run_overview_pass(
    client: ClaudeClient,
    *,
    diff: AugmentedDiff,
    meta: dict[str, Any],
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the overview call. Returns the raw submit_args from the model."""
    user_text = format_overview_prompt(diff, meta)

    if cache is not None:
        key = cache.key("overview", model, OVERVIEW_SYSTEM, user_text)
        entry = cache.get(key)
        if entry is not None:
            if trace_dir is not None:
                _write_cache_hit_marker(trace_dir / "overview.json", "overview", entry)
            return entry["response"]

    user_content = [{"type": "text", "text": user_text}]
    trace_path = (trace_dir / "overview.json") if trace_dir is not None else None
    result = await run_agentic(
        client,
        model=model,
        system=OVERVIEW_SYSTEM,
        user_content=user_content,
        tools=overview_tools(),
        submit_tool_name="submit_overview",
        trace_path=trace_path,
    )

    if cache is not None:
        cache.put(
            key, request={"system": OVERVIEW_SYSTEM, "user": user_text},
            response=result.submit_args,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
        )
    return result.submit_args


def _write_cache_hit_marker(path: Path, pass_name: str, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"cache_hit": True, "pass": pass_name, "response": entry.get("response")},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def apply_overview_to_diff(diff: AugmentedDiff, submit_args: dict[str, Any]) -> None:
    """Fold a submit_overview payload into an AugmentedDiff in place."""
    diff.overview = Overview(
        summary=submit_args.get("summary", ""),
        symbols_added=[OverviewSymbol(**s) for s in submit_args.get("symbols_added", [])],
        symbols_modified=[OverviewSymbol(**s) for s in submit_args.get("symbols_modified", [])],
        symbols_removed=[OverviewSymbol(**s) for s in submit_args.get("symbols_removed", [])],
        callgraph_edges=[OverviewEdge.model_validate(e) for e in submit_args.get("callgraph_edges", [])],
        themes=list(submit_args.get("themes", [])),
    )
    by_path = {f["path"]: f for f in submit_args.get("files", [])}
    for fp in diff.files:
        entry = by_path.get(fp.path)
        if entry is None:
            continue
        fp.summary = entry.get("summary", "")
        lang = entry.get("lang")
        if lang:
            fp.lang = lang
        sym = entry.get("symbols")
        if isinstance(sym, dict):
            fp.symbols = FileSymbols(
                added=list(sym.get("added", [])),
                modified=list(sym.get("modified", [])),
                removed=list(sym.get("removed", [])),
            )
