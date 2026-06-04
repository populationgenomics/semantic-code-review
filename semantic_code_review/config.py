"""User-level config for scr.

Loads (and merges) two optional TOML files:

  - `~/.config/scr/config.toml` (or `$XDG_CONFIG_HOME/scr/config.toml`)
    — user-wide defaults.
  - `<repo>/.scr/config.toml` — per-repo overrides, found by walking
    up from cwd.

Per-repo wins on conflict. Both files are optional; their absence is
the same as an empty config.

Schema (all top-level fields optional):

    backend = "claude-api"     # default backend if --backend not passed

    [model]
    default = "claude-opus-4-7"     # global model fallback

    [backends.claude-api]           # override a builtin backend's model
    model = "claude-sonnet-4-7"

    [backends.groq]                 # add a new backend (openai-compat type)
    type = "openai-compat"
    base_url = "https://api.groq.com/openai/v1"
    api_key_env = "GROQ_API_KEY"
    model = "llama-3.3-70b-versatile"

    [env]
    GOOGLE_CLOUD_PROJECT = "aasgard-dev"
    GOOGLE_CLOUD_LOCATION = "global"

`[env]` entries get applied via `os.environ.setdefault(...)` so the
user's existing `.env` / shell exports always take precedence — useful
for non-secret defaults like GCP project/location.

DO NOT put API keys here. Config files leak too easily (accidental
commits, dotfile repos, screen-shares). Use `.env`, your shell's
startup, or a system keychain (future work).
"""

from __future__ import annotations

import os
import re
import tomllib
import typing
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from .paths import default_config_path, find_repo_config_path


class BackendType(str, Enum):
    """The handler family a backend dispatches to.

    Each value corresponds to one branch in the CLI's `_select_client`
    dispatch. Adding a new family means a new branch there; adding a
    new backend that reuses an existing family is just a new entry in
    a builtin table or a `[backends.<name>]` block in user config.
    """
    ANTHROPIC_SDK = "anthropic-sdk"
    CLAUDE_CLI = "claude-cli"
    GOOGLE_SDK = "google-sdk"
    GEMINI_CLI = "gemini-cli"
    OPENAI_COMPAT = "openai-compat"


@dataclass(frozen=True)
class BackendDef:
    """One entry in the merged backend table.

    Per-field docs live in `Annotated[..., "..."]` metadata so the
    template renderer can extract them and emit them as TOML comments
    above each line of `scr config edit --template <name>`. Read with
    `field_doc(name)` below.

    Credential resolution order is `api_key_env` → `api_key_command`
    → error. The command is invoked with no shell interpretation
    (argv list, not a string) and its stdout becomes the bearer; this
    is how `gh auth token` and `gcloud secrets versions access ...`
    get plugged in.
    """
    type: Annotated[
        BackendType,
        "Handler family — selects which dispatch branch in _select_client.",
    ]
    default_model: Annotated[
        str | None,
        "Model used when neither --model nor [model] default resolves.",
    ] = None
    base_url: Annotated[
        str | None,
        "Endpoint URL. Only meaningful for openai-compat backends.",
    ] = None
    api_key_env: Annotated[
        str | None,
        "Env var holding the bearer; tried first.",
    ] = None
    api_key_command: Annotated[
        tuple[str, ...] | None,
        (
            "Argv to fetch the bearer when env is unset. Either a list "
            "of strings or a single shell-quoted string (shlex-split). "
            "Run with no shell expansion."
        ),
    ] = None
    description: Annotated[
        str | None,
        "Free-form blurb shown at the top of `scr config edit --template` output.",
    ] = None


def field_doc(name: str) -> str:
    """Return the doc string attached to `BackendDef.<name>` via Annotated metadata."""
    hints = typing.get_type_hints(BackendDef, include_extras=True)
    annotated = hints.get(name)
    if annotated is None:
        return ""
    args = typing.get_args(annotated)
    return next((a for a in args[1:] if isinstance(a, str)), "")


