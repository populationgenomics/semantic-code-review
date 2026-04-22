"""Strip augmented-diff annotations, leaving a plain unified diff.

The result is guaranteed to contain only lines that would appear in a
standard unified diff, so `git apply` accepts it.
"""

from __future__ import annotations


def strip_annotations(text: str) -> str:
    """Return `text` with every `#scr:` / `#scr>` line removed."""
    out: list[str] = []
    for line in text.split("\n"):
        if line.startswith("#scr:") or line.startswith("#scr>"):
            continue
        out.append(line)
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result
