"""Content-addressed disk cache for LLM call results.

Keys combine: pass identifier + model id + prompt template version + a
SHA-256 of the concatenated serialized inputs. A change to any of those
yields a cache miss. See plan §3.6.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(os.environ.get("SCR_CACHE_DIR", str(Path.home() / ".cache" / "scr" / "v1")))


@dataclass(frozen=True)
class CacheKey:
    pass_name: str
    model: str
    prompt_version: str
    digest: str  # hex sha256

    def path_under(self, root: Path) -> Path:
        # Shard by first 2 hex chars so a cache with many entries doesn't
        # produce a single huge directory.
        return root / self.pass_name / self.model / self.prompt_version / self.digest[:2] / f"{self.digest}.json"


class CacheStore:
    def __init__(self, root: Path | None = None, prompt_version: str = "p1") -> None:
        self.root = root if root is not None else DEFAULT_ROOT
        self.prompt_version = prompt_version

    def key(self, pass_name: str, model: str, *inputs: str | bytes) -> CacheKey:
        h = hashlib.sha256()
        for chunk in inputs:
            h.update(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            h.update(b"\x1f")  # unit separator between fields
        return CacheKey(
            pass_name=pass_name,
            model=model,
            prompt_version=self.prompt_version,
            digest=h.hexdigest(),
        )

    def get(self, key: CacheKey) -> dict[str, Any] | None:
        path = key.path_under(self.root)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, key: CacheKey, request: Any, response: Any, tokens_in: int = 0, tokens_out: int = 0) -> None:
        entry = {
            "pass": key.pass_name,
            "model": key.model,
            "prompt_version": key.prompt_version,
            "request": request,
            "response": response,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "created_at": time.time(),
        }
        path = key.path_under(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def clear(self) -> None:
        """Remove everything under the cache root. Used in tests."""
        if not self.root.exists():
            return
        for dirpath, _, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(dirpath) / name).unlink()
            try:
                Path(dirpath).rmdir()
            except OSError:
                pass