# Code-side preset table. Users can override any entry's `model` (and
# any other field, for power users) via `[backends.<name>]` in their
# TOML, or add brand-new named backends.
#
# Multi-transport vendors keep a `-api` / `-cli` suffix because the
# vendor name alone is ambiguous; single-transport providers use the
# bare vendor name. All non-Anthropic / non-Google entries reach the
# provider via the OpenAI Chat Completions wire format.
BUILTIN_BACKENDS: dict[str, BackendDef] = {
    "claude-api": BackendDef(
        type=BackendType.ANTHROPIC_SDK,
        default_model="claude-opus-4-7",
        api_key_env="ANTHROPIC_API_KEY",
        description="Anthropic SDK (paid). Best annotation quality and prompt caching.",
    ),
    "claude-cli": BackendDef(
        type=BackendType.CLAUDE_CLI,
        default_model="claude-opus-4-7",
        description=(
            "`claude -p` subprocess — uses your Claude Code subscription, "
            "no API key needed. Same model + prompts + MCP-backed repo tools "
            "as the SDK path; slower (subprocess startup + subscription rate "
            "limits) and may silently demote to the fallback model on a "
            "rate-limited hunk call."
        ),
    ),
    "gemini-api": BackendDef(
        type=BackendType.GOOGLE_SDK,
        default_model="gemini-2.5-pro",
        api_key_env="GEMINI_API_KEY",
        description=(
            "Google SDK. Paid tier via API key; if GOOGLE_CLOUD_PROJECT "
            "is set, uses Vertex AI via Application Default Credentials."
        ),
    ),
    "gemini-cli": BackendDef(
        type=BackendType.GEMINI_CLI,
        default_model="gemini-2.5-pro",
        description=(
            "`gemini -p` subprocess. Free tier via `gemini auth login`. "
            "Install: `npm install -g @google/gemini-cli`."
        ),
    ),
    "groq": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        description="Free tier with generous daily token quota; tool use works.",
    ),
    "github": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://models.github.ai/inference",
        api_key_env="GITHUB_TOKEN",
        api_key_command=("gh", "auth", "token"),
        default_model="openai/gpt-4o-mini",
        description=(
            "Free quota for any GitHub account. Falls back to `gh auth "
            "token` automatically. Model ids need a publisher prefix "
            "(`openai/...`)."
        ),
    ),
    "cerebras": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        description=(
            "Free tier; very fast inference. Pass --model — Cerebras' "
            "catalogue rotates so we don't pin a default."
        ),
    ),
    "openrouter": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        description=(
            "Hundreds of models including free-tier variants; pass "
            "--model (e.g. `meta-llama/llama-3.3-70b-instruct:free`)."
        ),
    ),
    "mistral": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        default_model="codestral-latest",
        description="La Plateforme free tier; Codestral is Mistral's code-tuned model.",
    ),
    "ollama": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="http://localhost:11434/v1",
        description=(
            "Local llama.cpp/Ollama; no credentials needed. Pass --model "
            "to name something you've pulled (e.g. `qwen2.5-coder:14b`)."
        ),
    ),
}


