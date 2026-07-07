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
global (ADR 0003; the no-global-mutable-state rule).

Thread-safe by compute-once-per-key locking (ADR 0003 Slice 3): the
hosted HTTP server dispatches the ~8 concurrent `claude -p` clients' tool
calls on a worker pool, so several threads hit one cache at once. A
per-key lock guarantees each `(sha, path)` computes exactly once while
letting distinct keys compute concurrently; `compute()` runs outside the
map guard so a slow read never blocks unrelated keys.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from .. import structural

_Key = tuple[str | None, str]
_T = TypeVar("_T")


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
        # Guards the maps and the per-key lock registries; held only for
        # cheap dict ops, never across a compute().
        self._guard = threading.Lock()
        self._source_locks: dict[_Key, threading.Lock] = {}
        self._outline_locks: dict[_Key, threading.Lock] = {}

    def source(self, sha: str | None, path: str, compute: Callable[[], str | None]) -> str | None:
        """Raw file content for `(sha, path)`; `compute()` runs once on a miss."""
        return self._memoise(self._sources, self._source_locks, (sha, path), compute)

    def outline(
        self,
        sha: str | None,
        path: str,
        compute: Callable[[], list[structural.Symbol]],
    ) -> list[structural.Symbol]:
        """Outline symbols for `(sha, path)`; `compute()` runs once on a miss."""
        return self._memoise(self._outlines, self._outline_locks, (sha, path), compute)

    def _memoise(
        self,
        store: dict[_Key, _T],
        locks: dict[_Key, threading.Lock],
        key: _Key,
        compute: Callable[[], _T],
    ) -> _T:
        with self._guard:
            if key in store:
                return store[key]
            lock = locks.setdefault(key, threading.Lock())
        with lock:
            # Re-check: another thread may have computed this key while we
            # waited on its per-key lock.
            with self._guard:
                if key in store:
                    return store[key]
            value = compute()
            with self._guard:
                store[key] = value
            return value
