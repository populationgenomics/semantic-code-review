"""`ClaudeClient` implementation that shells out to `gemini -p`.

Used when no `ANTHROPIC_API_KEY` is set and the user explicitly opts
for the Gemini CLI backend (`--backend=gemini`). Each `create_message`
call spawns a `gemini -p` subprocess with our MCP server injected via
the `GEMINI_CLI_SYSTEM_SETTINGS_PATH` env var (gemini has no
`--mcp-config` flag), parses the structured `--output-format json`
envelope, validates the model's reply against the submit-tool schema,
and returns a dict shaped like `runner._message_to_dict` so the rest
of the pipeline is unchanged.

Differences from `ClaudeCLIClient` worth flagging:

- **No `--json-schema` flag.** Gemini's CLI doesn't surface
  `responseSchema`, so we embed the schema in the prompt and validate
  client-side. One retry on validation failure with the error fed
  back; second failure raises.
- **No `--strict-mcp-config`.** We get isolation by writing a temp
  `settings.json`, pointing `GEMINI_CLI_SYSTEM_SETTINGS_PATH` at it
  (highest layer in the precedence chain), and passing
  `--allowed-mcp-server-names scr` so only our server loads in-session.
- **`GEMINI_CLI_TRUST_WORKSPACE=true`** is set unconditionally;
  without it `gemini -p` refuses to run from "untrusted" folders, and
  the trust list lives in interactive state we can't pre-seed.

Auth is the user's responsibility: either `GEMINI_API_KEY` /
`GOOGLE_API_KEY` in the environment, or a previously-completed
interactive `gemini` OAuth login (creds cached at `~/.gemini/`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .claude_cli_client import (
    _flatten_system,
    _pick_submit_tool,
    _tail,
)
from .tools import RepoTools


log = logging.getLogger(__name__)


class GeminiCLINotFound(RuntimeError):
    pass


class GeminiCLIError(RuntimeError):
    """Non-zero exits, malformed envelopes, or persistent validation failure."""


class GeminiCLIClient:
    """Protocol impl over `gemini -p`. Single-shot, schema-prompted, validate+retry."""

    is_subprocess_backend = True

    def __init__(
        self,
        *,
        gemini_path: str | None = None,
        max_validation_retries: int = 1,
    ) -> None:
        resolved = gemini_path or shutil.which("gemini")
        if not resolved:
            raise GeminiCLINotFound("`gemini` not on PATH")
        self._gemini = resolved
        self._max_validation_retries = max_validation_retries
        self._repo_tools: RepoTools | None = None
        self._settings_path: Path | None = None

    def set_repo_tools(self, repo_tools: RepoTools | None) -> None:
        self._repo_tools = repo_tools
        self._unlink_settings()

    def _ensure_settings(self) -> Path:
        """Materialise the system-settings file gemini will read.

        Cached for the lifetime of the client (or until `set_repo_tools`
        invalidates it). We pin the file rather than the directory
        because `GEMINI_CLI_SYSTEM_SETTINGS_PATH` expects a file path —
        pointing it at a directory raises EISDIR.
        """
        if self._settings_path is not None and self._settings_path.exists():
            return self._settings_path
        assert self._repo_tools is not None
        rt = self._repo_tools
        import semantic_code_review as _pkg
        pkg_root = str(Path(_pkg.__file__).resolve().parent.parent)
        existing_pp = os.environ.get("PYTHONPATH", "")
        pythonpath = (
            f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
        )
        settings = {
            "mcpServers": {
                "scr": {
                    "command": sys.executable,
                    "args": [
                        "-m", "semantic_code_review.augment.mcp_server",
                        "--head-worktree", str(rt.head_worktree),
                        "--repo-git", str(rt.repo_git),
                        "--base-sha", rt.base_sha,
                        "--head-sha", rt.head_sha,
                    ],
                    "env": {"PYTHONPATH": pythonpath},
                }
            }
        }
        fd, path = tempfile.mkstemp(prefix="scr-gemini-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
        self._settings_path = Path(path)
        return self._settings_path

    def _unlink_settings(self) -> None:
        if self._settings_path is not None:
            try:
                self._settings_path.unlink()
            except OSError:
                pass
            self._settings_path = None

    async def aclose(self) -> None:
        # Pipeline calls this in a try/finally so the temp settings file
        # is removed deterministically at end-of-pass. Idempotent.
        self._unlink_settings()

    async def create_message(self, **kwargs: Any) -> dict:
        # Anthropic-shaped kwargs (`model`, `system`, `tools`, `messages`)
        # are coerced into a single Gemini prompt string. We don't honour
        # `model` directly — gemini selects from its own model catalog;
        # the kwarg's only used for the synthesized response shape.
        model: str = kwargs["model"]
        system_blocks: list[dict[str, Any]] = kwargs.get("system", []) or []
        tools: list[dict[str, Any]] = kwargs.get("tools", []) or []
        messages: list[dict[str, Any]] = kwargs.get("messages", []) or []

        submit_tool = _pick_submit_tool(tools)
        if submit_tool is None:
            raise GeminiCLIError(
                "GeminiCLIClient requires a `submit_*` tool in `tools` to "
                "drive schema-prompted output"
            )
        schema = submit_tool["input_schema"]
        system_text = _flatten_system(system_blocks)

        env = self._build_env()

        attempts: list[str] = []
        last_error: str | None = None
        for attempt in range(self._max_validation_retries + 1):
            prompt = _build_prompt(
                system_text=system_text,
                messages=messages,
                submit_tool=submit_tool,
                schema=schema,
                prior_error=last_error,
            )
            log.info(
                "gemini -p attempt=%d submit=%s prompt_chars=%d",
                attempt + 1, submit_tool["name"], len(prompt),
            )
            envelope = await self._invoke(argv=self._build_argv(prompt), env=env)
            response_text = envelope.get("response") or ""
            try:
                submit_input = _extract_json_object(response_text)
            except ValueError as e:
                last_error = f"could not extract JSON from response: {e}"
                attempts.append(_tail(response_text, 400))
                continue
            try:
                _validate_against_schema(submit_input, schema)
            except _SchemaValidationError as e:
                last_error = f"validation failed: {e}"
                attempts.append(json.dumps(submit_input)[:400])
                continue
            return _synthesize_tool_use_message(
                envelope=envelope,
                model=model,
                submit_tool_name=submit_tool["name"],
                submit_input=submit_input,
            )

        raise GeminiCLIError(
            f"gemini -p produced no schema-conformant response after "
            f"{self._max_validation_retries + 1} attempts. Last error: "
            f"{last_error!r}. Last attempt[:400]: "
            f"{attempts[-1] if attempts else '<none>'!r}"
        )

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        if self._repo_tools is not None:
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(self._ensure_settings())
        return env

    def _build_argv(self, prompt: str) -> list[str]:
        # NB: gemini's `-p` takes the prompt *as a CLI argument*, unlike
        # claude's `-p` which reads from stdin. Putting any other flag
        # immediately after `-p` makes gemini interpret that flag's name
        # as the prompt value and fail with "Not enough arguments".
        argv = [
            self._gemini, "-p", prompt,
            "--output-format", "json",
            "--skip-trust",  # belt-and-suspenders alongside GEMINI_CLI_TRUST_WORKSPACE
        ]
        if self._repo_tools is not None:
            argv += ["--allowed-mcp-server-names", "scr"]
        return argv

    async def _invoke(
        self,
        *,
        argv: list[str],
        env: dict[str, str],
    ) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        log.info(
            "gemini -p exit=%d stdout=%d stderr=%d",
            proc.returncode, len(stdout), len(stderr),
        )
        if stderr:
            log.debug("gemini -p stderr: %s", _tail(stderr.decode("utf-8", errors="replace"), 2000))

        envelope = _parse_envelope(stdout, stderr)
        # gemini exposes the failure via the `error` key in the envelope
        # AND/OR a non-zero exit code. Normalise both into a single raise.
        err = envelope.get("error")
        if err or proc.returncode != 0:
            err_msg = (
                err.get("message") if isinstance(err, dict) else str(err) if err else ""
            ) or _tail(stderr.decode("utf-8", errors="replace"))
            err_str = (err_msg or "").lower()
            if "auth" in err_str or "credentials" in err_str or "unauthenticated" in err_str:
                raise GeminiCLIError(
                    "gemini is not authenticated. Set GEMINI_API_KEY "
                    "(or GOOGLE_API_KEY for Vertex), or run `gemini` once "
                    "interactively to complete the OAuth flow."
                )
            raise GeminiCLIError(
                f"gemini -p exited {proc.returncode} with error: {err_msg or '<no message>'}"
            )
        return envelope


def _build_prompt(
    *,
    system_text: str,
    messages: list[dict[str, Any]],
    submit_tool: dict[str, Any],
    schema: dict[str, Any],
    prior_error: str | None,
) -> str:
    """Compose the single text prompt fed to `gemini -p`.

    Anthropic's tool-use API receives the schema out-of-band; gemini's
    CLI doesn't surface that, so we paste the schema in as a fenced
    code block with an explicit "no prose" instruction. On retry, we
    also include the prior validation error so the model has a chance
    to self-correct.
    """
    parts: list[str] = []
    if system_text:
        parts.append(f"# System\n{system_text}")
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            parts.append(f"# {role}\n{content}")
        elif isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    chunks.append(block.get("text", ""))
                elif t == "tool_use":
                    chunks.append(
                        f"[tool_use: {block.get('name')} "
                        f"input={json.dumps(block.get('input', {}), ensure_ascii=False)}]"
                    )
                elif t == "tool_result":
                    chunks.append(
                        f"[tool_result {block.get('tool_use_id','')}]:\n"
                        f"{block.get('content','')}"
                    )
            if chunks:
                parts.append(f"# {role}\n" + "\n\n".join(chunks))

    parts.append(
        "# Task\n"
        f"Reply with a single JSON object matching the schema for "
        f"`{submit_tool['name']}` below. Do not include any prose, "
        "explanations, or code fences in your reply — output the JSON "
        "object directly.\n\n"
        "## Schema\n```json\n"
        f"{json.dumps(schema, indent=2, ensure_ascii=False)}\n```"
    )
    if prior_error:
        parts.append(
            "# Previous attempt failed\n"
            f"{prior_error}\n\n"
            "Try again with a valid response that matches the schema exactly."
        )
    return "\n\n".join(parts)


def _parse_envelope(stdout: bytes, stderr: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise GeminiCLIError(
            "gemini -p produced empty stdout; stderr="
            f"{_tail(stderr.decode('utf-8', errors='replace'))}"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise GeminiCLIError(
            f"gemini -p envelope not JSON: {e}; stdout[:200]={text[:200]!r}"
        ) from e


def _extract_json_object(response_text: str) -> dict[str, Any]:
    """Pull a JSON object out of the model's reply.

    Tolerant of stray prose and code fences — we ask the model not to
    emit them, but instruction-following isn't perfect. Strategy: try
    a clean parse first; on failure, look for the outermost balanced
    `{...}` and parse that.
    """
    text = response_text.strip()
    if text.startswith("```"):
        # Strip a fenced block: ```json\n...\n``` or ```\n...\n```
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError(f"top-level value is {type(obj).__name__}, want object")
        return obj
    except json.JSONDecodeError:
        pass

    # Fallback: scan for the first balanced object.
    start = text.find("{")
    if start == -1:
        raise ValueError("no '{' found in response")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    break
                break
    raise ValueError("could not locate a balanced JSON object in response")


class _SchemaValidationError(ValueError):
    pass


def _validate_against_schema(value: Any, schema: dict[str, Any]) -> None:
    """Minimal JSON-Schema check: required-keys + top-level type.

    We deliberately keep this shallow — the downstream pydantic models
    in `augment/schemas.py` and the consumers in `hunks.py` /
    `overview.py` will catch the rest. The point of this layer is to
    distinguish "model produced the wrong shape" (worth retrying)
    from "model produced fine JSON but downstream tightened a field"
    (caller's problem).
    """
    if schema.get("type") == "object" and not isinstance(value, dict):
        raise _SchemaValidationError(f"top-level type must be object, got {type(value).__name__}")
    required = schema.get("required") or []
    missing = [k for k in required if k not in value]
    if missing:
        raise _SchemaValidationError(f"missing required keys: {missing}")


def _synthesize_tool_use_message(
    *,
    envelope: dict[str, Any],
    model: str,
    submit_tool_name: str,
    submit_input: dict[str, Any],
) -> dict[str, Any]:
    """Translate a gemini envelope into the Anthropic-shaped dict the
    runner expects.

    `stats.models` is a dict keyed by model ID (not a flat object) —
    one entry per model gemini actually invoked, which often includes
    a utility/router model in addition to the main one. We sum tokens
    across all of them so the reported usage matches what the user is
    actually billed for.

    Field mapping (gemini → Anthropic):
      tokens.input      → input_tokens
      tokens.candidates → output_tokens   (gemini calls outputs "candidates")
      tokens.cached     → cache_read_input_tokens
    Cache *creation* isn't surfaced by the CLI envelope, so it stays 0.
    """
    input_tokens = output_tokens = cache_read_tokens = 0
    stats = envelope.get("stats") or {}
    models = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(models, dict):
        for model_stats in models.values():
            if not isinstance(model_stats, dict):
                continue
            tokens = model_stats.get("tokens") or {}
            input_tokens += int(tokens.get("input", 0) or 0)
            output_tokens += int(tokens.get("candidates", 0) or 0)
            cache_read_tokens += int(tokens.get("cached", 0) or 0)
    return {
        "id": envelope.get("session_id", ""),
        "model": model,
        "role": "assistant",
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cache_read_tokens,
        },
        "content": [
            {
                "type": "tool_use",
                "id": f"scr-gemini-{uuid.uuid4().hex[:12]}",
                "name": submit_tool_name,
                "input": submit_input,
            },
        ],
    }


__all__ = [
    "GeminiCLIClient",
    "GeminiCLIError",
    "GeminiCLINotFound",
]