@dataclass
class ScrConfig:
    """Resolved config: user file + per-repo file merged."""

    backend: str | None = None
    backends: dict[str, BackendDef] = field(default_factory=dict)
    model_default: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Inline text for an *additional* per-hunk review prompt, run
    # alongside the main comprehension pass. Output: extra line_notes
    # the reviewer can promote to comments. Set under
    # [augment].extra_prompt = """...""" in the config file. None
    # disables. The CLI --extra-prompt flag loads from a file path
    # and overrides this for the current run.
    extra_review_prompt: str | None = None
    # Where each setting came from, for `scr config show`.
    sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        *,
        user_path: Path | None = None,
        repo_path: Path | None = None,
        cwd: Path | None = None,
    ) -> "ScrConfig":
        """Load and merge config files. Either path can be overridden for tests."""
        cfg = cls(backends=dict(BUILTIN_BACKENDS))

        user = user_path if user_path is not None else default_config_path()
        if user.is_file():
            cfg._merge(_parse(user), source=str(user))

        repo = repo_path if repo_path is not None else find_repo_config_path(cwd)
        if repo is not None and repo.is_file():
            cfg._merge(_parse(repo), source=str(repo))

        # Backend reference must point at a defined backend (or "auto").
        if cfg.backend is not None and cfg.backend != "auto" and cfg.backend not in cfg.backends:
            raise ConfigError(
                f"{cfg.sources.get('backend', '?')}: backend = {cfg.backend!r} "
                f"not one of {sorted(['auto', *cfg.backends.keys()])}"
            )

        return cfg

    def _merge(self, raw: dict[str, Any], *, source: str) -> None:
        backend = raw.get("backend")
        if isinstance(backend, str):
            self.backend = backend
            self.sources["backend"] = source

        # Legacy [model] table: `default` is the global fallback;
        # any other key is sugar for `[backends.<key>] model = ...`.
        model = raw.get("model")
        if isinstance(model, dict):
            for k, v in model.items():
                if not isinstance(v, str):
                    raise ConfigError(
                        f"{source}: model.{k!r} must be a string, got {type(v).__name__}"
                    )
                if k == "default":
                    self.model_default = v
                    self.sources["model.default"] = source
                else:
                    self._set_backend_model(k, v, source=source, source_key=f"model.{k}")

        # New-style [backends.<name>] table.
        backends = raw.get("backends")
        if isinstance(backends, dict):
            for name, body in backends.items():
                if not isinstance(body, dict):
                    raise ConfigError(
                        f"{source}: backends.{name!r} must be a table, got {type(body).__name__}"
                    )
                self._merge_backend(name, body, source=source)

        env = raw.get("env")
        if isinstance(env, dict):
            for k, v in env.items():
                if not isinstance(v, str):
                    raise ConfigError(
                        f"{source}: env.{k!r} must be a string, got {type(v).__name__}"
                    )
                self.env[k] = v
                self.sources[f"env.{k}"] = source

        augment = raw.get("augment")
        if isinstance(augment, dict):
            extra = augment.get("extra_prompt")
            if extra is not None:
                if not isinstance(extra, str):
                    raise ConfigError(
                        f"{source}: augment.extra_prompt must be a string, "
                        f"got {type(extra).__name__}"
                    )
                text = extra.strip()
                if text:
                    self.extra_review_prompt = text
                    self.sources["augment.extra_prompt"] = source

    def _merge_backend(self, name: str, body: dict[str, Any], *, source: str) -> None:
        existing = self.backends.get(name)
        type_raw = body.get("type")
        if type_raw is not None:
            if not isinstance(type_raw, str):
                raise ConfigError(
                    f"{source}: backends.{name}.type must be a string, "
                    f"got {type(type_raw).__name__}"
                )
            try:
                btype = BackendType(type_raw)
            except ValueError:
                valid = sorted(t.value for t in BackendType)
                raise ConfigError(
                    f"{source}: backends.{name}.type = {type_raw!r} "
                    f"not one of {valid}"
                ) from None
        elif existing is not None:
            btype = existing.type
        else:
            raise ConfigError(
                f"{source}: backends.{name} is new — `type` is required "
                f"(one of {sorted(t.value for t in BackendType)})"
            )

        merged = BackendDef(
            type=btype,
            default_model=_pick_str(body, "model", existing.default_model if existing else None),
            base_url=_pick_str(body, "base_url", existing.base_url if existing else None),
            api_key_env=_pick_str(body, "api_key_env", existing.api_key_env if existing else None),
            api_key_command=_pick_strs(
                body, "api_key_command",
                existing.api_key_command if existing else None,
            ),
            description=_pick_str(
                body, "description",
                existing.description if existing else None,
            ),
        )
        self.backends[name] = merged
        self.sources[f"backends.{name}"] = source
        for key in ("model", "base_url", "api_key_env", "api_key_command", "description"):
            if key in body:
                self.sources[f"backends.{name}.{key}"] = source

    def _set_backend_model(self, name: str, model: str, *, source: str, source_key: str) -> None:
        """Fold a legacy `[model][<name>]` entry into the backends table.

        If <name> isn't a builtin and hasn't been declared in
        `[backends.<name>]` yet, this raises — we can't infer a type.
        """
        existing = self.backends.get(name)
        if existing is None:
            raise ConfigError(
                f"{source}: model.{name!r} refers to an unknown backend; "
                f"declare it with [backends.{name}] first or use one of "
                f"{sorted(self.backends.keys())}"
            )
        self.backends[name] = replace(existing, default_model=model)
        self.sources[source_key] = source

    # -----------------------------------------------------------------
    # Resolution helpers (called from the CLI command bodies)
    # -----------------------------------------------------------------

    def apply_env(self, environ: dict[str, str] | None = None) -> None:
        """Set `[env]` entries on the process environment if not already present.

        `os.environ.setdefault` semantics: the user's shell exports and
        any `.env` they loaded earlier win. The config is the LAST
        fallback before built-in defaults.
        """
        env = environ if environ is not None else os.environ
        for k, v in self.env.items():
            env.setdefault(k, v)

    def resolve_backend(self, cli_value: str | None) -> str:
        """Pick the effective backend.

        Priority: CLI flag > config > "auto".
        """
        if cli_value is not None:
            return cli_value
        return self.backend or "auto"

    def resolve_model(self, *, backend: str, cli_value: str | None) -> str:
        """Pick the effective model.

        Priority: CLI flag > backend's resolved default_model >
        `[model] default` > "claude-opus-4-7" (the historical fallback).
        """
        if cli_value is not None:
            return cli_value
        bdef = self.backends.get(backend)
        if bdef is not None and bdef.default_model is not None:
            return bdef.default_model
        if self.model_default is not None:
            return self.model_default
        return "claude-opus-4-7"


