"""Long-lived HTTP MCP host over `RepoTools` (ADR 0003 Slice 3).

Serves the `RepoTools` tool surface over MCP's Streamable-HTTP transport
so every `claude -p` in a run connects to one warm server instead of
spawning a stdio child per turn — killing the ~765 ms/spawn interpreter
cold start measured in Slice 0. Localhost-bound, bearer-token auth.

Each tool call fires an in-process `on_tool` callback, so the console can
publish `console-tool` activity directly (this is the retired Slice 2
back-channel, folded in). The tool surface is the same introspection-
derived set the stdio server used — `tools.mcp_tool_schemas()` for
`tools/list`, `tools.mcp_dispatch()` for `tools/call` — so the two
transports never diverge.

Runs its own uvicorn event loop on a daemon thread: the review server is
stdlib-threaded with no shared loop, and the host must stay warm across
the whole session (augment's per-hunk spawns, then console turns),
outliving any single `asyncio.run` scope. Sync tool calls are offloaded
to a worker thread so a slow `git show` never stalls other clients'
sessions; the shared `SourceCache` is thread-safe for exactly this.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import threading
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any, Self

import anyio.to_thread
import mcp.server.lowlevel
import mcp.server.streamable_http_manager
import mcp.types
import starlette.applications
import starlette.middleware
import starlette.responses
import starlette.routing
import starlette.types
import uvicorn

from . import tools

log = logging.getLogger(__name__)

MOUNT_PATH = "/mcp"

# Called with the tool name and its arguments the instant a `tools/call`
# arrives, before dispatch — the console turns it into a `console-tool` frame.
OnToolCall = Callable[[str, dict[str, Any]], None]

_STARTUP_TIMEOUT_S = 10.0


class McpHttpHost:
    """One warm Streamable-HTTP MCP server bound to a `RepoTools`.

    Start it once per run (or console session); every `claude -p` points
    at `url` with the bearer `token`. Not reusable after `stop()` — build
    a fresh host per run so it never outlives its worktrees.
    """

    def __init__(
        self,
        repo_tools: tools.RepoTools,
        *,
        on_tool: OnToolCall | None = None,
        name: str = "scr",
    ) -> None:
        self._repo_tools = repo_tools
        self._on_tool = on_tool
        self._name = name
        self._token = secrets.token_urlsafe(32)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("MCP host not started")
        return self._url

    def mcp_config(self) -> dict[str, Any]:
        """The `--mcp-config` server entry `claude -p` connects through."""
        return {
            "type": "http",
            "url": self.url,
            "headers": {"Authorization": f"Bearer {self._token}"},
        }

    def start(self) -> None:
        """Bind the socket and serve on a daemon thread; return once ready."""
        if self._server is not None:
            raise RuntimeError("MCP host already started")
        config = uvicorn.Config(
            self._build_app(),
            host="127.0.0.1",
            port=0,  # ephemeral — read the real port back after bind
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="scr-mcp-host", daemon=True)
        self._thread.start()

        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("MCP host thread died during startup")
            if time.monotonic() > deadline:
                raise RuntimeError("MCP host did not start within timeout")
            time.sleep(0.01)

        host, port = self._server.servers[0].sockets[0].getsockname()[:2]
        self._url = f"http://{host}:{port}{MOUNT_PATH}"
        log.info("MCP host serving at %s", self._url)

    def stop(self) -> None:
        """Signal shutdown and join the serving thread. Idempotent."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self._url = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _build_app(self) -> starlette.applications.Starlette:
        server: mcp.server.lowlevel.Server = mcp.server.lowlevel.Server(self._name)

        @server.list_tools()
        async def _list_tools() -> list[mcp.types.Tool]:
            return [
                mcp.types.Tool(
                    name=schema["name"],
                    description=schema["description"],
                    inputSchema=schema["inputSchema"],
                )
                for schema in tools.mcp_tool_schemas()
            ]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mcp.types.TextContent]:
            if self._on_tool is not None:
                self._on_tool(name, arguments)
            text = await anyio.to_thread.run_sync(tools.mcp_dispatch, self._repo_tools, name, arguments)
            return [mcp.types.TextContent(type="text", text=text)]

        manager = mcp.server.streamable_http_manager.StreamableHTTPSessionManager(app=server)

        async def _handle(
            scope: starlette.types.Scope, receive: starlette.types.Receive, send: starlette.types.Send
        ) -> None:
            await manager.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def _lifespan(_app: starlette.applications.Starlette) -> AsyncGenerator[None]:
            async with manager.run():
                yield

        # Mount at root, not at MOUNT_PATH: a path-mount 307-redirects the
        # slashless form (`/mcp` → `/mcp/`), and MCP clients (claude included)
        # don't reliably follow it. The manager routes on HTTP method + session
        # header, never on path, so the advertised MOUNT_PATH is cosmetic and
        # every request under root reaches it directly.
        return starlette.applications.Starlette(
            routes=[starlette.routing.Mount("/", app=_handle)],
            middleware=[starlette.middleware.Middleware(_BearerAuthMiddleware, token=self._token)],
            lifespan=_lifespan,
        )


class _BearerAuthMiddleware:
    """Reject any request without `Authorization: Bearer <token>`.

    Raw ASGI so the check runs ahead of MCP session handling; the token is
    a per-host secret, so a constant-time compare guards against timing
    oracles even though this only ever binds to localhost.
    """

    def __init__(self, app: starlette.types.ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(
        self, scope: starlette.types.Scope, receive: starlette.types.Receive, send: starlette.types.Send
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or ())
        presented = headers.get(b"authorization", b"").decode("latin-1")
        if not secrets.compare_digest(presented, self._expected):
            await starlette.responses.PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)
