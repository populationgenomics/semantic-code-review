"""Cache store: key stability, hit/miss, invalidation on any input change."""

from __future__ import annotations

from pathlib import Path

from semantic_code_review.cache.store import CacheStore


def test_key_stable_across_instances(tmp_path: Path) -> None:
    a = CacheStore(root=tmp_path / "a", prompt_version="p1")
    b = CacheStore(root=tmp_path / "b", prompt_version="p1")
    ka = a.key("hunk", "claude-x", "prompt", "hunk body")
    kb = b.key("hunk", "claude-x", "prompt", "hunk body")
    assert ka.digest == kb.digest


def test_key_changes_with_input(tmp_path: Path) -> None:
    s = CacheStore(root=tmp_path, prompt_version="p1")
    k1 = s.key("hunk", "claude-x", "prompt", "A")
    k2 = s.key("hunk", "claude-x", "prompt", "B")
    assert k1.digest != k2.digest


def test_key_changes_with_model(tmp_path: Path) -> None:
    s = CacheStore(root=tmp_path, prompt_version="p1")
    k1 = s.key("hunk", "claude-x", "prompt", "A")
    k2 = s.key("hunk", "claude-y", "prompt", "A")
    assert (k1.model, k1.digest) != (k2.model, k2.digest)
    assert k1.path_under(s.root) != k2.path_under(s.root)


def test_key_changes_with_prompt_version(tmp_path: Path) -> None:
    s1 = CacheStore(root=tmp_path, prompt_version="p1")
    s2 = CacheStore(root=tmp_path, prompt_version="p2")
    k1 = s1.key("hunk", "claude-x", "prompt", "A")
    k2 = s2.key("hunk", "claude-x", "prompt", "A")
    assert k1.path_under(s1.root) != k2.path_under(s2.root)


def test_put_then_get_round_trip(tmp_path: Path) -> None:
    s = CacheStore(root=tmp_path, prompt_version="p1")
    k = s.key("hunk", "claude-x", "prompt", "A")
    assert s.get(k) is None
    s.put(k, request={"prompt": "A"}, response={"intent": "ok"}, tokens_in=100, tokens_out=20)
    entry = s.get(k)
    assert entry is not None
    assert entry["response"]["intent"] == "ok"
    assert entry["tokens_in"] == 100


def test_field_order_matters(tmp_path: Path) -> None:
    """Concatenation order of inputs must affect the key."""
    s = CacheStore(root=tmp_path, prompt_version="p1")
    k1 = s.key("hunk", "claude-x", "A", "B")
    k2 = s.key("hunk", "claude-x", "B", "A")
    assert k1.digest != k2.digest


def test_path_is_sharded(tmp_path: Path) -> None:
    s = CacheStore(root=tmp_path, prompt_version="p1")
    k = s.key("hunk", "claude-x", "prompt", "A")
    p = k.path_under(s.root)
    # shard dir uses the first two hex chars
    assert p.parent.name == k.digest[:2]