class ConfigError(RuntimeError):
    """Raised when a config file is malformed."""


def _parse(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e


def _pick_str(body: dict[str, Any], key: str, fallback: str | None) -> str | None:
    if key not in body:
        return fallback
    v = body[key]
    if v is None:
        return None
    if not isinstance(v, str):
        raise ConfigError(f"backend field {key!r} must be a string, got {type(v).__name__}")
    return v


def _pick_strs(
    body: dict[str, Any], key: str, fallback: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """Read an argv-style field. Accepts either a list of strings or
    a single shell-quoted string (split via `shlex`). Same execution
    semantics either way — `subprocess.run(argv, shell=False)`. The
    string form is just a friendlier ergonomic for the common case.
    """
    if key not in body:
        return fallback
    v = body[key]
    if v is None:
        return None
    if isinstance(v, str):
        import shlex

        try:
            parts = shlex.split(v)
        except ValueError as e:
            raise ConfigError(
                f"backend field {key!r} has unbalanced quotes: {e}"
            ) from None
        if not parts:
            raise ConfigError(f"backend field {key!r} must not be empty")
        return tuple(parts)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        if not v:
            raise ConfigError(f"backend field {key!r} must not be empty")
        return tuple(v)
    raise ConfigError(
        f"backend field {key!r} must be a list of strings or a "
        f"shell-quoted string, got {type(v).__name__}"
    )


# --- inline-prompt round-trip --------------------------------------------
# Lives here (not in cli.py) because the regexes have to know our own
# write format and the read path is the same tomllib parser the rest of
# config loading uses. Keeping read+write next to each other in one
# module makes it harder for the two to drift.

# Anchors a triple-quoted string assignment for `extra_prompt`. The body
# matches the smallest run of characters between the opening and closing
# `"""`. Newlines inside are fine — `re.DOTALL` makes `.` match them too.
_RE_EXTRA_PROMPT_TRIPLE = re.compile(
    r'^extra_prompt\s*=\s*"""(?P<body>.*?)"""\s*\n?',
    re.MULTILINE | re.DOTALL,
)
# Single-line quoted form. Disjunct from the triple-quoted matcher so
# the triple form's body isn't confused with a single-quoted assignment.
_RE_EXTRA_PROMPT_QUOTED = re.compile(
    r'^extra_prompt\s*=\s*"(?P<body>[^"\n]*)"\s*\n?',
    re.MULTILINE,
)
_RE_AUGMENT_HEADER = re.compile(r"^\[augment\]\s*\n", re.MULTILINE)
_RE_ANY_SECTION = re.compile(r"^\[", re.MULTILINE)


def write_inline_extra_prompt(path: Path, new_body: str) -> None:
    '''Splice ``[augment].extra_prompt = """..."""`` into ``path``.

    Preserves all other sections + comments by editing the file as
    text rather than re-serialising through tomllib. ``new_body`` is
    the prompt content (no triple-quotes / no TOML escaping); we wrap
    it as a triple-quoted block here. An empty / whitespace-only body
    removes the assignment instead.

    Three cases:
    - No ``[augment]`` section yet → append one with the assignment.
    - ``[augment]`` present, no ``extra_prompt`` → insert the
      assignment at the top of the section.
    - ``extra_prompt`` already present → replace just that assignment.

    The file is created with a minimal header if it doesn't exist.
    '''
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    body = (new_body or "").strip()

    if not body:
        # Remove the assignment; leave the [augment] section header
        # in place even if it's now empty — a hand-edited section
        # might gain other keys later. tomllib treats an empty table
        # as no-op so this is safe.
        for rx in (_RE_EXTRA_PROMPT_TRIPLE, _RE_EXTRA_PROMPT_QUOTED):
            text = _replace_in_augment_section(text, rx, "")
        path.write_text(text, encoding="utf-8")
        return

    # The standard form we write. Leading + trailing newlines inside
    # the triple quotes so markdown bodies have breathing room and
    # the closing `"""` lands flush left rather than mid-line.
    new_block = f'extra_prompt = """\n{body}\n"""\n'

    # Replace an existing assignment if present.
    for rx in (_RE_EXTRA_PROMPT_TRIPLE, _RE_EXTRA_PROMPT_QUOTED):
        new_text = _replace_in_augment_section(text, rx, new_block)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            return
        text = new_text

    # No existing assignment. Insert under [augment] if it exists,
    # else append a fresh section.
    m = _RE_AUGMENT_HEADER.search(text)
    if m is not None:
        insert_at = m.end()
        text = text[:insert_at] + new_block + text[insert_at:]
    else:
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        if not text:
            sep = ""
        text = text + sep + "[augment]\n" + new_block
    path.write_text(text, encoding="utf-8")


def _replace_in_augment_section(text: str, rx: re.Pattern[str], replacement: str) -> str:
    """Run ``rx.sub(replacement, slice)`` against the body of the
    ``[augment]`` section only, leaving the rest of the file untouched.
    Returns the file unchanged when the section isn't present.
    """
    m = _RE_AUGMENT_HEADER.search(text)
    if m is None:
        return text
    start = m.end()
    nxt = _RE_ANY_SECTION.search(text, start)
    end = nxt.start() if nxt else len(text)
    section = text[start:end]
    new_section, n = rx.subn(replacement, section, count=1)
    if n == 0:
        return text
    return text[:start] + new_section + text[end:]


__all__ = [
    "BUILTIN_BACKENDS",
    "BackendDef",
    "BackendType",
    "ConfigError",
    "ScrConfig",
    "field_doc",
    "write_inline_extra_prompt",
]
