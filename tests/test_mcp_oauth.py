"""Native MCP OAuth (RFC 9745) end-to-end tests.

Tests the full DCR → authorize → consent → token exchange → /mcp flow.
Each test that hits /mcp obtains a valid access token via the consent flow
first (requirement under the 'Sign in now' connector mode).
"""

import json
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oauth_helpers import issue_oauth_access_token  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402


def _rpc(method, params, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


INITIALIZE = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0.0.1"},
}
HEADERS = {"Accept": "application/json, text/event-stream"}


def _parse(resp):
    body = resp.text
    if body.startswith("data:") or "\ndata:" in body:
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return resp.json()


# --- OAuth metadata endpoints ---------------------------------------------

def test_metadata_endpoint(client):
    """The authorization server metadata endpoint is reachable."""
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert "issuer" in body
    assert "authorization_endpoint" in body
    assert "token_endpoint" in body
    assert "registration_endpoint" in body


def test_metadata_scopes_supported(client):
    resp = client.get("/.well-known/oauth-authorization-server")
    body = resp.json()
    assert "issuer" in body
    assert body["response_types_supported"] == ["code"]
    assert "S256" in body["code_challenge_methods_supported"]


# --- Dynamic client registration -----------------------------------------

def test_register_public_client(client):
    resp = client.post(
        "/register",
        json={
            "redirect_uris": ["http://testserver/callback"],
            "client_name": "test-public",
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_id"]
    assert body["token_endpoint_auth_method"] == "none"
    assert body.get("client_secret") is None


def test_register_rejects_missing_redirect_uris(client):
    resp = client.post(
        "/register",
        json={"token_endpoint_auth_method": "none"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


# --- Consent page ---------------------------------------------------------

def test_consent_page_renders(client):
    """The consent page is reachable (GET with valid params returns HTML)."""
    verifier = "fake-verifier"
    import hashlib, base64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    # Register a client so the page is valid
    from oauth_helpers import register_client
    client_id = register_client(client)
    resp = client.get(
        "/mcp-auth/consent",
        params={
            "client_id": client_id,
            "redirect_uri": "http://testserver/callback",
            "code_challenge": challenge,
            "state": "test",
            "scope": "mcp:tools",
        },
    )
    assert resp.status_code == 200
    assert "connect" in resp.text.lower()


# --- Full flow integration -------------------------------------------------

def test_full_oauth_flow_issues_access_token(client):
    """Register → authorize → consent → code → token exchange succeeds."""
    token, email = issue_oauth_access_token(client)
    assert token
    assert len(token) > 20

    # Verify the user was created
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        assert user is not None
    finally:
        db.close()


def test_access_token_permits_mcp_initialize(client):
    """A valid access token lets us initialize /mcp."""
    token, _ = issue_oauth_access_token(client)
    resp = client.post(
        "/mcp",
        json=_rpc("initialize", INITIALIZE),
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = _parse(resp)
    info = body["result"]["serverInfo"]
    assert info["name"] == "singularity"


def test_access_token_permits_tool_call(client):
    """A valid access token lets us call tools via /mcp."""
    token, _ = issue_oauth_access_token(client)
    client.post(
        "/mcp",
        json=_rpc("initialize", INITIALIZE),
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {
            "name": "list_public_tools",
            "arguments": {},
        }, id_=2),
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = _parse(resp)
    text = "".join(c.get("text", "") for c in body["result"]["content"])
    assert "weather" in text


def test_invalid_token_rejected(client):
    """An invalid token returns 401 (not anonymous)."""
    resp = client.post(
        "/mcp",
        json=_rpc("initialize", INITIALIZE),
        headers={**HEADERS, "Authorization": "Bearer not.a.valid.token"},
    )
    assert resp.status_code == 401


def test_missing_token_rejected(client):
    """No token at all returns 401 (Sign in now)."""
    resp = client.post(
        "/mcp",
        json=_rpc("initialize", INITIALIZE),
        headers=HEADERS,
    )
    assert resp.status_code == 401


def test_refresh_token_flow(client):
    """Exchanging a refresh token yields a new access token."""
    import urllib.parse

    from oauth_helpers import pkce, register_client

    email = f"refresh-{secrets.token_hex(8)}@example.com"
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = "http://testserver/callback"
    client_id = register_client(client, redirect_uri)

    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "mcp:tools",
    }, follow_redirects=False)
    consent_url = resp.headers["location"]
    client.get(consent_url)

    parsed = urllib.parse.urlparse(consent_url)
    qs = urllib.parse.parse_qs(parsed.query)
    form = {k: v[0] for k, v in qs.items()}
    form.update({"signup": "1", "email": email, "password": "password123456"})
    client.post("/mcp-auth/consent", data=form)

    form["approve"] = "1"
    resp = client.post("/mcp-auth/consent", data=form, follow_redirects=False)
    location = resp.headers["location"]
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    initial = resp.json()
    refresh_token = initial["refresh_token"]
    assert refresh_token

    # Exchange refresh token
    resp = client.post("/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    })
    assert resp.status_code == 200, resp.text
    new_data = resp.json()
    assert new_data["access_token"]
    assert new_data["refresh_token"]
    assert new_data["access_token"] != initial["access_token"]

    # New token works on /mcp
    resp = client.post(
        "/mcp",
        json=_rpc("initialize", INITIALIZE),
        headers={**HEADERS, "Authorization": f"Bearer {new_data['access_token']}"},
    )
    assert resp.status_code == 200


def test_user_reuse_same_email_returns_same_user(client):
    """Two consent flows with the same email produce the same user."""
    shared_email = f"shared-{secrets.token_hex(8)}@example.com"
    token1, _ = issue_oauth_access_token(client, email=shared_email)
    token2, _ = issue_oauth_access_token(client, email=shared_email)

    # Both tokens resolve to the same user
    db = SessionLocal()
    try:
        from app.mcp_oauth import _hash_token, TokenRow
        row1 = db.query(TokenRow).filter(TokenRow.token_hash == _hash_token(token1)).first()
        row2 = db.query(TokenRow).filter(TokenRow.token_hash == _hash_token(token2)).first()
        assert row1 is not None and row2 is not None
        assert row1.user_id == row2.user_id
    finally:
        db.close()


# --- Incorrect password / duplicate email ---------------------------------

def test_consent_wrong_password_returns_401(client):
    """POST consent with wrong password returns 401."""
    import secrets
    import urllib.parse

    from oauth_helpers import pkce, register_client

    verifier, challenge = pkce()
    client_id = register_client(client)

    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://testserver/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "x",
        "scope": "mcp:tools",
    }, follow_redirects=False)
    consent_url = resp.headers["location"]

    parsed = urllib.parse.urlparse(consent_url)
    qs = urllib.parse.parse_qs(parsed.query)
    form = {k: v[0] for k, v in qs.items()}

    # Sign up first
    form.update({"signup": "1", "email": f"pw-test-{secrets.token_hex(4)}@example.com", "password": "password123456"})
    client.post("/mcp-auth/consent", data=form)

    # Now try to sign in with wrong password (using a different email)
    form2 = {k: v for k, v in form.items() if k not in ("email", "password", "signup", "approve")}
    form2.update({
        "email": f"pw-test-{secrets.token_hex(4)}@example.com",
        "password": "wrong-password-1",
        "signup": "0",
    })
    resp = client.post("/mcp-auth/consent", data=form2)
    assert resp.status_code == 401


# --- client_secret_post (the Render 500 regression) ------------------------

def test_register_client_secret_post_mints_secret(client):
    """client_secret_post registration succeeds and mints a secret (was a 500 on
    Postgres: the Fernet token exceeds the OLD VARCHAR(128) column)."""
    from oauth_helpers import register_secret_client

    body = register_secret_client(client)
    assert body["token_endpoint_auth_method"] == "client_secret_post"
    assert body["client_secret"]
    assert len(body["client_secret"]) == 64


def test_client_secret_round_trips_through_get_client(client):
    """The Fernet-encrypted secret must survive storage and decrypt on read
    (regression: column was too short to hold a Fernet token on Postgres)."""
    import asyncio

    from oauth_helpers import register_secret_client
    from app.mcp_oauth import SingularityOAuthProvider

    body = register_secret_client(client)
    provider = SingularityOAuthProvider()
    loaded = asyncio.run(provider.get_client(body["client_id"]))
    assert loaded is not None
    assert loaded.client_secret == body["client_secret"]


def test_full_oauth_flow_with_client_secret_post(client):
    """Full consent flow where /token authenticates with client_secret_post."""
    import urllib.parse

    from oauth_helpers import pkce, register_secret_client

    body = register_secret_client(client)
    client_id = body["client_id"]
    client_secret = body["client_secret"]
    verifier, challenge = pkce()

    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://testserver/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "cs-v1",
        "scope": "mcp:tools",
    }, follow_redirects=False)
    consent_url = resp.headers["location"]
    client.get(consent_url)

    parsed = urllib.parse.urlparse(consent_url)
    qs = urllib.parse.parse_qs(parsed.query)
    form = {k: v[0] for k, v in qs.items()}
    form.update({
        "signup": "1",
        "email": f"csp-{secrets.token_hex(6)}@example.com",
        "password": "csp-Pass1!",
    })
    client.post("/mcp-auth/consent", data=form)
    form["approve"] = "1"
    resp = client.post("/mcp-auth/consent", data=form, follow_redirects=False)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)["code"][0]

    # Success path: client authenticates WITH its minted secret.
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://testserver/callback",
        "client_id": client_id,
        "code_verifier": verifier,
        "client_secret": client_secret,
    })
    assert resp.status_code == 200, resp.text

    # A wrong secret must be rejected (401), proving the secret is actually checked.
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://testserver/callback",
        "client_id": client_id,
        "code_verifier": verifier,
        "client_secret": "wrong-secret-value",
    })
    assert resp.status_code == 401, resp.text


