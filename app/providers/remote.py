"""Tier 2 — hosted MCP remotes (RemoteMcpProvider).

A hosted MCP server (streamable-http/SSE remote) is a *process reachable over
HTTP* — unlike stdio servers, which live on the user's machine and can never be
reached from a cloud gateway. Singularity proxies these by acting as an **MCP
client**: it connects to the vetted remote URL, lists its tools, and forwards
calls through the same scope + trust + audit gateway as Tier 1 first-party
providers.

Safety (non-negotiable):
  - connect ONLY to hostnames in `settings.remote_mcp_allowed_hosts`; any other
    URL — including arbitrary user-supplied ones — is refused (SSRF guard).
  - remote URLs come from a curated catalog, never from user input.
  - calls carry no secrets until Phase 4.5 (credential vault) supplies them;
    `requires_auth=True` providers are refused with an explicit reason.

The async MCP SDK is bridged to the sync Provider contract with asyncio.run();
the session factory (`open_session`) is module-level so tests can stub it.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import get_settings
from app.providers.base import Provider, ProviderError


@asynccontextmanager
async def open_session(remote_url: str):
    """Async context manager yielding a connected MCP ClientSession.

    Tests stub this to avoid network I/O; production wires it to the real
    streamable-http transport.
    """
    async with streamable_http_client(remote_url) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            yield session


class RemoteMcpProvider(Provider):
    """A provider backed by a hosted MCP server's streamable-http endpoint.

    arguments = {"tool": "<remote tool name>", "arguments": {...}}
    """

    def __init__(self, slug: str, name: str, remote_url: str, description: str,
                 category: str = "databases", auth_required: bool = True,
                 version: str = "1.0.0"):
        self.slug = slug
        self.name = name
        self.version = version
        self.description = description
        self.category = category
        self.remote_url = remote_url
        self.auth_required = auth_required
        self.scopes = {"public:read"}

    # -- validation -------------------------------------------------------

    def validate(self, args: dict) -> dict:
        host = urlparse(self.remote_url).hostname
        allowed = set(get_settings().remote_mcp_allowed_hosts.split(","))
        allowed = {h.strip() for h in allowed if h.strip()}
        if not host or host not in allowed:
            raise ProviderError(
                f"remote host '{host}' is not in the Singularity allowlist "
                "(SSRF guard blocks un-vetted endpoints)"
            )
        if not self.remote_url.startswith("https://"):
            raise ProviderError("remote MCP endpoints must use https://")

        tool = str(args.get("tool") or "").strip()
        if not tool:
            raise ProviderError("'tool' is required (the remote server's tool name)")
        if len(tool) > 200:
            raise ProviderError("tool name is too long")
        tool_args = args.get("arguments")
        if tool_args is None:
            tool_args = {}
        if not isinstance(tool_args, dict):
            raise ProviderError("'arguments' must be an object")
        return {"tool": tool, "arguments": tool_args}

    # -- remote session helpers ------------------------------------------

    async def _async_list_tools(self) -> list:
        async with open_session(self.remote_url) as session:
            await session.initialize()
            res = await session.list_tools()
            return [
                {"name": t.name, "description": getattr(t, "description", "") or ""}
                for t in getattr(res, "tools", [])
            ]

    async def _async_call_tool(self, tool: str, tool_args: dict) -> dict:
        async with open_session(self.remote_url) as session:
            await session.initialize()
            result = await session.call_tool(tool, tool_args)
        content = getattr(result, "content", None)
        structured = getattr(result, "structuredContent", None)
        is_error = bool(getattr(result, "isError", False))
        payload: dict = {"is_error": is_error}
        if isinstance(content, list):
            payload["content"] = [
                getattr(item, "text", None)
                or getattr(item, "structured_content", None)
                or str(item)
                for item in content
            ]
        elif content is not None:
            payload["content"] = [str(content)]
        else:
            payload["content"] = []
        if structured is not None:
            payload["structured_content"] = structured
        return payload

    # -- Provider contract ------------------------------------------------

    def list_tools_cached(self) -> list:
        """Remote tool list with a short TTL cache; network errors -> []."""
        settings = get_settings()
        cache = self.__dict__.setdefault("_list_tools_cache", (0.0, []))
        now = time.monotonic()
        if now - cache[0] < settings.remote_list_tools_ttl:
            return cache[1]
        try:
            tools = asyncio.run(
                asyncio.wait_for(self._async_list_tools(),
                                 timeout=settings.remote_mcp_timeout)
            )
            self._list_tools_cache = (now, tools)
            return tools
        except Exception:
            return cache[1]

    def execute(self, args: dict) -> dict:
        if self.auth_required:
            raise ProviderError(
                "hosted remote execution requires a credential connection "
                "(Phase 4.5 OAuth/credential vault — not built yet)"
            )
        settings = get_settings()
        tool = args["tool"]
        tool_args = args["arguments"]
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    self._async_call_tool(tool, tool_args),
                    timeout=settings.remote_mcp_timeout,
                )
            )
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"remote MCP call timed out: {exc}") from exc
        return {
            "remote_url": self.remote_url,
            "tool": tool,
            "source": self.name,
            **result,
        }

    @property
    def requires_auth(self) -> bool:
        return self.auth_required