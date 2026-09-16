"""Tier-3 stdio bridge: spawn local stdio MCP servers and proxy them through the
audited gateway as if they were hosted remotes.

This is the "run-stdio-ourselves" lane (as claude.ai does for Drive/GitHub/Slack):
the stdio server is a subprocess; the gateway dials its stdin/stdout with the
official MCP client over stdio rather than over HTTP.

Security model:
  - Only allowlisted server specs (command + args + env map) may be spawned;
    arbitrary user commands are refused.
  - Credentials come from the user's vault, injected as env vars per spec.
  - Runs inside the sandbox of the container (Docker) or WASM runtime (WASI) in
    production; the allowlist is enforced before any subprocess is created.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.client.stdio import StdioServerParameters, stdio_client

from app.config import get_settings
from app.providers.base import Provider, ProviderError

# Allowlisted stdio server specs. binfmt: "docker" (Docker container) or "wasi"
# (WASM via the MCP wasi runtime) or "local" (direct process — dev/test only).
# env maps a credential key to the env var the server expects
# (e.g. {"access_token": "GITHUB_TOKEN"}).
ALLOWLISTED_STDIO: list[dict] = [
    {
        "slug": "github-stdio",
        "name": "GitHub (stdio bridge)",
        "description": "Official GitHub stdio server bridged server-side into the audited gateway.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "binfmt": "local",
        "env": {"access_token": "GITHUB_PERSONAL_ACCESS_TOKEN", "api_key": "GITHUB_PERSONAL_ACCESS_TOKEN"},
        "credential_env": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "auth_required": True,
    },
    {
        "slug": "google-drive-stdio",
        "name": "Google Drive (stdio bridge)",
        "description": "Community Google Drive MCP server bridged server-side. Requires Drive OAuth tokens.",
        "command": "npx",
        "args": ["-y", "@googledrive-mcp/gdrive"],
        "binfmt": "local",
        "env": {"access_token": "GOOGLE_DRIVE_ACCESS_TOKEN"},
        "credential_env": "GOOGLE_DRIVE_ACCESS_TOKEN",
        "auth_required": True,
    },
]


def stdio_spec(slug: str) -> dict | None:
    for spec in ALLOWLISTED_STDIO:
        if spec["slug"] == slug:
            return spec
    return None


@dataclass
class _CmdSpec:
    command: str
    args: list[str]
    env: dict[str, str]


def _build_env(spec: dict, credential: dict | None) -> dict[str, str]:
    env = dict(os.environ)
    if credential and spec.get("env"):
        for cred_key, env_var in spec["env"].items():
            val = credential.get(cred_key)
            if val:
                env[env_var] = str(val)
    return env


def _resolve_command(spec: dict) -> _CmdSpec:
    command = spec["command"]
    if shutil.which(command) is None:
        # allow npx/uvx on Windows where PATH shims may be npm-embedded
        for candidate in ("npx.cmd", "uvx.exe", command):
            if shutil.which(candidate):
                command = candidate
                break
    return _CmdSpec(command=command, args=list(spec.get("args", [])),
                    env={} if spec.get("env") is None else {})


@asynccontextmanager
async def _stdio_session(spec: dict, credential: dict | None):
    settings = get_settings()
    cmd = _resolve_command(spec)
    env = _build_env(spec, credential)
    params = StdioServerParameters(
        command=cmd.command,
        args=cmd.args,
        env=env,
        cwd=os.getcwd(),
    )
    async with stdio_client(params) as (read, write):
        from mcp.client.session import ClientSession
        async with ClientSession(read, write) as session:
            yield session


class StdioMcpProvider(Provider):
    """Provider that runs an allowlisted stdio server and proxies call_tool."""

    version = "0.4.0"
    scopes = {"public:read"}

    def __init__(self, spec: dict):
        self.spec = spec
        self.slug: str = spec["slug"]
        self.name: str = spec["name"]
        self.category: str = spec.get("category", "bridge")
        self.auth_required: bool = spec.get("auth_required", True)
        self.description: str = spec["description"]

    @property
    def requires_auth(self) -> bool:
        return self.auth_required

    def validate(self, args: dict) -> dict:
        tool = args.get("tool") or args.get("name")
        if not tool:
            raise ProviderError("'tool' is required (the remote server's tool name)")
        return {"tool": tool, "arguments": args.get("arguments", {})}

    async def _async_list_tools(self, credential=None) -> list:
        async with _stdio_session(self.spec, credential) as session:
            await session.initialize()
            res = await session.list_tools()
            return [{"name": t.name, "description": getattr(t, "description", "") or ""}
                    for t in getattr(res, "tools", [])]

    def list_tools_cached(self, credential=None) -> list:
        cache = self.__dict__.setdefault("_list_tools_cache", (0.0, []))
        now = time.monotonic()
        if now - cache[0] < get_settings().remote_list_tools_ttl:
            return cache[1]
        try:
            tools = asyncio.run(self._async_list_tools(credential=credential))
            self._list_tools_cache = (now, tools)
            return tools
        except Exception:
            return cache[1]

    async def _async_call_tool(self, tool: str, tool_args: dict, credential=None) -> dict:
        async with _stdio_session(self.spec, credential) as session:
            await session.initialize()
            result = await session.call_tool(tool, tool_args)
        content = getattr(result, "content", None)
        structured = getattr(result, "structuredContent", None)
        is_error = bool(getattr(result, "isError", False))
        payload: dict = {"is_error": is_error}
        if isinstance(content, list):
            payload["content"] = [getattr(i, "text", None) or str(i) for i in content]
        elif content is not None:
            payload["content"] = [str(content)]
        else:
            payload["content"] = []
        if structured is not None:
            payload["structured_content"] = structured
        return payload

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        validated = self.validate(args)
        if self.auth_required and not (credential and any(
                credential.get(k) for k in self.spec.get("env", {}))):
            raise ProviderError(
                "no credential connected for this provider — connect an account "
                "(or store an API key) before executing"
            )
        settings = get_settings()
        try:
            result = asyncio.run(asyncio.wait_for(
                self._async_call_tool(validated["tool"], validated["arguments"], credential=credential),
                timeout=settings.remote_mcp_timeout,
            ))
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"stdio MCP call timed out: {exc}") from exc
        return {
            "bridge": "stdio",
            "tool": validated["tool"],
            "authenticated": bool(credential),
            "source": self.name,
            **result,
        }