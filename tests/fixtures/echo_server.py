"""Minimal stdio MCP test server: exposes one tool (echo_message).

Used to exercise the Tier-3 stdio bridge without any network/npx dependency.
Run as: python tests/fixtures/echo_server.py  (spawned by stdio_client)
"""

import asyncio
import sys

from mcp.server.mcpserver import MCPServer


async def _echo(message: str = "") -> str:
    return f"echo:{message}"


async def main():
    server = MCPServer(name="echo-test", version="1.0.0")
    server.add_tool(
        _echo,
        name="echo_message",
        description="Echo a message.",
    )
    await server.run_stdio_async()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))