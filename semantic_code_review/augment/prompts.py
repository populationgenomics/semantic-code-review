"""System prompts and tool schemas for the two LLM passes.

Bump `PROMPT_VERSION` when either prompt or a schema changes — the cache
layer keys on it so a bump forces a full re-run.
"""

from __future__ import annotations

from typing import Any

from .tools import ANTHROPIC_TOOL_SCHEMAS


PROMPT_VERSION = "p4"


# --- Submission tools -------------------------------------------------------

_SMELL_TAGS = (
    "duplication, string-sql, no-input-validation, missing-test, "
    "security-sensitive, performance-regression, backward-incompatible, "
    "todo-left-behind, dead-code, unscoped-exception, resource-leak, race-condition"
)

SUBMIT_OVERVIEW_TOOL: dict[str, Any] = {
    "name": "submit_overview",
    "description": "Submit the final PR overview. Call this exactly once when you have the complete structure.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "1-3 sentence summary of the PR's intent."},
            "symbols_added": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "kind": {"type": "string", "description": "function, method, class, constant"},
                        "name": {"type": "string"},
                    },
                    "required": ["path", "kind", "name"],
                },
            },
            "symbols_modified": {"type": "array", "items": {"$ref": "#/properties/symbols_added/items"}},
            "symbols_removed": {"type": "array", "items": {"$ref": "#/properties/symbols_added/items"}},
            "callgraph_edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string", "description": "<path>:<name>"},
                        "to": {"type": "string", "description": "<path>:<name>"},
                    },
                    "required": ["from", "to"],
                },
            },
            "themes": {"type": "array", "items": {"type": "string"}},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "summary": {"type": "string"},
                        "lang": {"type": "string"},
                        "symbols": {
                            "type": "object",
                            "properties": {
                                "added": {"type": "array", "items": {"type": "string"}},
                                "modified": {"type": "array", "items": {"type": "string"}},
                                "removed": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["summary", "files"],
    },
}


SUBMIT_ANNOTATIONS_TOOL: dict[str, Any] = {
    "name": "submit_annotations",
    "description": "Submit the final annotations for this hunk. Call exactly once when you are ready.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "1-2 sentences of MOTIVE, not mechanics."},
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "new_start": {"type": "integer", "description": "Post-image line where the segment starts."},
                        "new_count": {"type": "integer", "description": "Number of post-image lines covered."},
                        "intent": {"type": "string"},
                        "smells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tag": {"type": "string", "description": f"One of: {_SMELL_TAGS}"},
                                    "note": {"type": "string"},
                                },
                                "required": ["tag"],
                            },
                        },
                        "context": {"type": "string"},
                        "refs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "line": {"type": "integer"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["path", "line"],
                            },
                        },
                    },
                    "required": ["new_start", "new_count", "intent"],
                },
            },
            "smells": {"$ref": "#/properties/segments/items/properties/smells"},
            "context": {"type": "string"},
            "refs": {"$ref": "#/properties/segments/items/properties/refs"},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "line_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer", "description": "Post-image line number."},
                        "body": {"type": "string"},
                    },
                    "required": ["line", "body"],
                },
            },
            "fold_descriptions": {
                "type": "array",
                "description": "One-line summary per indent fold region containing changes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "new_start": {"type": "integer"},
                        "new_count": {"type": "integer"},
                        "summary": {"type": "string", "description": "Short sentence, <= 12 words, plain text."},
                    },
                    "required": ["new_start", "new_count", "summary"],
                },
            },
        },
        "required": ["intent"],
    },
}


# --- System prompts ---------------------------------------------------------

