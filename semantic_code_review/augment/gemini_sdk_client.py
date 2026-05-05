"""`ClaudeClient` implementation that talks directly to the Gemini API.

Used when the user explicitly opts in via `--backend=gemini-api`.
Mirrors `AnthropicClient`'s shape so `run_agentic` is unchanged: each
`create_message` call does one model round-trip and returns an
Anthropic-shaped dict with `content` blocks (text + tool_use), a
`stop_reason`, and `usage` (input/output/cache_read tokens).

Auth ladder:
    1. ``GOOGLE_CLOUD_PROJECT`` set → Vertex AI client
       (uses ADC; ``GOOGLE_CLOUD_LOCATION`` defaults to us-central1).
    2. ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``) set → AI Studio client.
    3. Neither → raise.

Translation strategy: Anthropic and Gemini both express tool use as
discriminated message parts, but the field names differ. We:

- map ``messages`` → ``contents``, including tool_use → ``function_call``
  and tool_result → ``function_response``;
- map ``tools`` → a single ``Tool(function_declarations=[...])``;
- map the response's ``content.parts`` (text / function_call) back into
  Anthropic-shaped ``content`` blocks, synthesising ``tool_use_id`` per
  call (Gemini doesn't issue one — Anthropic does);
- map ``usage_metadata`` → input/output/cache-read token counts.

Prompt caching: Gemini's *implicit* cache (rolling, no setup) shows up
in ``cached_content_token_count`` and is reported via
``cache_read_input_tokens``. *Explicit* CachedContent (the long-lived
cache you create via ``client.caches.create``) is not used here — the
augment pipeline's per-hunk prompts are well below Gemini's 32k-token
explicit-cache minimum, so it would be a no-op.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any


log = logging.getLogger(__name__)


# Mapping of Gemini's finish reasons to Anthropic's stop_reason vocabulary.
# `tool_use` is what `run_agentic` watches for to dispatch tool calls; we
# emit it whenever the response includes any function_call parts so the
# loop drives correctly regardless of whether finish_reason was STOP or
# something else. STOP without tool calls becomes `end_turn`.
_FINISH_REASON_MAP = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "stop_sequence",   # closest analogue; we don't have a refusal slot
    "RECITATION": "stop_sequence",
    "OTHER": "end_turn",
}


class GeminiSDKError(RuntimeError):
    """Wraps SDK exceptions in a stable type so backoff/log code can match."""


class GeminiSDKClient:
    """Async ClaudeClient implementation backed by ``google-genai``."""

    is_subprocess_backend = False

    def __init__(
        self,
        *,
        client: Any | None = None,
        location: str | None = None,
    ) -> None:
        # Lazy import keeps `google-genai` optional for users who only use
        # the Anthropic / CLI backends.
        from google import genai

        if client is not None:
            self._client = client
            return

        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

        if project:
            # Vertex AI path — uses ADC. `location` defaults to env or
            # us-central1; let the user override via $GOOGLE_CLOUD_LOCATION
            # which Vertex respects natively.
            loc = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
            self._client = genai.Client(vertexai=True, project=project, location=loc)
        elif api_key:
            # AI Studio path. `genai.Client()` with no args also reads
            # GEMINI_API_KEY from env, but we pass it explicitly so the
            # client doesn't accidentally pick up GOOGLE_GENAI_USE_VERTEXAI
            # config from elsewhere.
            self._client = genai.Client(api_key=api_key)
        else:
            raise GeminiSDKError(
                "GeminiSDKClient: no credentials. Set GEMINI_API_KEY "
                "(AI Studio), GOOGLE_API_KEY (Vertex), or "
                "GOOGLE_CLOUD_PROJECT + ADC."
            )

    async def create_message(self, **kwargs: Any) -> dict:
        """One model round-trip. Inputs are Anthropic-shaped; output is too."""
        from google.genai import types as gt

        model: str = kwargs["model"]
        anthropic_system = kwargs.get("system") or []
        anthropic_tools = kwargs.get("tools") or []
        anthropic_messages = kwargs.get("messages") or []
        max_tokens = kwargs.get("max_tokens")

        system_text = _flatten_system(anthropic_system)
        tools = _translate_tools(anthropic_tools)
        contents = _translate_messages(anthropic_messages)

        config = gt.GenerateContentConfig(
            system_instruction=system_text or None,
            tools=tools or None,
            max_output_tokens=max_tokens,
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:  # noqa: BLE001
            # Surface Gemini SDK exceptions in a way `run_agentic`'s
            # backoff predicate (which matches "rate"/"429"/"overloaded"/
            # "503" in the str) can pick up retryable cases.
            raise GeminiSDKError(str(e)) from e

        return _translate_response(response, model=model)

    async def aclose(self) -> None:
        # The SDK manages its own httpx lifecycle; no per-client temp files
        # or sockets to release here.
        return None


# ---------------------------------------------------------------------------
# Anthropic → Gemini translation
# ---------------------------------------------------------------------------

def _flatten_system(blocks: list[dict[str, Any]]) -> str:
    """Concatenate Anthropic system blocks into a single string.

    Cache-control markers on the blocks are dropped — Gemini's implicit
    caching doesn't take instructions; explicit CachedContent is a
    separate API we're not using here.
    """
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "\n\n".join(p for p in parts if p)


def _translate_tools(anthropic_tools: list[dict[str, Any]]) -> list[Any] | None:
    """Pack Anthropic tool defs into a single Gemini ``Tool``.

    Gemini groups all function declarations under one ``Tool`` object;
    Anthropic exposes each as a separate dict. Field rename:
    ``input_schema`` → ``parameters``.
    """
    if not anthropic_tools:
        return None
    from google.genai import types as gt

    decls: list[Any] = []
    for t in anthropic_tools:
        decls.append(gt.FunctionDeclaration(
            name=t["name"],
            description=t.get("description", ""),
            parameters=_clean_schema(t.get("input_schema") or {"type": "object"}),
        ))
    return [gt.Tool(function_declarations=decls)]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip JSON-Schema dialect bits Gemini's validator rejects.

    Gemini's tool-parameter validator is a strict subset of JSON Schema
    Draft 7-ish: it doesn't accept ``$schema``, ``$defs``/``definitions``
    inlined references, ``additionalProperties: false`` on every level,
    or ``allOf``/``anyOf``/``oneOf`` past one level deep. Pydantic emits
    these freely. We do the bare minimum cleanup here — drop ``$schema``
    and ``$defs`` at the top level; recurse into ``properties`` and
    ``items``. Anything more elaborate hits the schema as-is and may be
    rejected by Gemini at request time, which surfaces as GeminiSDKError.
    """
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in ("$schema", "$defs", "definitions")}
    if "properties" in out and isinstance(out["properties"], dict):
        out["properties"] = {k: _clean_schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _clean_schema(out["items"])
    return out


def _translate_messages(anthropic_messages: list[dict[str, Any]]) -> list[Any]:
    """Anthropic message list → Gemini ``Content`` list.

    Two role mappings:
        - ``user``      → ``user``
        - ``assistant`` → ``model``
    Tool results live on Anthropic ``user``-role messages; in Gemini
    they go on the same ``user`` content but as ``function_response``
    parts (no separate ``tool`` role).

    Anthropic ``tool_use_id`` doesn't survive the round-trip — Gemini
    matches function calls and responses positionally / by name. We
    look the name up from earlier assistant messages in the same list.
    """
    from google.genai import types as gt

    # Build tool_use_id -> name map up front; tool_results reference IDs
    # that were issued in earlier assistant tool_use blocks.
    tool_name_by_id: dict[str, str] = {}
    for m in anthropic_messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name_by_id[block.get("id", "")] = block.get("name", "")

    out: list[Any] = []
    for m in anthropic_messages:
        role = m.get("role", "user")
        gemini_role = "model" if role == "assistant" else "user"
        content = m.get("content")
        parts: list[Any] = []
        if isinstance(content, str):
            if content:
                parts.append(gt.Part(text=content))
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(gt.Part(text=text))
                elif t == "tool_use":
                    parts.append(gt.Part(function_call=gt.FunctionCall(
                        name=block.get("name", ""),
                        args=block.get("input", {}) or {},
                    )))
                elif t == "tool_result":
                    use_id = block.get("tool_use_id", "")
                    name = tool_name_by_id.get(use_id, "")
                    raw = block.get("content", "")
                    response_payload = (
                        {"output": raw} if isinstance(raw, str) else (raw or {})
                    )
                    parts.append(gt.Part(function_response=gt.FunctionResponse(
                        name=name,
                        response=response_payload,
                    )))
        if parts:
            out.append(gt.Content(role=gemini_role, parts=parts))
    return out


# ---------------------------------------------------------------------------
# Gemini → Anthropic translation
# ---------------------------------------------------------------------------

def _translate_response(response: Any, *, model: str) -> dict[str, Any]:
    """Translate ``GenerateContentResponse`` → Anthropic-shaped dict.

    Tool calls become ``tool_use`` blocks with synthesised IDs.
    ``stop_reason`` is forced to ``tool_use`` whenever any tool call is
    present (matching Anthropic behaviour) so ``run_agentic`` dispatches
    correctly.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return {
            "id": getattr(response, "response_id", "") or "",
            "model": model,
            "role": "assistant",
            "stop_reason": "end_turn",
            "usage": _extract_usage(response),
            "content": [],
        }

    cand = candidates[0]
    content_blocks: list[dict[str, Any]] = []
    has_tool_call = False
    cand_content = getattr(cand, "content", None)
    parts = getattr(cand_content, "parts", None) or []
    for part in parts:
        text = getattr(part, "text", None)
        fc = getattr(part, "function_call", None)
        if text:
            content_blocks.append({"type": "text", "text": text})
        elif fc is not None:
            has_tool_call = True
            args = getattr(fc, "args", None) or {}
            # Gemini's `args` may be a dict-like proto map; coerce to plain dict.
            if not isinstance(args, dict):
                try:
                    args = dict(args)
                except Exception:  # noqa: BLE001
                    args = {}
            content_blocks.append({
                "type": "tool_use",
                "id": f"gem-{uuid.uuid4().hex[:12]}",
                "name": getattr(fc, "name", "") or "",
                "input": args,
            })

    if has_tool_call:
        stop_reason = "tool_use"
    else:
        finish = getattr(cand, "finish_reason", None)
        # finish_reason can be a proto enum, a string, or None.
        finish_str = (
            finish.name if hasattr(finish, "name") else (str(finish) if finish else "")
        )
        stop_reason = _FINISH_REASON_MAP.get(finish_str, "end_turn")

    return {
        "id": getattr(response, "response_id", "") or "",
        "model": model,
        "role": "assistant",
        "stop_reason": stop_reason,
        "usage": _extract_usage(response),
        "content": content_blocks,
    }


def _extract_usage(response: Any) -> dict[str, int]:
    """Pull token counts out of ``usage_metadata``.

    Field map (Gemini → Anthropic):
        prompt_token_count          → input_tokens
        candidates_token_count      → output_tokens
        cached_content_token_count  → cache_read_input_tokens
    Cache *creation* isn't a separate counter on the implicit cache,
    so it stays 0. Tool-call thoughts are billed under
    ``thoughts_token_count`` on 2.5+ models — we don't surface that
    separately yet; it's included in the model's input bill.
    """
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    return {
        "input_tokens": int(getattr(meta, "prompt_token_count", 0) or 0),
        "output_tokens": int(getattr(meta, "candidates_token_count", 0) or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": int(getattr(meta, "cached_content_token_count", 0) or 0),
    }


__all__ = [
    "GeminiSDKClient",
    "GeminiSDKError",
]
