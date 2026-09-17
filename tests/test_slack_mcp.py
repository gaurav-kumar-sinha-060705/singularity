"""Tests for the first-party Slack REST provider (slack-mcp).

Replaces the WayStation-hosted remote (402 DEPLOYMENT_DISABLED, host-side). A
stored xoxb-/xoxp- token drives api.slack.com through the same audited gateway;
the dashboard verify endpoint probes auth.test with it.
"""
import time

import pytest

from app.providers import registry, slack_mcp
from app.providers.base import ProviderError

TOKEN = "xoxb-fake-token"
TOKEN_JSON = {"access_token": TOKEN}


def _branch_api(responses):
    """Fake slack_mcp._api keyed by method name."""
    def fake_api(method_name, token, payload=None):
        assert token == TOKEN
        if method_name not in responses:
            raise ProviderError(f"unexpected method {method_name}")
        return responses[method_name]
    return fake_api


# ── registry wiring ─────────────────────────────────────────────────────────

def test_slack_registered_and_not_a_remote():
    provider = registry.get_provider("slack-mcp")
    assert provider is not None
    assert isinstance(provider, slack_mcp.SlackMcpProvider)
    assert provider.requires_auth is True
    assert all(t["slug"] != "slack-mcp" or "hosted" not in t for t in registry.list_public_tools())


# ── validate ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"tool": "list_channels", "arguments": {}},
    {"action": "list_channels"},
    {"list_channels": {}},
])
def test_validate_accepts_all_shapes(args):
    out = slack_mcp.provider.validate(args)
    assert out == {"action": "list_channels", "params": {}}


def test_validate_bad_types():
    with pytest.raises(ProviderError):
        slack_mcp.provider.validate(42)


def test_validate_unknown_action():
    with pytest.raises(ProviderError, match="unknown Slack action"):
        slack_mcp.provider.validate({"action": "delete_channel"})


def test_validate_channel_required():
    with pytest.raises(ProviderError, match="channel is required"):
        slack_mcp.provider.validate({"action": "post_message", "text": "hi"})


def test_validate_text_required():
    with pytest.raises(ProviderError, match="text is required"):
        slack_mcp.provider.validate({"action": "post_message", "channel": "#dev"})


def test_validate_query_required():
    with pytest.raises(ProviderError, match="query is required"):
        slack_mcp.provider.validate({"action": "search_messages"})


# ── execute ─────────────────────────────────────────────────────────────────

def test_execute_requires_token():
    out = slack_mcp.provider.run({"action": "list_channels"}, "public:read", credential=None)
    assert out["ok"] is False
    assert "no Slack token" in out["error"]


