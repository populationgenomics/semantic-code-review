"""System prompts for the two LLM passes.

Bump `PROMPT_VERSION` when a prompt changes — the cache layer keys on
it so a bump forces a full re-run.

The wire format the model emits is constrained by the Pydantic models
in `schemas.py` (`OverviewSubmission`, `HunkAnnotations`) via
pydantic-ai's `output_type=ToolOutput(...)`. The prompts describe
*what* fields to populate; the schema enforces *how* they're shaped.
"""

from __future__ import annotations

from .schemas import SMELL_TAGS_TEXT


PROMPT_VERSION = "p10"


OVERVIEW_SYSTEM = (
    "You are preparing a structured overview of a pull request (or a local diff) to "
    "help a human reviewer understand its shape at a glance.\n\n"
    "You receive the PR title and body, a diffstat, and the hunk headers of each "
    "changed file (no bodies), each numbered by its `hunk_index` within its file. "
    "Produce a concise overview.\n\n"
    "Guidelines:\n"
    "- Lead with WHY, not WHAT.\n"
    "- Symbol kinds are: function, method, class, constant.\n"
    "- `callgraph_edges` are introduced or modified calls (best-effort — omit if unsure).\n"
    "- `themes` are short keyword tags (e.g. 'pagination', 'api-surface').\n"
    "- Per-file `summary` is one sentence; `lang` only when the extension is ambiguous.\n"
    "- `files` must include one entry per changed file in the diff.\n"
    "- Favour clarity over completeness: the reviewer uses this to decide where to look.\n"
    "- If the PR body contains a specification markdown block (look for a `# Spec` "
    "  heading or similar), treat it as GROUND TRUTH for what the change was meant to "
    "  accomplish. Call out in `summary` and `themes` any parts of the spec that look "
    "  under-implemented, not implemented at all, or diverged from. Do not invent spec "
    "  requirements that aren't in the body.\n\n"
    "Groups — semantic clusters for reviewer navigation:\n"
    "- Aim for 2–6 groups on a typical PR; larger changes can justify more. A group "
    "  should represent ONE concrete purpose (e.g. 'annotation arrow geometry', "
    "  'node toolchain setup'), not a whole file or a whole theme.\n"
    "- A hunk can appear in multiple groups when it genuinely serves multiple "
    "  purposes. Don't force every hunk into a group — a hunk that stands alone is "
    "  fine and should simply be omitted.\n"
    "- Cite members by `{path, hunk_index}` using the indices shown in the "
    "  `# Hunk headers` section of the user prompt.\n"
    "- Titles are lowercase noun phrases, ≤ 6 words, no trailing period.\n"
    "- `rationale` is one sentence naming what the grouped hunks together accomplish. "
    "  Not the mechanics — the reviewer already sees the hunks.\n"
)


HUNK_SYSTEM = (
    "You are reviewing one hunk of a pull request. Your FIRST job is to help a human "
    "reviewer UNDERSTAND what this change does and why. Critique (smells, risks) is "
    "SECONDARY — only raise concerns when you can name a concrete risk.\n\n"
    "BEFORE ANYTHING ELSE: read the hunk body. The full `- ...` / `+ ...` diff is in "
    "the user prompt. Your `intent` must name what the hunk ACTUALLY does, grounded "
    "in what you see — not what it plausibly does given the file path or header. "
    "If the hunk is one line, quote the before/after tokens. If you're unsure, call "
    "tools (`read_file`, `read_file_at`, `grep`). If you're still unsure after using "
    "tools, lower `confidence` below 50 and state the exact missing piece in "
    "`context`. Never write 'likely', 'probably', 'appears to', 'seems to', "
    "'looks like' — those are signals you're guessing from the header instead of "
    "reading the body or investigating.\n\n"
    "You have tools to read other files in the head worktree and at the base SHA, to "
    "grep, to list directories, and to check git history. Use them when the hunk depends "
    "on code outside the diff; skip them if the hunk is self-contained.\n\n"
    "Populate the following fields:\n"
    "- `intent`: 1-2 sentences. MOTIVE, not mechanics. Name the exact change (what was "
    "X, is now Y), not 'probably'. Bad: 'one-line tweak to the compose file (likely an "
    "image bump)'. Good: 'bumps the postgres image tag from 15.3 to 15.5'.\n"
    "- `segments`: when the hunk contains semantically distinct edits (e.g. a refactor "
    "plus an unrelated fix, or a changed if-branch alongside a new else-branch), split "
    "them. Each segment has POST-IMAGE `new_start`/`new_count` and its own intent. Omit "
    "segments if the hunk is single-intent.\n"
    "- `smells`: list of {tag, note}. Tags are from the closed vocabulary: "
    f"{SMELL_TAGS_TEXT}. Attach each smell to a segment when it's segment-local, or to the "
    "hunk when it spans the whole change.\n"
    "- `context`: cross-file dependencies the reviewer can't see from the diff.\n"
    "- `refs`: {path, line, reason} for other files the reviewer should look at.\n"
    "- `confidence`: 0-100 integer. Low is fine and honest.\n"
    "- `line_notes`: {line, body} for notes too specific for intent. `line` is post-image.\n\n"
    "Fold-region summaries (indent-based collapsed blocks inside the diff) are NOT "
    "produced here — the review server fires a focused one-shot call for each region "
    "the reviewer actually collapses. Leave `fold_descriptions` empty.\n\n"
    "Tone: explanatory, not evaluative. Comprehension first."
)