OVERVIEW_SYSTEM = (
    "You are preparing a structured overview of a pull request (or a local diff) to "
    "help a human reviewer understand its shape at a glance.\n\n"
    "You receive the PR title and body, a diffstat, and the hunk headers of each "
    "changed file (no bodies). Produce a concise overview by calling "
    "`submit_overview`.\n\n"
    "Guidelines:\n"
    "- Lead with WHY, not WHAT.\n"
    "- Symbol kinds are: function, method, class, constant.\n"
    "- `callgraph_edges` are introduced or modified calls (best-effort — omit if unsure).\n"
    "- `themes` are short keyword tags (e.g. 'pagination', 'api-surface').\n"
    "- Per-file `summary` is one sentence; `lang` only when the extension is ambiguous.\n"
    "- Favour clarity over completeness: the reviewer uses this to decide where to look.\n"
    "- If the PR body contains a specification markdown block (look for a `# Spec` "
    "  heading or similar), treat it as GROUND TRUTH for what the change was meant to "
    "  accomplish. Call out in `summary` and `themes` any parts of the spec that look "
    "  under-implemented, not implemented at all, or diverged from. Do not invent spec "
    "  requirements that aren't in the body.\n"
)


HUNK_SYSTEM = (
    "You are reviewing one hunk of a pull request. Your FIRST job is to help a human "
    "reviewer UNDERSTAND what this change does and why. Critique (smells, risks) is "
    "SECONDARY — only raise concerns when you can name a concrete risk.\n\n"
    "You have tools to read other files in the head worktree and at the base SHA, to "
    "grep, to list directories, and to check git history. Use them when the hunk depends "
    "on code outside the diff; skip them if the hunk is self-contained.\n\n"
    "When done, call `submit_annotations` with:\n"
    "- `intent`: 1-2 sentences. MOTIVE, not mechanics. Bad: 'renames X to Y'. "
    "Good: 'rename aligns the public API with the new namespace introduced earlier'.\n"
    "- `segments`: when the hunk contains semantically distinct edits (e.g. a refactor "
    "plus an unrelated fix, or a changed if-branch alongside a new else-branch), split "
    "them. Each segment has POST-IMAGE `new_start`/`new_count` and its own intent. Omit "
    "segments if the hunk is single-intent.\n"
    "- `smells`: list of {tag, note}. Tags are from the closed vocabulary: "
    f"{_SMELL_TAGS}. Attach each smell to a segment when it's segment-local, or to the "
    "hunk when it spans the whole change.\n"
    "- `context`: cross-file dependencies the reviewer can't see from the diff.\n"
    "- `refs`: {path, line, reason} for other files the reviewer should look at.\n"
    "- `confidence`: 0-100 integer. Low is fine and honest.\n"
    "- `line_notes`: {line, body} for notes too specific for intent. `line` is post-image.\n"
    "- `fold_descriptions`: the viewer supports indent-based code folding inside the "
    "diff. The prompt lists the nested fold regions in this hunk that contain changed "
    "lines. For EACH region, return a one-line hint (<= 15 words, present tense, "
    "lowercase) that tells a reviewer who has the fold collapsed what the WHOLE folded "
    "block DOES as a unit. Describe behaviour, not structure; describe the effect, not "
    "the control flow. If the folded block REPLACES existing code, describe the "
    "CHANGE the block introduces, not just the new steps.\n"
    "Good: 'convert every top-level message in every descriptor to a json schema'. "
    "Good: 'fall back to inline generation when include_all is set'. "
    "Good: 'forward page/size kwargs to list_users so pagination reaches the handler'. "
    "Bad: 'iterate every file descriptor in the set' (structure, not effect). "
    "Bad: 'if / elif / else' (control flow). "
    "Bad: 'call self._convert_message_to_schema' (names a call, not what is achieved).\n"
    "Match each region's `new_start`/`new_count` exactly. If no regions are listed, "
    "omit the field.\n\n"
    "Tone: explanatory, not evaluative. Comprehension first."
)


# --- Tool sets exposed to each pass ----------------------------------------

def overview_tools() -> list[dict[str, Any]]:
    return [SUBMIT_OVERVIEW_TOOL]


def hunk_tools() -> list[dict[str, Any]]:
    return [*ANTHROPIC_TOOL_SCHEMAS, SUBMIT_ANNOTATIONS_TOOL]
