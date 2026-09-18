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
from urllib.parse import urlencode, urlparse

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from app.config import get_settings
from app.providers.base import Provider, ProviderError


def _flatten(exc: BaseException) -> BaseException:
    """anyio/MCP wrap transport failures in nested ExceptionGroups; peel down to
    the innermost real cause so callers see e.g. `MCPError: ...` instead of the
    generic "unhandled errors in a TaskGroup"."""
    while isinstance(exc, BaseExceptionGroup):
        children = exc.exceptions
        exc = children[0] if children else exc
    return exc


@asynccontextmanager
async def open_session(remote_url: str, headers: dict[str, str] | None = None):
    """Async context manager yielding a connected MCP ClientSession.

    Tests stub this to avoid network I/O; production wires it to the real
    streamable-http transport. `headers` carries a user's credential (Phase 4).
    """
    settings = get_settings()
    client = create_mcp_http_client(
        headers=headers or None,
        timeout=settings.remote_mcp_timeout,
    )
    async with streamable_http_client(remote_url, http_client=client) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            yield session


class RemoteMcpProvider(Provider):
    """A provider backed by a hosted MCP server's streamable-http endpoint.

    arguments = {"tool": "<remote tool name>", "arguments": {...}}
    """

    def __init__(self, slug: str, name: str, remote_url: str, description: str,
                 category: str = "databases", auth_required: bool = True,
                 version: str = "1.0.0", auth_kind: str = "bearer",
                 auth_param: str | None = None, api_key_hint: str = ""):
        self.slug = slug
        self.name = name
        self.version = version
        self.description = description
        self.category = category
        self.remote_url = remote_url
        self.auth_required = auth_required
        self.scopes = {"public:read"}
        # Auth shape: "bearer" = `Authorization: Bearer <key>` (Stripe/Notion);
        # "query" = key rides as a URL query parameter (Browserbase).
        self.auth_kind = auth_kind
        self.auth_param = auth_param
        self.api_key_hint = api_key_hint
        self.tier = "tier2"

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

    @staticmethod
    def _auth_headers(
        credential: dict | None,
        remote_url: str,
        auth_kind: str = "bearer",
        auth_param: str | None = None,
    ) -> tuple[dict[str, str], str]:
        """Map a stored credential to (headers, url).

        - bearer auth -> Authorization header, URL unchanged
        - query auth -> the key appended to the URL as `auth_param`
          (Browserbase), no header
        - header auth -> the token in a named header (`auth_param`)
        Supports an explicit {"headers": {...}} credential form, or a token from
        access_token/api_key/token.
        """
        if not credential:
            return {}, remote_url
        headers: dict[str, str] = {}
        explicit = credential.get("headers")
        if isinstance(explicit, dict) and explicit:
            headers = {str(k): str(v) for k, v in explicit.items()}
        token = credential.get("access_token") or credential.get("api_key") or credential.get("token")
        if token:
            if auth_kind == "query" and auth_param:
                sep = "&" if "?" in remote_url else "?"
                url = f"{remote_url}{sep}{urlencode({auth_param: str(token)})}"
                return headers, url
            if auth_kind == "header" and auth_param:
                headers.setdefault(auth_param, str(token))
                return headers, remote_url
            headers.setdefault("Authorization", f"Bearer {token}")
        return headers, remote_url

    def _session_target(self, credential: dict | None) -> tuple[dict[str, str], str]:
        return self._auth_headers(credential, self.remote_url, self.auth_kind, self.auth_param)

    async def _async_list_tools(self, headers: dict[str, str] | None = None,
                                url: str | None = None) -> list:
        async with open_session(url or self.remote_url, headers=headers) as session:
            await session.initialize()
            res = await session.list_tools()
            return [
                {"name": t.name, "description": getattr(t, "description", "") or ""}
                for t in getattr(res, "tools", [])
            ]

    async def _async_call_tool(self, tool: str, tool_args: dict,
                               headers: dict[str, str] | None = None,
                               url: str | None = None) -> dict:
        async with open_session(url or self.remote_url, headers=headers) as session:
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

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        headers, url = self._session_target(credential)
        authenticated = bool(headers) or bool(credential) and url != self.remote_url
        if self.auth_required and not authenticated:
            raise ProviderError(
                "no credential connected for this provider — connect an "
                "account (or store an API key) before executing"
            )
        settings = get_settings()
        tool = args["tool"]
        tool_args = args["arguments"]
        try:
            result = asyncio.run(
                asyncio.wait_for(
                    self._async_call_tool(tool, tool_args, headers=headers, url=url),
                    timeout=settings.remote_mcp_timeout,
                )
            )
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"remote MCP call timed out: {exc}") from exc
        except BaseException as exc:  # incl. anyio ExceptionGroup from the SDK
            raise ProviderError(f"remote MCP call failed: {_flatten(exc)}") from exc
        return {
            "remote_url": self.remote_url,
            "tool": tool,
            "authenticated": authenticated,
            "source": self.name,
            **result,
        }

    @property
    def requires_auth(self) -> bool:
        return self.auth_required