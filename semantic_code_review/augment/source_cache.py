"""In-memory `(sha, path)` memo for source reads and structural parses.

Tools are read-only against pinned base/head SHAs (ADR 0003), so a file's
content at a given `(sha, path)` is immutable for the run's lifetime and
never needs invalidation. This memoises the two expensive operations the
tool surface repeats — reading a file (worktree read or `git show`) and
its tree-sitter outline parse — so repeated tool calls over the same
`(sha, path)` pay the cost once.

`sha is None` denotes the head worktree; it keys separately from an
explicit `head_sha` read (which goes through `git show`).

Owned by the run and passed into every `RepoTools` — never a module
global (ADR 0003; the no-global-mutable-state rule). Single-threaded: the
SDK tool loop calls these from the event-loop thread, and the stdio MCP
server is one process per spawn, so a plain dict suffices. Concurrent
access from a hosted HTTP server (multiple client threads) is out of
scope until that slice, which adds per-key locking.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import structural

_Key = tuple[str | None, str]


class SourceCache:
    """Compute-once memo for `(sha, path)` reads and outline parses.

    Agnostic to how values are produced: callers pass a `compute` thunk
    that runs only on a miss. Values (including `None` for an unreadable
    file, and `[]` for an empty/failed parse) are cached — a miss is
    distinguished by key presence, not truthiness.
    """

    def __init__(self) -> None:
        self._sources: dict[_Key, str | None] = {}
        self._outlines: dict[_Key, list[structural.Symbol]] = {}

    def source(self, sha: str | None, path: str, compute: Callable[[], str | None]) -> str | None:
        """Raw file content for `(sha, path)`; `compute()` runs only on a miss."""
        key = (sha, path)
        if key not in self._sources:
            self._sources[key] = compute()
        return self._sources[key]

    def outline(
        self,
        sha: str | None,
        path: str,
        compute: Callable[[], list[structural.Symbol]],
    ) -> list[structural.Symbol]:
        """Outline symbols for `(sha, path)`; `compute()` runs only on a miss."""
        key = (sha, path)
        if key not in self._outlines:
            self._outlines[key] = compute()
        return self._outlines[key]