def test_execute_get_workspace_info(monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({
        "auth.test": {"ok": True, "team": "Acme", "team_id": "T1",
                      "user": "bot1", "user_id": "U1", "url": "https://acme.slack.com"}}))
    out = slack_mcp.provider.run({"action": "get_workspace_info"}, "public:read",
                                 credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["team"] == "Acme"
    assert out["result"]["url"] == "https://acme.slack.com"


def test_execute_list_channels(monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({
        "conversations.list": {"ok": True, "channels": [
            {"id": "C1", "name": "general", "is_private": False, "is_member": True,
             "is_archived": False, "topic": {"value": "home"}}]}}))
    out = slack_mcp.provider.run({"action": "list_channels"}, "public:read",
                                 credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["count"] == 1
    assert out["result"]["channels"][0]["name"] == "general"


def test_execute_list_messages_resolves_channel(monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({
        "conversations.list": {"ok": True, "channels": [
            {"id": "C9", "name": "dev"}]},
        "conversations.history": {"ok": True, "messages": [
            {"type": "message", "ts": "1", "user": "U1", "text": "hello"},
            {"type": "message", "ts": "2", "user": "U1", "text": "world",
             "thread_ts": "1"}]}}))
    out = slack_mcp.provider.run({"action": "list_messages", "channel": "#dev"},
                                 "public:read", credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["channel_id"] == "C9"
    assert out["result"]["count"] == 2
    assert out["result"]["messages"][0]["text"] == "hello"


def test_execute_post_message(monkeypatch):
    calls = []

    def fake_api(method_name, token, payload=None):
        calls.append(method_name)
        if method_name == "conversations.list":
            return {"ok": True, "channels": [{"id": "C9", "name": "dev"}]}
        if method_name == "chat.postMessage":
            assert payload == {"channel": "C9", "text": "hi"}
            return {"ok": True, "ts": "12", "message": {"text": "hi"}}
        raise ProviderError("boom")

    monkeypatch.setattr(slack_mcp, "_api", fake_api)
    out = slack_mcp.provider.run(
        {"action": "post_message", "channel": "dev", "text": "hi"},
        "public:read", credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["ts"] == "12"
    assert calls == ["conversations.list", "chat.postMessage"]


def test_execute_search_messages(monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({
        "search.messages": {"ok": True, "messages": {"matches": [
            {"ts": "1", "user": "U1", "text": "deploy the gateway",
             "channel": "C9", "permalink": "https://x"}]}}}))
    out = slack_mcp.provider.run({"action": "search_messages", "query": "gateway"},
                                 "public:read", credential=TOKEN_JSON)
    assert out["ok"] is True
    assert out["result"]["count"] == 1
    assert "deploy" in out["result"]["messages"][0]["text"]


def test_execute_api_error_surfaced(monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({}))
    out = slack_mcp.provider.run({"action": "list_users"}, "public:read",
                                 credential=TOKEN_JSON)
    assert out["ok"] is False
    assert "unexpected method" in out["error"]


def test_execute_scope_gate():
    out = slack_mcp.provider.run({"action": "list_channels"}, "commercial:write",
                                 credential=TOKEN_JSON)
    assert out["ok"] is False
    assert "not granted" in out["error"]


def test_auth_headers_and_verify_probe(monkeypatch):
    import asyncio
    assert slack_mcp.SlackMcpProvider._auth_headers(TOKEN_JSON) == {"Authorization": "Bearer xoxb-fake-token"}
    assert slack_mcp.SlackMcpProvider._auth_headers({"token": "x"}) == {"Authorization": "Bearer x"}
    assert slack_mcp.SlackMcpProvider._auth_headers({}) is None

    probed = {}

    def fake_api(method_name, token, payload=None):
        probed.update(method=method_name, token=token)
        return {"ok": True, "user": "bot1", "team": "Acme"}

    monkeypatch.setattr(slack_mcp, "_api", fake_api)
    tools = asyncio.run(slack_mcp.provider._async_list_tools({"Authorization": "Bearer " + TOKEN}))
    names = [t["name"] for t in tools]
    assert "post_message" in names and "list_channels" in names
    assert probed["method"] == "auth.test"
    assert probed["token"] == TOKEN


# ── integration: through the app + gateway + vault ─────────────────────────

def _signup(client):
    email = f"slack-{int(time.time() * 1e6)}@example.com"
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "hunter2hunter"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_gateway_execute_slack_mcp_allowed(client, monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({
        "auth.test": {"ok": True, "team": "Acme", "user": "bot1"}}))
    headers = _signup(client)
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "slack-mcp", "credential": TOKEN_JSON},
                    headers=headers)
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/execute",
                    json={"provider_slug": "slack-mcp",
                          "arguments": {"action": "get_workspace_info"}},
                    headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["team"] == "Acme"


def test_gateway_execute_slack_mcp_no_credential_fails(client):
    headers = _signup(client)
    r = client.post("/api/v1/execute",
                    json={"provider_slug": "slack-mcp",
                          "arguments": {"action": "get_workspace_info"}},
                    headers=headers)
    assert r.status_code == 502
    assert "no Slack token" in r.json()["detail"]


def test_verify_slack_mcp_ok(client, monkeypatch):
    monkeypatch.setattr(slack_mcp, "_api", _branch_api({
        "auth.test": {"ok": True, "team": "Acme", "user": "bot1"}}))
    headers = _signup(client)
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "slack-mcp", "credential": TOKEN_JSON},
                    headers=headers)
    assert r.status_code == 201
    r = client.get("/api/v1/connections/slack-mcp/verify", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["authenticated"] is True
    assert "post_message" in body["tools"]


def test_verify_slack_mcp_bad_token(client, monkeypatch):
    # real slack_mcp._api maps {"ok": false, "error": ...} to ProviderError
    def fake_api(method_name, token, payload=None):
        raise ProviderError("Slack API auth.test: invalid_auth")

    monkeypatch.setattr(slack_mcp, "_api", fake_api)
    headers = _signup(client)
    r = client.post("/api/v1/connections",
                    json={"provider_slug": "slack-mcp", "credential": TOKEN_JSON},
                    headers=headers)
    assert r.status_code == 201
    r = client.get("/api/v1/connections/slack-mcp/verify", headers=headers)
    assert r.status_code == 502
    assert "invalid_auth" in r.json()["detail"]


def test_verify_slack_mcp_no_credential_400(client):
    headers = _signup(client)
    r = client.get("/api/v1/connections/slack-mcp/verify", headers=headers)
    assert r.status_code == 400
    assert "no credentials" in r.json()["detail"]