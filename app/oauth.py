"""OAuth 2.0 (Authorization Code + PKCE) config for connecting external providers.

Fully data-driven: provider OAuth *app* configs live in `data/provider_oauth.json`
(keyed by a short slug like "github", "google"). Each row is activated at runtime
by env vars:

    SINGULARITY_<SLUG>_CLIENT_ID / SINGULARITY_<SLUG>_CLIENT_SECRET

OAuth here is one of the connect strategies for keyed tools (see
app.services.oauth_caps / data/oauth_capabilities.json). The gateway stores the
resulting tokens in the encrypted vault and injects them via the credential
adapter at execution. pkce_state() / resolve_state() let an anonymous browser
round-trip carry (user_id, code_verifier) safely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

OAUTH_STATE_TTL_SECONDS = 600

_OAUTH_JSON = Path(__file__).resolve().parent.parent / "data" / "provider_oauth.json"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(msg: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return b64url(digest)


def _verify(msg: bytes, signature: str, secret: str) -> bool:
    return hmac.compare_digest(b64url_decode(signature), hmac.new(secret.encode(), msg, hashlib.sha256).digest())


class OAuthStateError(ValueError):
    """Raised when an OAuth callback presents an invalid/expired state token."""


def build_state(user_id: str, provider_slug: str, code_verifier: str, secret: str) -> str:
    """Encode (user_id, provider, code_verifier) into a signed, expiring state token."""
    payload = json.dumps({
        "u": user_id,
        "p": provider_slug,
        "v": code_verifier,
        "exp": int(time.time()) + OAUTH_STATE_TTL_SECONDS,
    }, separators=(",", ":")).encode()
    sig = _sign(payload, secret)
    return f"{b64url(payload)}.{sig}"


def resolve_state(state: str, secret: str) -> tuple[str, str, str]:
    """Validate a state token; returns (user_id, provider_slug, code_verifier)."""
    try:
        raw, sig = state.rsplit(".", 1)
        payload = b64url_decode(raw)
    except Exception as exc:
        raise OAuthStateError("malformed state") from exc
    if not _verify(payload, sig, secret):
        raise OAuthStateError("invalid state signature")
    data = json.loads(payload)
    if data.get("exp", 0) < time.time():
        raise OAuthStateError("state expired")
    return data["u"], data["p"], data["v"]


@dataclass
class OAuthProviderConfig:
    slug: str
    name: str
    authorize_url: str
    token_url: str
    scopes: list[str] = field(default_factory=list)
    client_auth: str = "body"  # "body" = credentials in POST body; "basic" = Basic header
    token_accept: Optional[str] = None  # optional Accept header on token exchange

    @property
    def client_id(self) -> str:
        return os.getenv(f"SINGULARITY_{self.slug.upper()}_CLIENT_ID", "")

    @property
    def client_secret(self) -> str:
        return os.getenv(f"SINGULARITY_{self.slug.upper()}_CLIENT_SECRET", "")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_query(
        self,
        redirect_uri: str,
        state: str,
        code_verifier: str,
    ) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": b64url(hashlib.sha256(code_verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        return f"{self.authorize_url}?{urlencode(params)}"


def _load_providers() -> dict[str, OAuthProviderConfig]:
    raw = json.loads(_OAUTH_JSON.read_text(encoding="utf-8"))
    result: dict[str, OAuthProviderConfig] = {}
    for slug, entry in raw.items():
        result[slug] = OAuthProviderConfig(
            slug=slug,
            name=entry.get("name", slug),
            authorize_url=entry["authorize_url"],
            token_url=entry["token_url"],
            scopes=list(entry.get("scopes", [])),
            client_auth=entry.get("client_auth", "body"),
            token_accept=entry.get("token_accept"),
        )
    return result


_PROVIDERS: dict[str, OAuthProviderConfig] = _load_providers()


def providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def get_provider(slug: str) -> Optional[OAuthProviderConfig]:
    return _PROVIDERS.get(slug)


def new_code_verifier() -> str:
    return b64url(secrets.token_bytes(48))


class ProviderRefreshError(Exception):
    pass


def provider_refresh(p: OAuthProviderConfig, credential: dict) -> dict:
    """Silent refresh for provider-OAuth credentials that carry a refresh_token
    (e.g. Google). Returns the merged credential with fresh access_token."""
    if not credential.get("refresh_token"):
        return credential
    if not p.configured:
        raise ProviderRefreshError(f"{p.name} OAuth is not configured for refresh")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": credential["refresh_token"],
        "client_id": p.client_id,
        "client_secret": p.client_secret,
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            p.token_url,
            data=payload,
            headers={"Accept": p.token_accept or "application/json"},
        )
    try:
        resp.raise_for_status()
    except Exception as exc:
        raise ProviderRefreshError(f"refresh failed: {exc}") from exc
    data = resp.json()
    if "access_token" not in data:
        raise ProviderRefreshError("no access_token in refresh response")
    updated = dict(credential)
    updated["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        updated["refresh_token"] = data["refresh_token"]
    updated["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
    return updated