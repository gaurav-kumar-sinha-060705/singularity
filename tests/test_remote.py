import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oauth_helpers import issue_oauth_access_token  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import AuditLog  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.remote import RemoteMcpProvider  # noqa: E402


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
    captured = []

    @asynccontextmanager
    async def fake_open_session(url, headers=None):
        captured.append((url, headers))
        yield FakeSession()

    monkeypatch.setattr("app.providers.remote.open_session", fake_open_session)
    return captured


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
    assert "no credential connected" in out["error"]


def test_remote_provider_with_credential_authenticates(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="stripe-mcp", name="Stripe", remote_url="https://mcp.stripe.com",
        description="d", auth_required=True,
    )
    out = provider.run(
        {"tool": "customers_list", "arguments": {}},
        "public:read",
        credential={"api_key": "sk_test_123"},
    )
    assert out["ok"] is True
    assert out["result"]["authenticated"] is True


def test_browserbase_query_param_auth(monkeypatch):
    """Browserbase keys ride as a query param, never an Authorization header."""
    captured = _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="browserbase-mcp", name="Browserbase",
        remote_url="https://mcp.browserbase.com/mcp",
        description="d", auth_required=True,
        auth_kind="query", auth_param="browserbaseApiKey",
    )
    out = provider.run(
        {"tool": "navigate", "arguments": {"url": "https://example.com"}},
        "public:read",
        credential={"api_key": "bb_live_xyz"},
    )
    assert out["ok"] is True
    assert out["result"]["authenticated"] is True
    url, headers = captured[-1]
    assert "browserbaseApiKey=bb_live_xyz" in url
    assert not headers or "Authorization" not in headers


def test_bearer_auth_sends_header_without_query(monkeypatch):
    captured = _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="notion-mcp", name="Notion", remote_url="https://mcp.notion.com/mcp",
        description="d", auth_required=True,
    )
    out = provider.run(
        {"tool": "search", "arguments": {"query": "ship"}},
        "public:read",
        credential={"api_key": "secret_abc"},
    )
    assert out["ok"] is True
    url, headers = captured[-1]
    assert headers["Authorization"] == "Bearer secret_abc"
    assert "?" not in url


def test_browserbase_allowlisted(monkeypatch):
    _stub_session(monkeypatch)
    provider = RemoteMcpProvider(
        slug="browserbase-mcp", name="Browserbase",
        remote_url="https://mcp.browserbase.com/mcp",
        description="d", auth_required=True,
        auth_kind="query", auth_param="browserbaseApiKey",
    )
    assert provider.validate({"tool": "navigate", "arguments": {}}) is not None


def test_list_public_tools_tiers_and_hints():
    by_slug = {t["slug"]: t for t in registry.list_public_tools()}
    assert by_slug["github-mcp"]["tier"] == "tier1"
    assert by_slug["github-mcp"]["api_key_hint"].startswith("ghp_")
    assert by_slug["weather"]["tier"] == "tier1"
    assert by_slug["weather"]["requires_credential"] is False

    assert by_slug["stripe-mcp"]["tier"] == "tier2"
    assert by_slug["stripe-mcp"]["auth_kind"] == "bearer"
    assert by_slug["browserbase-mcp"]["tier"] == "tier2"
    assert by_slug["browserbase-mcp"]["auth_kind"] == "query"
    assert by_slug["browserbase-mcp"]["api_key_hint"] == "BROWSERBASE_API_KEY"
    assert by_slug["browserbase-mcp"]["remote_url"] == "https://mcp.browserbase.com/mcp"

    for slug in ("github-stdio", "google-drive-stdio"):
        assert by_slug[slug]["tier"] == "tier3"

    # Catalog-only tier-3 placeholders are NOT executable until built —
    # including the poisoned canary, which must never appear in the registry.
    for slug in ("postgres-mcp", "sentry-mcp", "quickledger-pro"):
        assert slug not in by_slug


def test_remote_call_through_gateway_audited(client, monkeypatch):
    _stub_session(monkeypatch)
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "stripe-mcp",
        "arguments": {"tool": "customers_list", "arguments": {}},
    })
    # no credential -> 401 with the authorization popup URL
    assert resp.status_code == 401
    assert "/authorize/stripe-mcp" in resp.json()["detail"]
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
    assert "auth_required" in hits


def test_remote_call_missing_tool_argument(client):
    resp = client.post("/api/v1/execute", json={
        "provider_slug": "stripe-mcp",
        "arguments": {"arguments": {}},
    })
    assert resp.status_code == 502
    assert "tool" in resp.json()["detail"].lower()


def test_end_to_end_user_credential_unlocks_remote(client, monkeypatch):
    """Signup -> store credential -> execute hosted remote through the gateway."""
    _stub_session(monkeypatch)
    email = f"e2e{int(__import__('time').time() * 1000000)}@example.com"
    tokens = client.post("/api/v1/auth/signup",
                         json={"email": email, "password": "hunter2hunter"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    conn = client.post("/api/v1/connections",
                       json={"provider_slug": "stripe-mcp", "credential": {"api_key": "sk_test_abc"}},
                       headers=headers)
    assert conn.status_code == 201

    resp = client.post("/api/v1/execute",
                       json={"provider_slug": "stripe-mcp",
                             "arguments": {"tool": "customers_list", "arguments": {}}},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["authenticated"] is True

    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.event == "tool_executed").all()
        hits = []
        for row in rows:
            detail = json.loads(row.detail_json or "{}")
            if detail.get("provider") == "stripe-mcp":
                hits.append(detail)
        assert any(h.get("authenticated") is True and h.get("user_id") for h in hits)
    finally:
        db.close()


def test_mcp_endpoint_authed_call_tool_uses_vault(client, monkeypatch):
    """OAuth access token on /mcp resolves user -> vault credential -> hosted remote."""
    _stub_session(monkeypatch)
    # 1) Create user via the REST auth endpoint (gets a JWT for /api/v1/*)
    import secrets as _secrets
    email = f"mcp-e2e{_secrets.token_hex(8)}@example.com"
    tokens = client.post("/api/v1/auth/signup",
                         json={"email": email, "password": "hunter2hunter"}).json()
    jwt_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # 2) Create connection with the JWT (the connections endpoint uses /api/v1 auth)
    conn = client.post("/api/v1/connections",
                       json={"provider_slug": "stripe-mcp", "credential": {"api_key": "sk_test_abc"}},
                       headers=jwt_headers)
    assert conn.status_code == 201, conn.text

    # 3) Get an MCP OAuth access token for the SAME email (reuse the user we just created)
    from oauth_helpers import issue_oauth_access_token
    mcp_token, _ = issue_oauth_access_token(client, email=email, password="hunter2hunter")
    auth = {"Authorization": f"Bearer {mcp_token}",
            "Accept": "application/json, text/event-stream"}

    def _rpc(method, params, id_):
        return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}

    init = client.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0.0.1"},
    }, 1), headers=auth)
    assert init.status_code == 200, init.text

    resp = client.post("/mcp", json=_rpc("tools/call", {
        "name": "call_tool",
        "arguments": {"provider_slug": "stripe-mcp",
                      "arguments": {"tool": "customers_list", "arguments": {}}},
    }, 2), headers=auth)
    assert resp.status_code == 200, resp.text

    def _parse(resp):
        body = resp.text
        if body.startswith("data:") or "\ndata:" in body:
            for line in body.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
        return resp.json()

    body = _parse(resp)
    content = body["result"]["content"]
    text = "".join(c.get("text", "") for c in content)
    assert "executed through the audited gateway" in text
    assert "authenticated:** True" in text