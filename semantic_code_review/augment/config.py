"""Settings for one augmentation pass.

The LLM-pass half of a review session's settings — what the overview,
per-hunk and extra-review passes need, with none of the viewer, server
or cache wiring. It lives in the augment layer so `augment_run_dir` can
name its own inputs without importing the review layer; the review
layer derives one of these from its own config
(`review.config.ReviewConfig.for_augment`).

Settings only: the `Client`, the `CacheStore` and the `on_event`
publisher are collaborators and stay explicit parameters.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class AugmentConfig:
    """Inputs to `augment_run_dir` that are pure configuration."""

    model: str = "claude-opus-4-7"
    concurrency: int = 8
    #: File globs to skip in the LLM passes (config `[augment].skip_globs`).
    skip_globs: tuple[str, ...] = ()
    #: Already-resolved prompt text for the extra-review pass. When set,
    #: each hunk gets a second LLM call with this as the system prompt and
    #: the returned line-anchored notes merge into `hunk.line_notes`.
    extra_review_prompt: str | None = None


__all__ = ["AugmentConfig"]
