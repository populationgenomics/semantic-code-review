"""Settings shared by every review session, whatever sourced the diff.

`scr review` and `scr pr` differ only in where the diff comes from. Once
a [[run-directory]] exists, the LLM passes, the cache, the viewer server
and the change explainer are the same either way, so those settings live
here and each flow's options type holds one of these plus its own
fetch-side fields.

The split is settings vs collaborators: a value the user chose (model,
concurrency, port, globs) travels in the config; a constructed
object the flow hands to a callee — the `Client`, the `CacheStore`, the
`on_event` publisher — stays an explicit parameter. `no_cache` and
`cache_dir` are settings; the `CacheStore` built from them is not.
"""

from __future__ import annotations

import dataclasses
import pathlib

from .. import paths
from ..augment import agents
from ..augment import config as augment_config


@dataclasses.dataclass(frozen=True)
class ReviewConfig:
    """The half of a review's inputs that is independent of the diff source."""

    runs_root: pathlib.Path = dataclasses.field(default_factory=paths.default_runs_root)
    augment: bool = True
    model: str = augment_config.DEFAULT_MODEL
    concurrency: int = 8
    no_cache: bool = False
    cache_dir: pathlib.Path | None = None
    open_browser: bool = True
    port: int = 0
    #: Idle seconds, not a session lifetime — see `ReviewServer.wait_until_done`.
    timeout: int = 3600
    extra_review_prompt: str | None = None
    #: Preselected backend handle. None → `augment_run_dir` defaults to a
    #: `Client` for the Anthropic SDK path.
    client: agents.Client | None = None
    #: `--debug` / SCR_DEBUG: surface each CLI-backend subprocess spawn
    #: (raw argv + envelope) in the viewer's debug drawer.
    debug: bool = False
    skip_globs: tuple[str, ...] = ()
    #: Change explainer (ADR 0007). Opt-out: `[augment].explainer = false`
    #: turns it off. There is no flag to turn it on, because the reviewers
    #: who benefit most are the ones who never configured it, and
    #: generation is press-triggered so default-on costs nothing.
    explainer: bool = True
    #: House style for the explainer document: inline
    #: `[augment].explainer_prompt`, or the file named by
    #: `--explainer-prompt`. Appended to the guidance of the three
    #: explainer passes and nothing else — the per-hunk pass has no
    #: channel for it, so the intents a document's claims are checked
    #: against stay hermetic.
    explainer_prompt: str | None = None

    def for_augment(self) -> augment_config.AugmentConfig:
        """Project the augmentation pass's share of these settings."""
        return augment_config.AugmentConfig(
            model=self.model,
            concurrency=self.concurrency,
            skip_globs=self.skip_globs,
            extra_review_prompt=self.extra_review_prompt,
        )


__all__ = ["ReviewConfig"]
