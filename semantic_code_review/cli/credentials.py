"""Composable credential sources for `scr init`, and live model listing.

A *source* is one way to provide a backend's credential. Each backend
accepts a subset — determined by its type and whether it takes a key —
and the wizard offers only those. Detection (is a credential resolvable
right now?) is unified in `init_cmd._detect`; this module owns the
menu-facing catalogue and which sources a backend allows.
"""

from __future__ import annotations

import dataclasses

from ..config import BackendDef, BackendType


@dataclasses.dataclass(frozen=True)
class CredentialSource:
    """A credential-acquisition method. `id` is stable (dispatch key);
    `label` is the menu text, with `{var}` filled in for the key name.
    """

    id: str
    label: str


# The catalogue. `{var}` is substituted with the backend's key env name.
SOURCES: dict[str, CredentialSource] = {
    "env": CredentialSource("env", "I'll set ${var} myself (shell export)"),
    "dotenv": CredentialSource("dotenv", "Paste the key into a gitignored .env (chmod 0600)"),
    "config": CredentialSource("config", "Store the key in my user config (chmod 0600)"),
    "command": CredentialSource("command", "Fetch it from a command, e.g. `gh auth token`"),
    "vertex": CredentialSource("vertex", "Vertex AI via GOOGLE_CLOUD_PROJECT (ADC — no key)"),
    "none": CredentialSource("none", "No key needed"),
}


def allowed_source_ids(name: str, bdef: BackendDef, *, scope: str) -> list[str]:
    """The credential sources a backend accepts, in menu order.

    `config` (a literal key in the TOML) is offered only for `--scope=user`
    — a per-repo `.scr/config.toml` sits inside the repo, one `git add .`
    from a committed secret.
    """
    if bdef.type is BackendType.CLAUDE_CLI:
        return ["none"]
    # A keyless local endpoint (e.g. ollama) declares neither an env var
    # nor a fetch command.
    if not bdef.api_key_env and not bdef.api_key_command:
        return ["none"]
    ids = ["env", "dotenv"]
    if scope == "user":
        ids.append("config")
    ids.append("command")
    if name == "gemini-api":
        ids.append("vertex")
    return ids


def list_models(bdef: BackendDef, api_key: str | None) -> list[str] | None:
    """Best-effort list of model ids the credential can reach, or None.

    Returns None (caller falls back to free-text) on any failure, an
    unsupported backend, or a missing key. Doubles as a live credential
    check when it succeeds.
    """
    try:
        if bdef.type is BackendType.ANTHROPIC_SDK:
            return _list_anthropic(api_key)
        if bdef.type is BackendType.OPENAI_COMPAT:
            return _list_openai_compat(bdef, api_key)
        if bdef.type is BackendType.GOOGLE_SDK:
            return _list_google(api_key)
    except Exception:  # noqa: BLE001 — any failure degrades to free-text entry
        return None
    return None


def _list_anthropic(api_key: str | None) -> list[str] | None:
    if not api_key:
        return None
    import anthropic

    page = anthropic.Anthropic(api_key=api_key).models.list(limit=50)
    return sorted((m.id for m in page.data), reverse=True)


# Substrings that mark a model as not a chat/completion model, so the
# picker doesn't offer embeddings/audio/image models for a review task.
_NON_CHAT = ("embed", "whisper", "tts", "audio", "dall-e", "image", "moderation", "rerank")


def _list_openai_compat(bdef: BackendDef, api_key: str | None) -> list[str] | None:
    import openai

    client = openai.OpenAI(api_key=api_key or "x", base_url=bdef.base_url)
    ids = [m.id for m in client.models.list().data]
    chat = [i for i in ids if not any(tok in i.lower() for tok in _NON_CHAT)]
    return sorted(chat or ids)


def _list_google(api_key: str | None) -> list[str] | None:
    if not api_key:
        return None
    # Aliased import: `from google import genai` trips pyright on the
    # `google` namespace package (reportAttributeAccessIssue).
    import google.genai as genai  # noqa: PLR0402

    out: list[str] = []
    for m in genai.Client(api_key=api_key).models.list():
        methods = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
        if "generateContent" in methods:
            out.append((m.name or "").removeprefix("models/"))
    return sorted(n for n in out if n) or None
