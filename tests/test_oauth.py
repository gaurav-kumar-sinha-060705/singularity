import base64
import hashlib
import hmac
import json
import time

import pytest

from app.config import get_settings
from app.oauth import (
    OAuthStateError,
    b64url,
    build_state,
    get_provider,
    new_code_verifier,
    providers,
    resolve_state,
)


def test_providers_registry():
    slugs = providers()
    assert "stripe" in slugs and "notion" in slugs and "google" in slugs
    p = get_provider("slack")
    assert p.name == "Slack"
    assert p.configured is False  # no env creds in tests


def test_state_round_trip():
    secret = get_settings().jwt_secret
    verifier = new_code_verifier()
    state = build_state("user-123", "stripe", verifier, secret)
    u, p, v = resolve_state(state, secret)
    assert u == "user-123" and p == "stripe" and v == verifier


def test_state_tamper_detected():
    secret = get_settings().jwt_secret
    state = build_state("user-123", "stripe", new_code_verifier(), secret)
    with pytest.raises(OAuthStateError):
        resolve_state(state + "x", secret)


def test_state_wrong_secret():
    state = build_state("u", "stripe", "v", "secret-a")
    with pytest.raises(OAuthStateError):
        resolve_state(state, "secret-b")


def test_state_expired():
    secret = get_settings().jwt_secret
    payload = {
        "u": "user", "p": "stripe", "v": "verifier",
        "exp": int(time.time()) - 10,
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    digest = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()
    sig = b64url(digest)
    with pytest.raises(OAuthStateError):
        resolve_state(f"{raw}.{sig}", secret)


def test_authorize_url_includes_pkce():
    p = get_provider("google")
    qs = p.authorize_query(
        redirect_uri="https://singularity.example/cb",
        state="abc123",
        code_verifier="v" * 43,
    )
    assert "response_type=code" in qs
    assert "code_challenge=" in qs
    assert "code_challenge_method=S256" in qs
    assert "redirect_uri=https%3A%2F%2Fsingularity.example%2Fcb" in qs


def test_authorize_requires_auth(client):
    resp = client.get("/api/v1/oauth/providers")
    assert resp.status_code == 200
    slugs = [p["slug"] for p in resp.json()]
    assert "google" in slugs and "slack" in slugs


def test_authorize_unconfigured_returns_503(client):
    """Without env OAuth creds, authorize should refuse clearly."""
    # needs signed-in user first
    email = f"oauth{int(time.time() * 1000000)}@example.com"
    tokens = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "hunter2hunter"},
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    resp = client.get("/api/v1/oauth/unknown/authorize", headers=headers)
    assert resp.status_code == 404
    resp = client.get("/api/v1/oauth/stripe/authorize", headers=headers)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]