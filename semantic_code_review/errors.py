"""The failures a review answers with, rather than crashes on.

An augment pass refusing (no sidecar yet, a section whose hunks aren't
annotated) and the review session refusing (a malformed request, a pass
already in flight) are the same kind of event: the reviewer gets told
what happened, in terms the viewer can act on. `ScrError` is the one
base for those, so `review/server.py` maps every one of them in a single
place instead of matching classes route by route.

Stdlib only and dependency-free in both directions: the augment layer
and the review layer each import it, neither imports the other.
"""

from __future__ import annotations

from typing import Any


class ScrError(Exception):
    """A domain failure the review server answers with directly.

    Subclasses set `status` to the HTTP status their failure means, and
    override `body()` when the answer needs more than the message — the
    counts that tell a viewer what a pass is waiting for, say. An
    exception that is *not* a `ScrError` is a bug rather than an
    outcome, and the routes answer it with a 500 naming its type.
    """

    #: The status the routes answer with. 500 on the base because an
    #: unclassified failure is the server's fault by default.
    status: int = 500

    def body(self) -> dict[str, Any]:
        """The JSON body sent alongside `status`."""
        return {"error": str(self)}
