"""Change-explainer generation: the skeleton pass (ADR 0007, slice 1).

Generation is not part of `augment_run_dir`. It runs from a route on
the live review server, on request, following the `/fold-summary`
pattern: an async closure wired in after augment completes, results
fanned out over the SSE bus. A document nobody opens costs nothing.

The skeleton is one structured, tool-less call. It fixes the decisions
the later prose calls must agree on (the verdict, the figure family and
cast) and writes the reading **Map** in full — the cheapest section and
the one worth the most at t=0. The three prose sections are written
`pending`; filling them is the prose route in `explainer_section.py`,
which writes them in two tool-using calls rather than three.

The document's shape, its persistence and its reference validation live
in `explainer_schema.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from ..structural import SymbolDelta
from ..viewer.build_json import ViewerIdIndex, viewer_id_index
from . import explainer_schema, prompts
from .agents import Client
from .pass_ import PassMeta, run_pass
from .prompts import EXPLAINER_ROLE, EXPLAINER_SKELETON_GUIDANCE
from .schemas import AnnotatedDiff, AnnotatedFile

log = logging.getLogger(__name__)


_SKELETON = PassMeta(name="explainer-skeleton", submit_tool="submit_explainer_skeleton")

#: Sections the skeleton leaves for the prose route, in document order.
#: Derived from the pass table rather than restated: the Map leads the
#: document, and these follow in the order their calls write them.
_PROSE_KINDS: tuple[explainer_schema.SectionKind, ...] = explainer_schema.prose_kinds()


class ExplainerNotReady(RuntimeError):
    """The run dir has no `augmented.scr.json` yet.

    Maps to HTTP 409 at the review-server boundary — the skeleton is
    seeded with the overview, so it cannot run before the augment pass
    has produced one.
    """


class SkeletonMapRow(BaseModel):
    """One Map row as the model submits it: a file id and why to read it."""

    file_id: str = Field(description="A viewer file id from the `# Files` section, verbatim (e.g. `F3`).")
    why: str = Field(description="One sentence: what this file teaches that the ones before it did not.")


class SkeletonSectionRefs(BaseModel):
    """The files one prose section is written about.

    File ids only: the skeleton is shown files, not hunks. The
    per-section pass expands each file into its hunks and their intents,
    and may narrow its own references down to individual hunks from
    there.
    """

    section: Literal["background", "intuition", "code"] = Field(
        description="Which prose section these files belong to."
    )
    file_ids: list[str] = Field(
        default_factory=list,
        description="Viewer file ids from the `# Files` section, verbatim. May be empty.",
    )


class ExplainerSkeletonSubmission(BaseModel):
    """Wire format of `submit_explainer_skeleton`."""

    verdict: Literal["narrate", "not_warranted"] = Field(
        description="`not_warranted` when a document would not beat reading the hunks directly."
    )
    verdict_note: str = Field(
        default="",
        description="One or two sentences. On `not_warranted` this is the whole answer the reviewer gets.",
    )
    figure_family: str = Field(
        default="",
        description="One sentence fixing which shape carries which meaning in this change's diagrams.",
    )
    cast: list[str] = Field(
        default_factory=list,
        description="The named components that recur across the change, plus the data object worth tracing.",
    )
    map_rows: list[SkeletonMapRow] = Field(
        default_factory=list,
        description="The reading order: source of truth, then consumers, then tests and docs, then generated output.",
    )
    section_refs: list[SkeletonSectionRefs] = Field(
        default_factory=list,
        description="Which files each prose section is written about. One entry per section, at most.",
    )


def make_explainer_skeleton_agent(model: str | Model, system: str) -> Agent[None, ExplainerSkeletonSubmission]:
    """Agent for the skeleton pass.

    No repo tools: the skeleton orders files it is already shown, and
    it is on the critical path for the first screen. The prose passes
    are where the worktree is read. `system` is the fully-assembled
    instruction text, which differs by backend — see
    :func:`carry_guidance`.
    """
    return Agent(
        model=model,
        output_type=ToolOutput(ExplainerSkeletonSubmission, name="submit_explainer_skeleton"),
        instructions=system,
    )


def carry_guidance(client: Client, guidance: str) -> tuple[str, str]:
    """Split the prompt between argv and stdin for this backend.

    The CLI drivers put `system_text` on argv as `--system-prompt`, and
    a long command line is not free — Cortex XDR, deployed on CPG
    laptops, parses argv quadratically. So argv carries only bounded
    fixed strings and bulk guidance rides stdin instead. SDK backends
    keep the guidance in the system prompt, where it is the cacheable
    prefix.

    Args:
        client: The backend handle; `is_subprocess_backend` chooses the
            carrier.
        guidance: The bulk block for this call — the skeleton's, or a
            prose section's plus :func:`prose_figure_guidance`.

    Returns:
        `(system_text, user_prefix)`. Exactly one of them carries the
        guidance block; the model sees the same words either way.
    """
    if client.is_subprocess_backend:
        return EXPLAINER_ROLE, guidance
    return f"{EXPLAINER_ROLE}\n\n{guidance}", ""


def prose_figure_guidance(doc: explainer_schema.ExplainerDocument) -> str:
    """Everything a prose call needs to draw a figure for this document.

    The vocabulary block, which is fixed, plus the family and cast the
    skeleton fixed for this change. Returns `""` when the skeleton fixed
    no family: with nothing to keep three sections drawing the same
    component the same way, the document is better off with no figures
    than with three visual languages. `explainer_schema.figures_fixed`
    is that condition, and the apply step drops what a call with no
    guidance submits anyway.
    """
    if not explainer_schema.figures_fixed(doc):
        return ""
    context = prompts.format_figure_context(doc.figure_family, doc.cast)
    return f"{prompts.EXPLAINER_FIGURE_GUIDANCE}\n\n{context}"


def format_skeleton_prompt(
    diff: AnnotatedDiff,
    *,
    overview_json: str,
    delta: SymbolDelta | None = None,
) -> str:
    """Assemble the skeleton call's user text.

    The `# Files` section is the id map the Map rows must cite from, so
    it carries each file's viewer id alongside its path, size and role.
    """
    parts = [f"# Change overview\n{overview_json}", "", _format_file_list(diff)]
    symbols = _format_symbol_section(delta)
    if symbols:
        parts += ["", symbols]
    parts += ["", "Produce the skeleton: the verdict, the figure family and cast, and the Map."]
    return "\n".join(parts) + "\n"


def file_seed_lines(index: int, file: AnnotatedFile) -> list[str]:
    """One file's rows in a seed listing: its id row, then its summary.

    Shared with the prose passes' seed in `explainer_section.py`, which
    interleaves each file's hunks under these rows. One formatter, so the
    skeleton and the prose calls cannot come to describe the same file
    differently.
    """
    adds = sum(sum(1 for ln in h.parsed.body.splitlines() if ln.startswith("+")) for h in file.hunks)
    dels = sum(sum(1 for ln in h.parsed.body.splitlines() if ln.startswith("-")) for h in file.hunks)
    role = file.ann.role.value if file.ann.role else "modified"
    lines = [f"  F{index}  {file.path}  +{adds} -{dels}  ({len(file.hunks)} hunks, {role})"]
    summary = (file.ann.summary or "").strip()
    if summary:
        lines.append(f"        {summary}")
    return lines


def _format_file_list(diff: AnnotatedDiff) -> str:
    lines = [
        "# Files",
        "Cite these ids verbatim in `map_rows[].file_id`. Nothing else is a valid id.",
    ]
    for fi, f in enumerate(diff.files):
        lines += file_seed_lines(fi, f)
    return "\n".join(lines)


def _format_symbol_section(delta: SymbolDelta | None) -> str:
    """Render the deterministic symbol delta compactly, or `""`.

    Kind and qualified name only: the skeleton is ordering files, so a
    symbol's line range would be weight it cannot spend.
    """
    if delta is None:
        return ""
    buckets = (("added", delta.added), ("removed", delta.removed), ("modified", delta.modified))
    if not any(items for _, items in buckets):
        return ""
    lines = ["# Symbols changed (deterministic — tree-sitter, not inference)"]
    for label, items in buckets:
        if not items:
            continue
        lines.append(f"{label}:")
        for c in items:
            lines.append(f"  {c.kind} {c.qualified_name}  ({c.path})")
    return "\n".join(lines)


def build_skeleton_document(
    submission: ExplainerSkeletonSubmission,
    *,
    base_sha: str,
    head_sha: str,
    ids: ViewerIdIndex,
) -> explainer_schema.ExplainerDocument:
    """Fold a `submit_explainer_skeleton` payload into a document.

    Map rows whose `file_id` addresses no file in this diff are dropped
    and counted into `dropped_refs`. A `not_warranted` verdict yields
    the Map alone: the document *is* the answer, so there is nothing to
    mark pending and nothing to offer to generate.
    """
    rows: list[explainer_schema.MapRow] = []
    dropped = 0
    for raw in submission.map_rows:
        ref = explainer_schema.Reference(kind="file", id=raw.file_id)
        kept, n = explainer_schema.validate_references([ref], file_ids=ids.files, hunk_ids=ids.hunks)
        dropped += n
        if kept:
            rows.append(explainer_schema.MapRow(ref=kept[0], why=raw.why.strip()))

    section_refs, n = _section_refs(submission.section_refs, ids=ids)
    dropped += n

    map_section = explainer_schema.Section(
        id="map",
        kind="map",
        pass_id=explainer_schema.SKELETON_PASS,
        title=explainer_schema.SECTION_TITLES["map"],
        state="ready",
        refs=[row.ref for row in rows],
        map_rows=rows,
    )
    # Map leads. It is the only section the skeleton can fill, so it is what
    # renders the moment the button is pressed; behind three pending sections
    # the first screen would be entirely things that are not ready yet.
    sections = [map_section]
    if submission.verdict != "not_warranted":
        sections.extend(_pending_section(k, section_refs.get(k, [])) for k in _PROSE_KINDS)

    return explainer_schema.ExplainerDocument(
        base_sha=base_sha,
        head_sha=head_sha,
        verdict=submission.verdict,
        verdict_note=submission.verdict_note.strip(),
        figure_family=submission.figure_family.strip(),
        cast=[c.strip() for c in submission.cast if c.strip()],
        sections=sections,
        dropped_refs=dropped,
    )


def _section_refs(
    submitted: list[SkeletonSectionRefs],
    *,
    ids: ViewerIdIndex,
) -> tuple[dict[str, list[explainer_schema.Reference]], int]:
    """Validated per-section file references, keyed by section kind.

    A section's references are what its prose pass is seeded with, so an
    invented file id here costs that section the code it was meant to be
    written about. Same policy as everywhere else: dropped, counted, and
    surfaced in the document's `dropped_refs`.

    Returns:
        `(refs_by_kind, dropped_count)`. A kind the model omitted is
        absent from the mapping; duplicates within one kind are collapsed
        keeping first-seen order.
    """
    out: dict[str, list[explainer_schema.Reference]] = {}
    dropped = 0
    for entry in submitted:
        refs = [explainer_schema.Reference(kind="file", id=fid) for fid in entry.file_ids]
        kept, n = explainer_schema.validate_references(refs, file_ids=ids.files, hunk_ids=ids.hunks)
        dropped += n
        bucket = out.setdefault(entry.section, [])
        seen = {r.id for r in bucket}
        for ref in kept:
            if ref.id in seen:
                continue
            seen.add(ref.id)
            bucket.append(ref)
    return out, dropped


def _pending_section(
    kind: explainer_schema.SectionKind,
    refs: list[explainer_schema.Reference],
) -> explainer_schema.Section:
    return explainer_schema.Section(
        id=kind,
        kind=kind,
        pass_id=explainer_schema.pass_for_kind(kind),
        title=explainer_schema.SECTION_TITLES[kind],
        state="pending",
        refs=refs,
    )


async def generate_explainer_skeleton(
    client: Client,
    *,
    run_dir: Path,
    model: str,
    house_style: str | None,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> explainer_schema.ExplainerDocument:
    """Run the skeleton pass against a run dir and persist the result.

    Args:
        client: The LLM backend handle. Chooses the guidance carrier.
        run_dir: The run directory; must already hold `augmented.scr.json`.
        model: The user-facing model string, for the cache key.
        house_style: The reviewed repo's house-style note, or None. No
            default: a repo that configured none is an ordinary state,
            but it is the caller's to state — the wiring bug this
            catches is a flow that never threads the config field
            through at all.
        cache: Optional response cache.
        trace_dir: Optional `trace/` directory for the call envelope.

    Returns:
        The persisted document.

    Raises:
        ExplainerNotReady: The augment pass has not written a sidecar,
            so there is no overview to seed the call with.
    """
    sidecar = run_dir / "augmented.scr.json"
    if not sidecar.exists():
        raise ExplainerNotReady("augmented.scr.json missing — augment not complete")

    # Lazy: keeps the format machinery off the import path for callers
    # that only want the prompt assembly or the apply step.
    from ..format.sidecar import load_sidecar
    from .hunks import overview_to_prompt_json

    diff = load_sidecar(sidecar)
    guidance = EXPLAINER_SKELETON_GUIDANCE
    if house_style is not None:
        guidance = f"{guidance}\n\n{prompts.format_house_style(house_style)}"
    system_text, user_prefix = carry_guidance(client, guidance)
    user_text = format_skeleton_prompt(
        diff,
        overview_json=overview_to_prompt_json(diff, include_symbols=False),
        delta=_symbol_delta(run_dir, diff),
    )
    if user_prefix:
        user_text = f"{user_prefix}\n\n{user_text}"

    payload = await run_pass(
        _SKELETON,
        client=client,
        agent=make_explainer_skeleton_agent(client.model, system_text),
        user_content=user_text,
        system=system_text,
        model=model,
        cache_inputs=(user_text,),
        cache=cache,
        trace_path=(trace_dir / "explainer-skeleton.json") if trace_dir is not None else None,
        cache_request={"system": system_text, "user": user_text},
    )
    assert payload is not None  # `_SKELETON.swallow_errors` is false

    doc = build_skeleton_document(
        ExplainerSkeletonSubmission.model_validate(payload),
        base_sha=diff.pr.base_sha,
        head_sha=diff.pr.head_sha,
        ids=viewer_id_index(diff),
    )
    return explainer_schema.save_explainer(run_dir, doc)


def _symbol_delta(run_dir: Path, diff: AnnotatedDiff) -> SymbolDelta | None:
    """The deterministic base→head structural delta, or None.

    Best-effort, as it is for the overview seed: a parse failure leaves
    the skeleton unseeded rather than aborting the document.
    """
    from .source_cache import SourceCache
    from .tools import RepoTools

    try:
        return RepoTools(
            head_worktree=run_dir / "head",
            repo_git=run_dir / "repo.git",
            base_sha=diff.pr.base_sha,
            head_sha=diff.pr.head_sha,
            cache=SourceCache(),
        ).compute_symbol_delta()
    except Exception:  # noqa: BLE001 — seed is best-effort
        log.warning("structural symbol seed failed; explainer skeleton runs unseeded", exc_info=True)
        return None


def document_to_payload(doc: explainer_schema.ExplainerDocument) -> dict[str, Any]:
    """The document as the wire shape the route and the SSE frame carry."""
    return doc.model_dump(mode="json")
