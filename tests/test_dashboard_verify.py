"""Tests for the dashboard page + the GET /api/v1/connections/{slug}/verify
endpoint that probes the hosted remote with the stored credential.
"""
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from app.database import SessionLocal
from app.models import AuditLog


# ── Fake remote session (same pattern as test_remote.py) ────────────────────

class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return type("LTR", (), {
            "tools": [
                type("Tool", (), {"name": "list_repos", "description": "List repos."})(),
                type("Tool", (), {"name": "get_contents", "description": "Get file contents."})(),
            ]
        })()

    async def call_tool(self, name, arguments):
        return type("CTR", (), {
            "content": [type("Text", (), {"text": "[repo data]"})()],
            "structuredContent": {},
            "isError": False,
        })()


def _stub_session(monkeypatch):
    @asynccontextmanager
    async def fake_open_session(url, headers=None):
        session = FakeSession()
        session._headers = headers
        yield session

    monkeypatch.setattr("app.providers.remote.open_session", fake_open_session)


# ── Dashboard page ──────────────────────────────────────────────────────────

def test_dashboard_returns_200(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Connect your tools" in resp.text
    assert "sg_token" in resp.text


def test_root_redirects_to_dashboard(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert urlparse(resp.headers["location"]).path == "/dashboard"


def test_connections_alias_redirects_to_dashboard(client):
    resp = client.get("/connections", follow_redirects=False)
    assert resp.status_code == 302
    assert urlparse(resp.headers["location"]).path == "/dashboard"


def test_authorize_page_serves_popup_with_slug(client):
    for slug in ("github-mcp", "slack-mcp", "notion-mcp", "stripe-mcp"):
        resp = client.get(f"/authorize/{slug}")
        assert resp.status_code == 200
        assert "Authentication" in resp.text
        assert slug in resp.text


def test_notion_mcp_call_without_credential_returns_authorize_popup(client, monkeypatch):
    """The exact flow the user hit: call notion-mcp with no credential -> the
    response points at the Approve popup instead of 'can't connect'."""
    _stub_session(monkeypatch)  # notion-mcp is a hosted remote
    email = f"authreq-{int(time.time() * 1e6)}@example.com"
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert r.status_code in (200, 201), r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/v1/execute",
                    json={"provider_slug": "notion-mcp",
                          "arguments": {"tool": "list_pages", "arguments": {}}},
                    headers=headers)
    assert r.status_code == 401, r.text
    detail = r.json()["detail"]
    assert "authentication required" in detail
    assert "/authorize/notion-mcp" in detail
    assert "click Approve" in detail


def test_authorize_popup_store_token_flow(client):
    """The popup's fallback path (no OAuth configured): storing a token through
    the same endpoint the page's Connect button posts to."""
    email = f"popup-{int(time.time() * 1e6)}@example.com"
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert r.status_code in (200, 201), r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "notion-mcp",
                          "credential": {"access_token": "secret_x"}},
                    headers=headers)
    assert r.status_code == 201, r.text
    conns = client.get("/api/v1/connections", headers=headers).json()
    assert any(c["provider_slug"] == "notion-mcp" for c in conns)


# ── Helper: signup + store a credential ─────────────────────────────────────

def _signup_and_store(client, slug="stripe-mcp", email=None, password="hunter2hunter"):
    email = email or f"verify-{int(time.time() * 1e6)}@example.com"
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    c = client.post(
        f"/api/v1/connections",
        json={"provider_slug": slug, "credential": {"api_key": "sk_test_abc"}},
        headers=headers,
    )
    assert c.status_code == 201, c.text
    return token


# ── Verify endpoint ─────────────────────────────────────────────────────────

def test_verify_success(client, monkeypatch):
    _stub_session(monkeypatch)
    token = _signup_and_store(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/v1/connections/stripe-mcp/verify", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verified"] is True
    assert body["provider_slug"] == "stripe-mcp"
    assert body["authenticated"] is True
    assert body["tool_count"] == 2
    assert "list_repos" in body["tools"]
    # audit row
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.event == "connection_verified").all()
        hits = [json.loads(r.detail_json or "{}") for r in rows if "stripe-mcp" in json.loads(r.detail_json or "{}").get("provider", "")]
        assert len(hits) >= 1
        assert hits[-1]["user_id"]
    finally:
        db.close()


def test_verify_no_connection(client, monkeypatch):
    _stub_session(monkeypatch)
    token = _signup_and_store(client, slug="notion-mcp")  # different slug
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/v1/connections/stripe-mcp/verify", headers=headers)
    assert resp.status_code == 400
    assert "no credentials" in resp.json()["detail"]


def test_verify_unknown_provider(client):
    token = _signup_and_store(client, slug="stripe-mcp")
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/v1/connections/fake-slug/verify", headers=headers)
    assert resp.status_code == 404


def test_verify_remote_fails(client, monkeypatch):
    @asynccontextmanager
    async def fail_session(url, headers=None):
        class FailSession:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
            async def initialize(s): return None
            async def list_tools(s):
                raise RuntimeError("connection refused")
        yield FailSession()

    monkeypatch.setattr("app.providers.remote.open_session", fail_session)
    token = _signup_and_store(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/v1/connections/stripe-mcp/verify", headers=headers)
    assert resp.status_code == 502
    assert "verification failed" in resp.json()["detail"].lower()


def test_verify_unauthenticated(client, monkeypatch):
    _stub_session(monkeypatch)
    resp = client.get("/api/v1/connections/stripe-mcp/verify")
    assert resp.status_code == 401


def test_verify_keyless_provider_501(client, monkeypatch):
    """Non-hosted providers (e.g. npms_lookup) return 501."""
    token = _signup_and_store(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/v1/connections",
        json={"provider_slug": "npms_lookup", "credential": {"api_key": "x"}},
        headers=headers,
    )
    resp = client.get("/api/v1/connections/npms_lookup/verify", headers=headers)
    assert resp.status_code == 501
    assert "hosted remote" in resp.json()["detail"].lower()
