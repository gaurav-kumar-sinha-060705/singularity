"""Shared OAuth helpers for /mcp tests (drive the full consent flow)."""

import base64
import hashlib
import secrets
import urllib.parse


def pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def register_client(client, redirect_uri="http://testserver/callback"):
    resp = client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "pytest-client",
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


def issue_oauth_access_token(client, *, email=None, password="password123456"):
    """Drive the full OAuth consent flow and return (access_token, email)."""
    if not email:
        email = f"oauth-{secrets.token_hex(8)}@example.com"
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = "http://testserver/callback"

    client_id = register_client(client, redirect_uri)

    # 1) /authorize → 302 to consent page
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "mcp:tools",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    consent_url = resp.headers["location"]

    # 2) GET consent page (sign-in form)
    resp = client.get(consent_url)
    assert resp.status_code == 200

    # Extract hidden params from the consent URL query string
    parsed = urllib.parse.urlparse(consent_url)
    qs = urllib.parse.parse_qs(parsed.query)
    form = {k: v[0] for k, v in qs.items()}

    # 3) POST signup (creates user, shows approve page)
    form.update({"signup": "1", "email": email, "password": password})
    resp = client.post("/mcp-auth/consent", data=form)
    if resp.status_code == 409:
        # User already exists from a prior flow — sign in instead
        form["signup"] = "0"
        resp = client.post("/mcp-auth/consent", data=form)
    assert resp.status_code == 200

    # 4) POST approve → 303 to redirect_uri?code=...
    form["approve"] = "1"
    resp = client.post("/mcp-auth/consent", data=form, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    location_qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    code = location_qs["code"][0]

    # 5) /token → access_token
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"], email