# --- private_key_jwt (Claude.ai DCR) ----------------------------------------

def test_metadata_advertises_private_key_jwt(client):
    """RFC 8414 metadata lists private_key_jwt as a supported auth method."""
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert "private_key_jwt" in body["token_endpoint_auth_methods_supported"]


def test_metadata_scopes_supported(client):
    """valid_scopes publishes scopes_supported in the RFC 8414 metadata."""
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["scopes_supported"] == ["mcp:tools"]


def test_register_private_key_jwt_client(client):
    """A private_key_jwt client registers with its JWKS, no secret minted."""
    from oauth_helpers import rsa_client_keypair

    private_pem, jwks = rsa_client_keypair()
    resp = client.post("/register", json={
        "redirect_uris": ["http://testserver/callback"],
        "client_name": "claude-pytest",
        "token_endpoint_auth_method": "private_key_jwt",
        "jwks": jwks,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "mcp:tools",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token_endpoint_auth_method"] == "private_key_jwt"
    assert body.get("client_secret") is None


def test_registration_rejects_out_of_scope_scopes(client):
    """DCR scope must be within valid_scopes (mcp:tools)."""
    resp = client.post("/register", json={
        "redirect_uris": ["http://testserver/callback"],
        "client_name": "scope-abuser",
        "token_endpoint_auth_method": "none",
        "scope": "mcp:tools admin:*",
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_private_key_jwt_full_flow(client):
    """Full OAuth flow where /token authenticates via an RFC 7523 assertion."""
    import urllib.parse

    from oauth_helpers import pkce, rsa_client_keypair, sign_client_assertion

    private_pem, jwks = rsa_client_keypair()
    resp = client.post("/register", json={
        "redirect_uris": ["http://testserver/callback"],
        "client_name": "claude-pytest",
        "token_endpoint_auth_method": "private_key_jwt",
        "jwks": jwks,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "mcp:tools",
    })
    assert resp.status_code == 201, resp.text
    client_id = resp.json()["client_id"]
    verifier, challenge = pkce()

    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://testserver/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "pk-v1",
        "scope": "mcp:tools",
    }, follow_redirects=False)
    consent_url = resp.headers["location"]
    client.get(consent_url)

    parsed = urllib.parse.urlparse(consent_url)
    qs = urllib.parse.parse_qs(parsed.query)
    form = {k: v[0] for k, v in qs.items()}
    form.update({
        "signup": "1",
        "email": f"pkjwt-{secrets.token_hex(6)}@example.com",
        "password": "pkjwt-Pass1!",
    })
    client.post("/mcp-auth/consent", data=form)
    form["approve"] = "1"
    resp = client.post("/mcp-auth/consent", data=form, follow_redirects=False)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)["code"][0]

    assertion = sign_client_assertion(client_id, private_pem, jwks)
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://testserver/callback",
        "client_id": client_id,
        "code_verifier": verifier,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    })
    assert resp.status_code == 200, resp.text
    access_token = resp.json()["access_token"]

    resp = client.post(
        "/mcp",
        json=_rpc("initialize", INITIALIZE),
        headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert _parse(resp)["result"]["serverInfo"]["name"] == "singularity"


def test_private_key_jwt_bad_signature_rejected(client):
    """A client_assertion signed by the wrong key is rejected with 401."""
    import urllib.parse

    from oauth_helpers import pkce, rsa_client_keypair, sign_client_assertion

    # Register client with keypair A, but sign the assertion with a different key
    private_pem, jwks = rsa_client_keypair(kid="key-a")
    other_pem, _other_jwks = rsa_client_keypair(kid="key-b")

    resp = client.post("/register", json={
        "redirect_uris": ["http://testserver/callback"],
        "client_name": "claude-pytest-bad",
        "token_endpoint_auth_method": "private_key_jwt",
        "jwks": jwks,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "mcp:tools",
    })
    assert resp.status_code == 201, resp.text
    client_id = resp.json()["client_id"]
    verifier, challenge = pkce()

    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://testserver/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "pk-bad",
        "scope": "mcp:tools",
    }, follow_redirects=False)
    consent_url = resp.headers["location"]
    client.get(consent_url)
    parsed = urllib.parse.urlparse(consent_url)
    qs = urllib.parse.parse_qs(parsed.query)
    form = {k: v[0] for k, v in qs.items()}
    form.update({
        "signup": "1",
        "email": f"pkjwt-bad-{secrets.token_hex(6)}@example.com",
        "password": "pkjwt-Pass1!",
    })
    client.post("/mcp-auth/consent", data=form)
    form["approve"] = "1"
    resp = client.post("/mcp-auth/consent", data=form, follow_redirects=False)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)["code"][0]

    # Sign with the WRONG key (key-b) while client registered key-a
    assertion = sign_client_assertion(client_id, other_pem, _other_jwks, kid="key-b")
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://testserver/callback",
        "client_id": client_id,
        "code_verifier": verifier,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    })
    assert resp.status_code == 401, resp.text

    # Also verify an assertion with a valid signature but wrong audience is rejected
    good_assertion = sign_client_assertion(client_id, private_pem, jwks, audience="https://evil.example/token")
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://testserver/callback",
        "client_id": client_id,
        "code_verifier": verifier,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": good_assertion,
    })
    assert resp.status_code == 401, resp.text
