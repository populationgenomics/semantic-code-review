"""Reviewer comments: model, storage, markdown formatter."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


CommentSource = Literal["local", "github"]


class ReadOnlyCommentError(Exception):
    """Raised by CommentStore when the caller tries to mutate a comment
    that wasn't authored in this run (e.g. an ingested PR comment)."""


class Comment(BaseModel):
    id: str
    file: str
    side: str = Field(pattern=r"^(old|new)$")
    line: int
    body: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    # Provenance + threading. All optional; absent on session-local comments
    # so the on-disk format stays backwards-compatible with older runs.
    source: CommentSource = "local"
    author: str | None = None
    author_avatar_url: str | None = None
    in_reply_to_id: str | None = None
    # The commit SHA the comment was anchored to upstream. May not match the
    # run's head_sha if upstream advanced after the comment was left — the
    # viewer surfaces the comment at (file, side, line) regardless.
    commit_id: str | None = None
    html_url: str | None = None
    # GitHub-rendered body. When present the viewer prefers it over `body`
    # so we don't ship a markdown parser to the client.
    body_html: str | None = None

    @property
    def is_writable(self) -> bool:
        """True iff this run owns the comment — i.e. the server may
        mutate or delete it. Ingested comments stay read-only."""
        return self.source == "local"


class CommentStore:
    """Thread-safe in-memory store with atomic flush to disk.

    The HTTP server hands every request through ``upsert`` / ``delete``;
    neither call returns until the backing file has been written. Callers
    on the CLI side read the file directly when the server has exited.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, Comment] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for d in data.get("comments", []):
                    c = Comment.model_validate(d)
                    self._items[c.id] = c
            except (OSError, ValueError):
                pass

    def upsert(self, payload: dict[str, Any]) -> Comment:
        with self._lock:
            now = time.time()
            existing = self._items.get(payload.get("id", ""))
            if existing is not None and not existing.is_writable:
                raise ReadOnlyCommentError(
                    f"comment {existing.id} is from {existing.source}; not editable"
                )
            if existing is None:
                c = Comment.model_validate(payload)
                c.created_at = payload.get("created_at", now)
                c.updated_at = now
                # Ignore any source claim on the wire — newly-authored
                # comments are always local.
                c.source = "local"
            else:
                data = existing.model_dump()
                data.update({k: v for k, v in payload.items() if k in {"body", "line", "side", "file"}})
                data["updated_at"] = now
                c = Comment.model_validate(data)
            self._items[c.id] = c
            self._flush_locked()
            return c

    def delete(self, comment_id: str) -> bool:
        with self._lock:
            existing = self._items.get(comment_id)
            if existing is None:
                return False
            if not existing.is_writable:
                raise ReadOnlyCommentError(
                    f"comment {existing.id} is from {existing.source}; not deletable"
                )
            del self._items[comment_id]
            self._flush_locked()
            return True

    def all(self) -> list[Comment]:
        with self._lock:
            return sorted(
                self._items.values(),
                key=lambda c: (c.file, c.line, c.created_at),
            )

    # --- internal --------------------------------------------------------

    def _flush_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "comments": [c.model_dump() for c in
                         sorted(self._items.values(),
                                key=lambda c: (c.file, c.line, c.created_at))],
        }
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def format_markdown(comments: list[Comment], *, run_slug: str = "") -> str:
    """Produce the stdout markdown dump the slash command feeds back in."""
    if not comments:
        header = f"# Review comments for {run_slug}" if run_slug else "# Review comments"
        return f"{header}\n\n_No comments left. The reviewer had no concerns._\n"

    out: list[str] = []
    if run_slug:
        out.append(f"# Review comments for {run_slug}")
    else:
        out.append("# Review comments")
    out.append("")
    for c in comments:
        header = f"## {c.file}:{c.line} ({c.side})"
        if c.author and c.source != "local":
            header += f" — @{c.author}"
        out.append(header)
        for line in c.body.splitlines() or [""]:
            out.append(f"> {line}" if line else ">")
        out.append("")
    total = len(comments)
    word = "comment" if total == 1 else "comments"
    out.append(f"_{total} {word} total._")
    return "\n".join(out) + "\n"
