import asyncio
import json
from contextlib import asynccontextmanager

from app.database import SessionLocal
from app.models import AuditLog
from app.providers import registry
from app.providers.remote import RemoteMcpProvider


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return type("LTR", (), {"tools": [
            type("Tool", (), {"name": "customers_list", "description": "List customers."})(),
            type("Tool", (), {"name": "charges_list", "description": "List charges."})(),
        ]})()

    async def call_tool(self, name, arguments):
        return type("CTR", (), {
            "content": [type("Text", (), {"text": "[customers]"})()],
            "structuredContent": {"count": len(arguments or {})},
            "isError": False,
        })()


def _stub_session(monkeypatch):
    @asynccontextmanager
    async def fake_open_session(url):
        session = FakeSession()
        yield session

    monkeypatch.setattr("app.providers.remote.open_session", fake_open_session)


def test_remote_provider_listed_as_hosted(client):
    tools = registry.list_public_tools()
    hosted = [t for t in tools if t.get("hosted")]
    slugs = {t["slug"] for t in hosted}
    assert "stripe-mcp" in slugs
    stripe = next(t for t in hosted if t["slug"] == "stripe-mcp")
    assert stripe["remote_url"] == "https://mcp.stripe.com"
    assert stripe["auth_required"] is True


def test_remote_provider_lists_tools_cached(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="stripe-mcp", name="Stripe", remote_url="https://mcp.stripe.com",
        description="d", auth_required=False,
    )
    tools = provider.list_tools_cached()
    assert {t["name"] for t in tools} == {"customers_list", "charges_list"}


def test_remote_provider_allows_valid_https(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="stripe-mcp", name="Stripe", remote_url="https://mcp.stripe.com",
        description="d", auth_required=False,
    )
    out = provider.run({"tool": "customers_list", "arguments": {"x": 1}}, "public:read")
    assert out["ok"] is True
    assert out["result"]["tool"] == "customers_list"
    assert out["result"]["is_error"] is False
    assert out["result"]["content"] == ["[customers]"]


def test_remote_provider_refuses_ssrf_host(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="evil", name="Evil", remote_url="https://192.168.1.1/mcp",
        description="d", auth_required=False,
    )
    out = provider.run({"tool": "x", "arguments": {}}, "public:read")
    assert out["ok"] is False
    assert "allowlist" in out["error"].lower()


def test_remote_provider_refuses_non_https(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="http-remote", name="Http", remote_url="http://mcp.stripe.com",
        description="d", auth_required=False,
    )
    out = provider.run({"tool": "x", "arguments": {}}, "public:read")
    assert out["ok"] is False
    assert "https" in out["error"].lower()


def test_remote_provider_auth_required_refuses_execution(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="stripe-mcp", name="Stripe", remote_url="https://mcp.stripe.com",
        description="d", auth_required=True,
    )
    out = provider.run({"tool": "customers_list", "arguments": {}}, "public:read")
    assert out["ok"] is False
    assert "Phase 4.5" in out["error"]


def test_remote_call_through_gateway_audited(client, monkeypatch):
    _stub_session(monkeypatch)
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "stripe-mcp",
        "arguments": {"tool": "customers_list", "arguments": {}},
    })
    # auth_required -> refused, audited as failed (decision=failed)
    assert resp.status_code == 502
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.event == "tool_executed").all()
        hits = []
        for row in rows:
            detail = json.loads(row.detail_json or "{}")
            if detail.get("provider") == "stripe-mcp":
                hits.append(detail.get("decision"))
    finally:
        db.close()
    assert "failed" in hits


def test_remote_call_missing_tool_argument(client):
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "stripe-mcp",
        "arguments": {"arguments": {}},
    })
    assert resp.status_code == 502
    assert "tool" in resp.json()["detail"].lower()