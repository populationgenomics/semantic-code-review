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
    AugmentedDiff, FilePatch, FileSymbols, Overview, OverviewEdge,
    OverviewGroup, OverviewGroupMember, OverviewSymbol,
)
from ..cache.store import CacheStore
from .agents import Backend, make_overview_agent
from .prompts import OVERVIEW_SYSTEM, PROMPT_VERSION
from .trace_adapter import submit_args_from_result, write_pydantic_ai_trace


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

    # Each hunk header is prefixed with its 0-based `hunk_index` within
    # the file, so the model can cite `{path, hunk_index}` from the
    # `groups` output unambiguously.
    parts.append("\n# Hunk headers")
    for f in diff.files:
        parts.append(f"{f.path}")
        for i, h in enumerate(f.hunks):
            parts.append(f"  [{i}] {h.header}")

    return "\n".join(parts) + "\n"


async def run_overview_pass(
    backend: Backend,
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

    trace_path = (trace_dir / "overview.json") if trace_dir is not None else None

    agent = make_overview_agent(backend.model)
    run_result = await agent.run(user_text)
    submit_args = submit_args_from_result(run_result)
    if trace_path is not None:
        write_pydantic_ai_trace(
            run_result,
            trace_path=trace_path,
            model=str(backend.model),
            system=OVERVIEW_SYSTEM,
            tool_names=[],
            submit_tool="submit_overview",
        )
    usage = run_result.usage()
    tokens_in = usage.input_tokens or 0
    tokens_out = usage.output_tokens or 0

    if cache is not None:
        cache.put(
            key, request={"system": OVERVIEW_SYSTEM, "user": user_text},
            response=submit_args,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
    return submit_args


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
        groups=_resolve_groups(diff, submit_args.get("groups") or []),
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


def _resolve_groups(diff: AugmentedDiff, raw_groups: list[dict[str, Any]]) -> list[OverviewGroup]:
    """Build OverviewGroup instances from raw submit_overview payload.

    Members whose (path, hunk_index) don't resolve to a real hunk in
    the diff are dropped with a warning, the same defensive pattern
    hunks.py uses for out-of-range segments. A group whose members
    all get dropped is itself dropped.
    """
    import logging
    log = logging.getLogger(__name__)
    hunks_per_path: dict[str, int] = {fp.path: len(fp.hunks) for fp in diff.files}

    out: list[OverviewGroup] = []
    for raw in raw_groups:
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        rationale = (raw.get("rationale") or "").strip()
        members: list[OverviewGroupMember] = []
        for m in raw.get("members") or []:
            try:
                path = str(m["path"])
                idx = int(m["hunk_index"])
            except (KeyError, TypeError, ValueError):
                log.warning("group %r: malformed member %r — dropped", title, m)
                continue
            n = hunks_per_path.get(path)
            if n is None:
                log.warning("group %r: path %r not in diff — dropped", title, path)
                continue
            if idx < 0 or idx >= n:
                log.warning(
                    "group %r: hunk_index %d out of range for %s (n=%d) — dropped",
                    title, idx, path, n,
                )
                continue
            members.append(OverviewGroupMember(path=path, hunk_index=idx))
        if not members:
            log.warning("group %r: no valid members — dropped", title)
            continue
        out.append(OverviewGroup(title=title, rationale=rationale, members=members))
    return out
