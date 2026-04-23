"""Reviewer comments: model, storage, markdown formatter."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Comment(BaseModel):
    id: str
    file: str
    side: str = Field(pattern=r"^(old|new)$")
    line: int
    body: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


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
            if existing is None:
                c = Comment.model_validate(payload)
                c.created_at = payload.get("created_at", now)
                c.updated_at = now
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
            existed = self._items.pop(comment_id, None) is not None
            if existed:
                self._flush_locked()
            return existed

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
        out.append(f"## {c.file}:{c.line} ({c.side})")
        for line in c.body.splitlines() or [""]:
            out.append(f"> {line}" if line else ">")
        out.append("")
    total = len(comments)
    word = "comment" if total == 1 else "comments"
    out.append(f"_{total} {word} total._")
    return "\n".join(out) + "\n"
