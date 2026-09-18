"""Provider popup approval: MCP challenge, consent popup, and authorize_url.

Covers the two halves of the click-to-approve flow:

  1. tools/call on an unconnected provider -> 401 + MCP-Authorization-Request
     (the header a real client uses to pop the browser window), and
     pass-through once a credential exists.
  2. The /mcp-auth consent popup (resource=<provider>) storing the credential
     and completing the MCP code flow; plus the REST authorize_url JSON.
"""

import json
import secrets
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oauth_helpers import issue_oauth_access_token, pkce, register_client  # noqa: E402


def _rpc(method, params, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


HEADERS = {"Accept": "application/json, text/event-stream"}
INIT = {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0.1"}}


def _user_id_for(email):
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        return user.id if user else None
    finally:
        db.close()


def _store_connection(user_id, provider_slug, token="github_pat_test"):
    import uuid

    from app.database import SessionLocal
    from app.models import Connection, log_event
    from app.vault import encrypt

    db = SessionLocal()
    try:
        db.query(Connection).filter(
            Connection.user_id == user_id, Connection.provider_slug == provider_slug
        ).delete()
        db.add(Connection(
            id=str(uuid.uuid4()),
            user_id=user_id,
            provider_slug=provider_slug,
            credential_json=encrypt(json.dumps({"access_token": token})),
        ))
        log_event(db, "connection_created", channel="test", user_id=user_id, provider=provider_slug)
        db.commit()
    finally:
        db.close()


def test_mcp_provider_challenge_without_credential(client):
    """Calling an unconnected requires_auth provider returns 401 with the popup
    challenge header (so compliant clients pop the approve window)."""
    token, _ = issue_oauth_access_token(client)
    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    client.post("/mcp", json=_rpc("initialize", INIT), headers=auth_headers)
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "call_tool",
                                 "arguments": {"provider_slug": "github-mcp",
                                               "arguments": {"tool": "list_repositories", "arguments": {}}}},
                  id_=2),
        headers=auth_headers,
    )
    assert resp.status_code == 401, resp.text
    www = resp.headers.get("www-authenticate", "")
    assert "MCP-Authorization-Request" in www
    assert "github-mcp" in www
    assert "/authorize/github-mcp" in resp.text


def test_mcp_provider_challenge_passes_when_credential(client, monkeypatch):
    """Once a credential exists for the calling user, the call runs (no popup)."""
    import app.providers.github as gh
    from app.services.gateway import execute_via_gateway

    def fake_execute(self, args, credential=None):
        return {"action": args["action"], "repositories": ["octo/repo"], "authenticated_as": "github…"}

    monkeypatch.setattr(gh.GithubProvider, "execute", fake_execute)

    token, email = issue_oauth_access_token(client)
    user_id = _user_id_for(email)
    _store_connection(user_id, "github-mcp")

    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    client.post("/mcp", json=_rpc("initialize", INIT), headers=auth_headers)
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "call_tool",
                                 "arguments": {"provider_slug": "github-mcp",
                                               "arguments": {"tool": "list_repositories", "arguments": {}}}},
                  id_=2),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    text = "".join(c.get("text", "") for c in body["result"]["content"])
    assert "executed through the audited gateway" in text
    assert "octo/repo" in text


def test_mcp_provider_challenge_ignores_keyless_provider(client):
    """Keyless providers are not challenged — they always run."""
    token, _ = issue_oauth_access_token(client)
    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    client.post("/mcp", json=_rpc("initialize", INIT), headers=auth_headers)
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "call_tool",
                                 "arguments": {"provider_slug": "npms_lookup",
                                               "arguments": {"package": "fastapi"}}},
                  id_=2),
        headers=auth_headers,
    )
    assert resp.status_code != 401
    assert "MCP-Authorization-Request" not in resp.headers.get("www-authenticate", "")


def test_mcp_consent_popup_stores_token_and_completes(client, monkeypatch):
    """Full server-side popup: MCP /authorize with resource=<provider> leads to a
    provider consent popup; approving with a token stores the vault credential and
    returns a code, and the resulting token powers the tool."""
    import uuid

    import app.providers.github as gh
    from app.database import SessionLocal
    from app.models import Connection
    from app.vault import decrypt

    def fake_execute(self, args, credential=None):
        return {"action": args["action"], "repositories": ["notion-ish/repo"], "authenticated_as": "github…"}

    monkeypatch.setattr(gh.GithubProvider, "execute", fake_execute)

    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = "http://testserver/callback"
    client_id = register_client(client, redirect_uri)
    email = f"popup-{secrets.token_hex(6)}@example.com"

    # 1) SDK /authorize carrying resource=github-mcp -> 302 to consent page
    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "mcp:tools",
        "resource": "github-mcp",
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    consent_url = resp.headers["location"]
    assert "resource=github-mcp" in consent_url

    # 2) GET consent popup -> provider connect page
    resp = client.get(consent_url)
    assert resp.status_code == 200
    assert "Connect GitHub" in resp.text
    parsed = urllib.parse.urlparse(consent_url)
    form = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # 3) sign in (creates user, cookie set)
    form.update({"signup": "1", "email": email, "password": "password123456"})
    resp = client.post("/mcp-auth/consent", data=form)
    if resp.status_code == 409:
        form["signup"] = "0"
        resp = client.post("/mcp-auth/consent", data=form)
    assert resp.status_code == 200

    # 4) approve with a token -> credential stored, MCP code minted
    form.update({"approve": "1", "token": "github_pat_abcdef1234567890"})
    resp = client.post("/mcp-auth/consent", data=form, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    code = qs["code"][0]
    assert qs["state"][0] == state

    # 5) credential is in the vault under the consent user
    db = SessionLocal()
    try:
        from app.models import User

        user = db.query(User).filter(User.email == email).first()
        conn = db.query(Connection).filter(
            Connection.user_id == user.id, Connection.provider_slug == "github-mcp"
        ).first()
    finally:
        db.close()
    assert conn is not None
    stored = json.loads(decrypt(conn.credential_json))
    assert stored["access_token"] == "github_pat_abcdef1234567890"

    # 6) exchange the code for an access token
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    # 7) the popup-authorized token now runs the tool (no challenge)
    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    client.post("/mcp", json=_rpc("initialize", INIT), headers=auth_headers)
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "call_tool",
                                 "arguments": {"provider_slug": "github-mcp",
                                               "arguments": {"tool": "list_repositories", "arguments": {}}}},
                  id_=2),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    text = "".join(c.get("text", "") for c in resp.json()["result"]["content"])
    assert "notion-ish/repo" in text


def test_oauth_authorize_url_json(client):
    """The popup reads a JSON authorize URL instead of following a 302 (opaque
    cross-origin redirects are unreadable)."""
    email = f"authurl-{secrets.token_hex(6)}@example.com"
    tokens = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "hunter2hunter"},
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # unconfigured provider -> 503 with a clear reason, not a redirect
    resp = client.get("/api/v1/oauth/stripe/authorize_url", headers=headers)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]

    # unknown provider -> 404
    resp = client.get("/api/v1/oauth/nope/authorize_url", headers=headers)
    assert resp.status_code == 404