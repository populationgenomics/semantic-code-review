"""Overview pass: one call per PR producing the PR-level summary.

Input: PR metadata + diffstat + per-file hunk headers (bodies omitted
to save tokens). Output: the `Overview` object plus per-file summary
text and optional `lang` override that populate `FileAnnotations` fields.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..augment.schemas import (
    AnnotatedDiff, AnnotatedFile, FileAnnotations, FileSymbols, Overview,
    OverviewEdge, OverviewGroup, OverviewGroupMember, OverviewSymbol,
)
from ..cache.store import CacheStore
from .agents import Client, make_overview_agent
from .pass_ import PassMeta, run_pass
from .prompts import OVERVIEW_SYSTEM


log = logging.getLogger(__name__)


_OVERVIEW = PassMeta(name="overview", submit_tool="submit_overview")


def format_overview_prompt(diff: AnnotatedDiff, meta: dict[str, Any]) -> str:
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
        adds = sum(sum(1 for ln in h.parsed.body.splitlines() if ln.startswith("+")) for h in f.hunks)
        dels = sum(sum(1 for ln in h.parsed.body.splitlines() if ln.startswith("-")) for h in f.hunks)
        parts.append(f"  {f.path}  +{adds} -{dels}  ({len(f.hunks)} hunks)")

    # Each hunk header is prefixed with its 0-based `hunk_index` within
    # the file, so the model can cite `{path, hunk_index}` from the
    # `groups` output unambiguously.
    parts.append("\n# Hunk headers")
    for f in diff.files:
        parts.append(f"{f.path}")
        for i, h in enumerate(f.hunks):
            parts.append(f"  [{i}] {h.parsed.header}")

    return "\n".join(parts) + "\n"


async def run_overview_pass(
    client: Client,
    *,
    diff: AnnotatedDiff,
    meta: dict[str, Any],
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the overview call. Returns the raw submit_args from the model."""
    user_text = format_overview_prompt(diff, meta)
    payload = await run_pass(
        _OVERVIEW,
        client=client,
        agent=make_overview_agent(client.model),
        user_content=user_text,
        system=OVERVIEW_SYSTEM,
        model=model,
        cache_inputs=(user_text,),
        cache=cache,
        trace_path=(trace_dir / "overview.json") if trace_dir is not None else None,
        cache_request={"system": OVERVIEW_SYSTEM, "user": user_text},
    )
    # `_OVERVIEW.swallow_errors` is false, so `payload` is never None here.
    assert payload is not None
    return payload


def apply_overview_to_diff(diff: AnnotatedDiff, submit_args: dict[str, Any]) -> AnnotatedDiff:
    """Fold a submit_overview payload into an AnnotatedDiff. Returns a new
    AnnotatedDiff; `diff` is not mutated.

    Per-file fields named in the submission overwrite existing
    `FileAnnotations.summary`/`lang`/`symbols`; files not named are
    untouched (preserving e.g. the `GENERATED` role pre-set by the
    pipeline for skipped files).
    """
    overview = Overview(
        summary=submit_args.get("summary", ""),
        symbols_added=[OverviewSymbol(**s) for s in submit_args.get("symbols_added", [])],
        symbols_modified=[OverviewSymbol(**s) for s in submit_args.get("symbols_modified", [])],
        symbols_removed=[OverviewSymbol(**s) for s in submit_args.get("symbols_removed", [])],
        callgraph_edges=[OverviewEdge.model_validate(e) for e in submit_args.get("callgraph_edges", [])],
        themes=list(submit_args.get("themes", [])),
        groups=_resolve_groups(diff, submit_args.get("groups") or []),
    )
    by_path = {f["path"]: f for f in submit_args.get("files", [])}
    new_files: list[AnnotatedFile] = []
    for fp in diff.files:
        entry = by_path.get(fp.path)
        if entry is None:
            new_files.append(fp)
            continue
        sym = entry.get("symbols")
        ann = fp.ann.model_copy(update={
            "summary": entry.get("summary", ""),
            **({"lang": entry["lang"]} if entry.get("lang") else {}),
            **({"symbols": FileSymbols(
                added=list(sym.get("added", [])),
                modified=list(sym.get("modified", [])),
                removed=list(sym.get("removed", [])),
            )} if isinstance(sym, dict) else {}),
        })
        new_files.append(fp.model_copy(update={"ann": ann}))
    return diff.model_copy(update={"overview": overview, "files": new_files})


def _resolve_groups(diff: AnnotatedDiff, raw_groups: list[dict[str, Any]]) -> list[OverviewGroup]:
    """Build OverviewGroup instances from raw submit_overview payload.

    Members whose (path, hunk_index) don't resolve to a real hunk in
    the diff are dropped with a warning. A group whose members all get
    dropped is itself dropped.
    """
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
