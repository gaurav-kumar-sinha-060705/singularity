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


def rsa_client_keypair(kid="pytest-key"):
    """Generate an RSA keypair: (private_pem, jwks_dict) for client_assertions."""
    from base64 import urlsafe_b64encode

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")

    def _b64u(i):
        return urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    numbers = key.public_key().public_numbers()
    jwks = {
        "keys": [
            {"kty": "RSA", "n": _b64u(numbers.n), "e": _b64u(numbers.e), "kid": kid, "alg": "RS256"}
        ]
    }
    return private_pem, jwks


def register_secret_client(client, redirect_uri="http://testserver/callback", method="client_secret_post", **extra):
    """Register a client that authenticates with a minted client secret (or private_key_jwt)."""
    payload = {
        "redirect_uris": [redirect_uri],
        "client_name": "pytest-secret-client",
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "mcp:tools",
        **extra,
    }
    resp = client.post("/register", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def sign_client_assertion(client_id, private_pem, jwks, audience="https://testserver/token", kid="pytest-key"):
    """Sign an RFC 7523 client_assertion JWT for /token authentication."""
    import time

    from jose import jwt as jose_jwt

    return jose_jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": audience,
            "exp": int(time.time()) + 300,
            "jti": secrets.token_hex(16),
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


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