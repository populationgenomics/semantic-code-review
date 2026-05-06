"""`pydantic_ai.Model` subclasses for the CLI subprocess backends.

These wrap `claude -p` / `gemini -p` directly: each `request()` spawns
a subprocess, parses its envelope, and returns a `ModelResponse` that
the Agent's loop sees as a single tool-call submission. The CLI
subprocess itself runs whatever multi-turn tool exploration the model
does — pydantic-ai's loop is degenerate for these models.

`function_tools` registered on the agent (the repo `read_file` /
`grep` etc. functions) reach the CLI subprocess out-of-band through
the MCP server we spawn via `--mcp-config` (claude) or
`GEMINI_CLI_SYSTEM_SETTINGS_PATH` (gemini); pydantic-ai itself
never sees the intermediate tool turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from .tools import RepoTools


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared message → flat-text translation
# ---------------------------------------------------------------------------

def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def _flatten_messages(messages: list[ModelMessage]) -> tuple[str, str]:
    """Translate pydantic-ai messages → (system_text, user_text).

    System prompts go to a separate channel (claude `--system-prompt`,
    gemini prompt header). Everything else — user prompts and any
    replayed tool calls / results from prior turns — gets flattened
    into a single `user_text` string. Both CLI subprocesses run
    single-shot per pydantic-ai request; the loop doesn't iterate.
    """
    system_chunks: list[str] = []
    user_chunks: list[str] = []

    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, SystemPromptPart):
                    system_chunks.append(part.content)
                elif isinstance(part, UserPromptPart):
                    content = part.content
                    if isinstance(content, str):
                        user_chunks.append(f"# user\n{content}")
                    else:
                        for item in content:
                            text = (
                                getattr(item, "text", None)
                                or (item if isinstance(item, str) else None)
                            )
                            if text:
                                user_chunks.append(f"# user\n{text}")
                elif isinstance(part, ToolReturnPart):
                    user_chunks.append(
                        f"# tool_result {part.tool_call_id}\n{_stringify(part.content)}"
                    )
                elif isinstance(part, RetryPromptPart):
                    user_chunks.append(
                        f"# retry {part.tool_call_id or ''}\n"
                        f"{_stringify(part.content)}"
                    )
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    user_chunks.append(f"# assistant\n{part.content}")
                elif isinstance(part, ToolCallPart):
                    user_chunks.append(
                        f"# assistant\n[tool_use: {part.tool_name} "
                        f"input={json.dumps(part.args_as_dict(), ensure_ascii=False)}]"
                    )

    return "\n\n".join(system_chunks), "\n\n".join(user_chunks)


def _output_tool(mrp: ModelRequestParameters) -> ToolDefinition:
    """The `output_type=ToolOutput(...)` tool the Agent expects us to call."""
    if not mrp.output_tools:
        raise RuntimeError(
            "CLI Model requires an output_type-driven Agent — "
            "model_request_parameters.output_tools is empty."
        )
    return mrp.output_tools[0]


def _instructions_to_system(mrp: ModelRequestParameters) -> str:
    """Concatenate `Agent(instructions=...)` parts into a system block.

    Pydantic-ai puts `Agent(instructions=...)` strings in
    `model_request_parameters.instruction_parts`, NOT as a
    `SystemPromptPart` in the messages — so a plain message walk
    misses them.
    """
    chunks: list[str] = []
    for ip in (mrp.instruction_parts or []):
        text = getattr(ip, "content", None) or getattr(ip, "text", None) or ""
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def _tail(text: str, n: int = 400) -> str:
    t = text.strip()
    if len(t) <= n:
        return t
    return "..." + t[-n:]


def _mcp_config_for(rt: RepoTools) -> dict[str, Any]:
    """Build the JSON the CLIs use to spawn our stdio MCP server.

    Both clients accept this shape (claude reads it via `--mcp-config`,
    gemini via `mcpServers` in `settings.json`). Ensures the child
    Python finds `semantic_code_review` even when the parent CLI was
    launched from an unrelated cwd.
    """
    import semantic_code_review as _pkg
    pkg_root = str(Path(_pkg.__file__).resolve().parent.parent)
    existing_pp = os.environ.get("PYTHONPATH", "")
    pythonpath = (
        f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
    )
    return {
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


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ClaudeCLINotFound(RuntimeError):
    pass


class ClaudeCLIError(RuntimeError):
    """Non-zero exits or malformed envelopes from `claude -p`.

    The string form includes common transient markers ("rate",
    "overloaded") when stderr suggests them, so retry predicates
    upstream can pick them up.
    """


class GeminiCLINotFound(RuntimeError):
    pass


class GeminiCLIError(RuntimeError):
    """Non-zero exits, malformed envelopes, or persistent validation failure."""


# ---------------------------------------------------------------------------
# ClaudeCLIModel — `claude -p` with --json-schema
# ---------------------------------------------------------------------------

class ClaudeCLIModel(Model):
    """`claude -p` subprocess as a pydantic-ai Model.

    Used when no `ANTHROPIC_API_KEY` is set but the `claude` CLI is on
    PATH (the user has a Claude Code subscription). Each `request()`
    call spawns a `claude -p` subprocess with `--json-schema` set to
    the output_type's JSON schema, parses the structured result, and
    returns a `ModelResponse` carrying a single `ToolCallPart` for the
    output tool.

    If `set_repo_tools()` has bound a `RepoTools`, a stdio MCP server
    is also injected via `--mcp-config`, so the model can explore the
    worktree during the call. Without it, the client runs single-shot
    and the model answers from the prompt alone.
    """

    is_subprocess_backend = True
    _provider = None  # type: ignore[assignment]

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        claude_path: str | None = None,
        fallback_model: str | None = "claude-sonnet-4-6",
        max_turns_single_shot: int = 3,
        max_turns_with_mcp: int = 20,
    ) -> None:
        super().__init__()
        resolved = claude_path or shutil.which("claude")
        if not resolved:
            raise ClaudeCLINotFound("`claude` not on PATH")
        self._model = model
        self._claude = resolved
        self._fallback_model = fallback_model
        self._max_turns_single_shot = max_turns_single_shot
        self._max_turns_with_mcp = max_turns_with_mcp
        self._repo_tools: RepoTools | None = None
        self._mcp_config_path: Path | None = None

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def system(self) -> str:
        return "claude-cli"

    def set_repo_tools(self, repo_tools: RepoTools | None) -> None:
        """Bind a `RepoTools` so subsequent calls get MCP-backed repo access.

        Called by the augment pipeline once it has resolved the run dir
        (head worktree + repo.git + SHAs). Calling with None disables
        MCP injection and reverts to single-shot mode.
        """
        self._repo_tools = repo_tools
        self._unlink_mcp_config()

    async def aclose(self) -> None:
        # Pipeline calls this in a try/finally so the temp config file
        # is removed deterministically at end-of-pass. Idempotent.
        self._unlink_mcp_config()

    def _ensure_mcp_config(self) -> Path:
        if self._mcp_config_path is not None and self._mcp_config_path.exists():
            return self._mcp_config_path
        assert self._repo_tools is not None
        # `claude -p` launches stdio MCP servers as subprocesses itself; it
        # owns stdin/stdout of the child.
        config = {
            "mcpServers": {
                "scr": {"type": "stdio", **_mcp_config_for(self._repo_tools)},
            }
        }
        fd, path = tempfile.mkstemp(prefix="scr-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        self._mcp_config_path = Path(path)
        return self._mcp_config_path

    def _unlink_mcp_config(self) -> None:
        if self._mcp_config_path is not None:
            try:
                self._mcp_config_path.unlink()
            except OSError:
                pass
            self._mcp_config_path = None

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        sys_text, user_text = _flatten_messages(messages)
        instructions = _instructions_to_system(model_request_parameters)
        system_text = "\n\n".join(x for x in (instructions, sys_text) if x)

        tool_def = _output_tool(model_request_parameters)
        schema_json = json.dumps(tool_def.parameters_json_schema, ensure_ascii=False)

        prompt = _build_claude_prompt(user_text, tool_def.name)

        mcp_active = self._repo_tools is not None
        max_turns = (
            self._max_turns_with_mcp if mcp_active else self._max_turns_single_shot
        )

        # NOTE: do NOT pass --bare here. Its docs are explicit:
        # "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper
        #  via --settings (OAuth and keychain are never read)."
        # Our entire reason to shell out to `claude -p` is that the user
        # lacks an API key but has OAuth/keychain credentials — --bare
        # would defeat the point and always return "Not logged in".
        # We pick the useful pieces of --bare individually below.
        argv = [
            self._claude, "-p",
            "--model", self._model,
            "--system-prompt", system_text,
            "--json-schema", schema_json,
            "--tools", "",
            "--no-session-persistence",
            "--setting-sources", "",
            "--permission-mode", "bypassPermissions",
            "--output-format", "json",
            "--max-turns", str(max_turns),
        ]
        if self._fallback_model:
            argv += ["--fallback-model", self._fallback_model]
        if mcp_active:
            argv += [
                "--mcp-config", str(self._ensure_mcp_config()),
                "--strict-mcp-config",
            ]

        log.info(
            "claude -p invoking: model=%s mcp=%s max_turns=%d submit=%s",
            self._model, mcp_active, max_turns, tool_def.name,
        )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(prompt.encode("utf-8"))
        log.info(
            "claude -p exit=%d stdout=%d stderr=%d",
            proc.returncode, len(stdout), len(stderr),
        )

        # `claude -p` frequently writes the real failure into the stdout
        # envelope (is_error=true, result=<message>) and exits non-zero
        # with empty stderr — e.g. when not logged in. So we parse stdout
        # first regardless of exit code.
        try:
            envelope = _parse_claude_envelope(stdout, stderr)
        except ClaudeCLIError:
            if proc.returncode != 0:
                raise ClaudeCLIError(
                    f"claude -p exited {proc.returncode}: "
                    f"{_tail(stderr.decode('utf-8', errors='replace')) or '<no stderr>'}"
                )
            raise

        if envelope.get("is_error") or proc.returncode != 0:
            message = (envelope.get("result") or "").strip()
            if "not logged in" in message.lower() or "please run /login" in message.lower():
                raise ClaudeCLIError(
                    "claude is not logged in; run `claude /login` (or "
                    "`claude setup-token` for a long-lived token) before "
                    "using --backend=cli."
                )
            raise ClaudeCLIError(
                f"claude -p returned error (exit {proc.returncode}, "
                f"stop_reason={envelope.get('stop_reason')!r}, "
                f"num_turns={envelope.get('num_turns')}): {message or '<empty>'}"
            )

        # When --json-schema is set, the validated object is returned in
        # `structured_output` (already parsed). `result` is empty in that
        # case. Fall back to parsing `result` as JSON for older `claude`
        # versions that emitted it there.
        structured = envelope.get("structured_output")
        if structured is None:
            result_text = envelope.get("result") or ""
            if not result_text:
                stop = envelope.get("stop_reason")
                turns = envelope.get("num_turns")
                raise ClaudeCLIError(
                    "claude -p returned no structured_output and no result "
                    f"(stop_reason={stop!r}, num_turns={turns}). This usually "
                    "means the model tried to call a tool instead of emitting "
                    "the JSON payload — bump --max-turns or reinforce the "
                    "no-tool instruction in the prompt."
                )
            try:
                structured = json.loads(result_text)
            except json.JSONDecodeError as e:
                stop = envelope.get("stop_reason")
                turns = envelope.get("num_turns")
                raise ClaudeCLIError(
                    f"claude -p result is not valid JSON (schema mode); "
                    f"stop_reason={stop!r} num_turns={turns}: {e}; "
                    f"result[:400]={result_text[:400]!r}"
                ) from e

        return _structured_to_response(
            structured,
            tool_name=tool_def.name,
            envelope=envelope,
            model_name=self._model,
            provider_name="claude-cli",
        )


def _build_claude_prompt(user_text: str, submit_tool_name: str) -> str:
    """The text fed via stdin to `claude -p`.

    Trailing instruction nudges the model to emit the JSON object
    directly without prose / fences. `--json-schema` constrains the
    shape; the prompt nudge avoids the "I'd like to call submit_X"
    detour some models still try.
    """
    parts = [user_text] if user_text else []
    parts.append(
        f"# Task\nReply with a single JSON object matching the schema for "
        f"`{submit_tool_name}`. Do not include any prose or code fences."
    )
    return "\n\n".join(parts)


def _parse_claude_envelope(stdout: bytes, stderr: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise ClaudeCLIError(
            f"claude -p produced empty stdout; stderr="
            f"{_tail(stderr.decode('utf-8', errors='replace'))}"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ClaudeCLIError(
            f"claude -p envelope not JSON: {e}; stdout[:200]={text[:200]!r}"
        ) from e


def _structured_to_response(
    structured: dict[str, Any],
    *,
    tool_name: str,
    envelope: dict[str, Any],
    model_name: str,
    provider_name: str,
) -> ModelResponse:
    usage_src = envelope.get("usage") or {}
    usage = RequestUsage(
        input_tokens=int(usage_src.get("input_tokens", 0) or 0),
        output_tokens=int(usage_src.get("output_tokens", 0) or 0),
        cache_write_tokens=int(usage_src.get("cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(usage_src.get("cache_read_input_tokens", 0) or 0),
    )
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name=tool_name,
                args=structured,
                tool_call_id=envelope.get("session_id", "") or "",
            ),
        ],
        usage=usage,
        model_name=model_name,
        provider_name=provider_name,
        finish_reason="tool_call",
    )


# ---------------------------------------------------------------------------
# GeminiCLIModel — `gemini -p` with prompt-embedded schema + validate/retry
# ---------------------------------------------------------------------------

class GeminiCLIModel(Model):
    """`gemini -p` subprocess as a pydantic-ai Model.

    Differences from `ClaudeCLIModel` worth flagging:

    - **No `--json-schema` flag.** Gemini's CLI doesn't surface
      `responseSchema`, so we embed the schema in the prompt and
      validate client-side. One retry on validation failure with the
      error fed back; second failure raises.
    - **`-p` takes the prompt as an argv value**, not via stdin. The
      argv slot immediately after `-p` *must* be the prompt or gemini
      bails with "Not enough arguments".
    - **MCP injection** uses a temp `settings.json` pointed at by
      `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (highest layer in the
      precedence chain) plus `--allowed-mcp-server-names scr`.
    - **`GEMINI_CLI_TRUST_WORKSPACE=true`** is set unconditionally;
      without it `gemini -p` refuses to run from "untrusted" folders.
    """

    is_subprocess_backend = True
    _provider = None  # type: ignore[assignment]

    def __init__(
        self,
        *,
        model: str = "gemini-2.5-pro",
        gemini_path: str | None = None,
        max_validation_retries: int = 1,
    ) -> None:
        super().__init__()
        resolved = gemini_path or shutil.which("gemini")
        if not resolved:
            raise GeminiCLINotFound("`gemini` not on PATH")
        self._model = model
        self._gemini = resolved
        self._max_validation_retries = max_validation_retries
        self._repo_tools: RepoTools | None = None
        self._settings_path: Path | None = None

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def system(self) -> str:
        return "gemini-cli"

    def set_repo_tools(self, repo_tools: RepoTools | None) -> None:
        self._repo_tools = repo_tools
        self._unlink_settings()

    async def aclose(self) -> None:
        self._unlink_settings()

    def _ensure_settings(self) -> Path:
        """Materialise the system-settings file gemini will read.

        We pin the file rather than the directory because
        `GEMINI_CLI_SYSTEM_SETTINGS_PATH` expects a file path —
        pointing it at a directory raises EISDIR.
        """
        if self._settings_path is not None and self._settings_path.exists():
            return self._settings_path
        assert self._repo_tools is not None
        settings = {"mcpServers": {"scr": _mcp_config_for(self._repo_tools)}}
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

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        sys_text, user_text = _flatten_messages(messages)
        instructions = _instructions_to_system(model_request_parameters)
        system_text = "\n\n".join(x for x in (instructions, sys_text) if x)

        tool_def = _output_tool(model_request_parameters)
        schema = tool_def.parameters_json_schema

        env = self._build_env()

        attempts: list[str] = []
        last_error: str | None = None
        for attempt in range(self._max_validation_retries + 1):
            prompt = _build_gemini_prompt(
                system_text=system_text,
                user_text=user_text,
                submit_tool_name=tool_def.name,
                schema=schema,
                prior_error=last_error,
            )
            log.info(
                "gemini -p attempt=%d submit=%s prompt_chars=%d",
                attempt + 1, tool_def.name, len(prompt),
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
            return _structured_to_response(
                submit_input,
                tool_name=tool_def.name,
                envelope=_synthesize_anthropic_usage(envelope),
                model_name=self._model,
                provider_name="gemini-cli",
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
            "--skip-trust",
        ]
        if self._repo_tools is not None:
            argv += ["--allowed-mcp-server-names", "scr"]
        return argv

    async def _invoke(self, *, argv: list[str], env: dict[str, str]) -> dict[str, Any]:
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

        envelope = _parse_gemini_envelope(stdout, stderr)
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
                f"gemini -p exited {proc.returncode} with error: "
                f"{err_msg or '<no message>'}"
            )
        return envelope


def _build_gemini_prompt(
    *,
    system_text: str,
    user_text: str,
    submit_tool_name: str,
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
    if user_text:
        parts.append(user_text)
    parts.append(
        "# Task\n"
        f"Reply with a single JSON object matching the schema for "
        f"`{submit_tool_name}` below. Do not include any prose, "
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


def _parse_gemini_envelope(stdout: bytes, stderr: bytes) -> dict[str, Any]:
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

    Shallow on purpose — pydantic-ai will validate the model's output
    against the full Pydantic schema once we hand it back. This layer
    just distinguishes "wrong shape, retry" from "fine JSON, downstream
    contract bug" so the retry path is bounded.
    """
    if schema.get("type") == "object" and not isinstance(value, dict):
        raise _SchemaValidationError(
            f"top-level type must be object, got {type(value).__name__}"
        )
    required = schema.get("required") or []
    missing = [k for k in required if k not in value]
    if missing:
        raise _SchemaValidationError(f"missing required keys: {missing}")


def _synthesize_anthropic_usage(gemini_envelope: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a gemini envelope's stats as anthropic-shaped usage.

    `_structured_to_response` reads the unified anthropic shape; we
    coerce here so that single helper handles both backends. Field map:
      tokens.input      → input_tokens
      tokens.candidates → output_tokens   (gemini calls outputs "candidates")
      tokens.cached     → cache_read_input_tokens
    `stats.models` is a dict keyed by model id (one entry per routed
    model), so we sum across all of them.
    """
    input_tokens = output_tokens = cache_read_tokens = 0
    stats = gemini_envelope.get("stats") or {}
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
        "session_id": gemini_envelope.get("session_id", ""),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cache_read_tokens,
        },
    }


__all__ = [
    "ClaudeCLIError",
    "ClaudeCLIModel",
    "ClaudeCLINotFound",
    "GeminiCLIError",
    "GeminiCLIModel",
    "GeminiCLINotFound",
]
