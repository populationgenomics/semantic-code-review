"""Which changed files the LLM passes leave un-analysed.

Lock files, vendored bundles, and binary formats carry no reviewable
intent. Two consumers must agree on this set: the augment pipeline (which
decides what to *dispatch* for per-hunk analysis) and the pending-page
builder (which decides what enters the viewer's progress grid). If they
diverge, a file the pipeline skips still shows in the grid and sits
"queued" forever — the pipeline never emits an event for it — and the
review can't reach a complete state. So the decision lives here, imported
by both. Deliberately dependency-free (stdlib `fnmatch`) so the
lightweight page-build path never pulls the augment stack.
"""

from __future__ import annotations

import fnmatch

# Paths we do not send to the LLM — lock files, vendored bundles, binary
# formats. The hunks still appear in the viewer; they render as "generated"
# (not analysed) rather than being queued for a per-hunk pass.
DEFAULT_SKIP_GLOBS: tuple[str, ...] = (
    "*.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.otf",
    "*.pdf",
)

# The file summary shown for a skipped file in both the pending page and
# the augmented output — kept identical so the two paths never disagree.
SKIP_SUMMARY = "Generated / lock file — not analysed."


def should_skip(path: str, extra_globs: tuple[str, ...] = ()) -> bool:
    """True if `path` matches a default or caller-supplied skip glob.

    Matches against both the full path and the basename, so a glob like
    `uv.lock` catches `uv.lock` and `sub/uv.lock` alike.
    """
    globs = DEFAULT_SKIP_GLOBS + tuple(extra_globs)
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in globs)
