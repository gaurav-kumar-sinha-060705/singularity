"""Tier-3 stdio bridge tests: spawn a real local MCP server and proxy it."""

import sys
from pathlib import Path

import pytest

from app.providers.stdio import StdioMcpProvider, stdio_spec

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "echo_server.py"


@pytest.fixture
def echo_spec():
    return {
        "slug": "echo-test",
        "name": "Echo (stdio fixture)",
        "description": "Local test server.",
        "command": sys.executable,
        "args": [str(_FIXTURE)],
        "binfmt": "local",
        "env": {"api_key": "ECHO_API_KEY"},
        "auth_required": True,
    }


def test_stdio_spec_allowlist():
    assert stdio_spec("github-stdio") is not None
    assert stdio_spec("does-not-exist") is None


def test_stdio_provider_validate_requires_tool(echo_spec):
    p = StdioMcpProvider(echo_spec)
    with pytest.raises(Exception) as exc:
        p.validate({})
    assert "tool" in str(exc.value)
    assert p.validate({"tool": "echo_message", "arguments": {"message": "hi"}}) == {
        "tool": "echo_message", "arguments": {"message": "hi"},
    }


def test_stdio_bridge_calls_real_server(echo_spec):
    p = StdioMcpProvider(echo_spec)
    out = p.run(
        {"tool": "echo_message", "arguments": {"message": "hello"}},
        "public:read",
        credential={"api_key": "test-key"},
    )
    assert out["ok"] is True, out.get("error")
    result = out["result"]
    assert result["bridge"] == "stdio"
    assert "echo:hello" in "".join(
        c.get("text", "") if isinstance(c, dict) else str(c)
        for c in (result["content"] if isinstance(result.get("content"), list) else [])
    ) or "echo:hello" in str(result)


def test_stdio_bridge_lists_tools(echo_spec):
    p = StdioMcpProvider(echo_spec)
    tools = p.list_tools_cached(credential={"api_key": "test-key"})
    names = [t["name"] for t in tools]
    assert "echo_message" in names


def test_stdio_bridge_requires_credential(monkeypatch, echo_spec):
    p = StdioMcpProvider(echo_spec)
    # force auth failure: env empty so no credential keys are present
    out = p.run({"tool": "echo_message", "arguments": {}}, "public:read", credential={"other": "x"})
    assert out["ok"] is False
    assert "no credential" in out["error"]