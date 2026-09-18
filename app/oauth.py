"""OAuth 2.0 (Authorization Code + PKCE) for connecting external providers.

Phase 4.3. Each provider is configured via env vars:

    SINGULARITY_<SLUG>_CLIENT_ID / SINGULARITY_<SLUG>_CLIENT_SECRET

OAuth is complementary to the manual API-key path (POST /connections): for
providers that *only* support OAuth (Slack, Google Drive), users connect via a
redirect flow; the gateway stores the resulting tokens in the encrypted vault
and injects them as Bearer headers on execution. pkce_state() / resolve_state()
let an anonymous browser round-trip carry (user_id, code_verifier) safely.
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
from typing import Optional
from urllib.parse import urlencode

OAUTH_STATE_TTL_SECONDS = 600


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


def providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def get_provider(slug: str) -> Optional[OAuthProviderConfig]:
    return _PROVIDERS.get(slug)


def new_code_verifier() -> str:
    return b64url(secrets.token_bytes(48))


_PROVIDERS: dict[str, OAuthProviderConfig] = {
    "stripe": OAuthProviderConfig(
        slug="stripe", name="Stripe",
        authorize_url="https://connect.stripe.com/oauth/authorize",
        token_url="https://connect.stripe.com/oauth/token",
        scopes=["read_only"],
    ),
    "notion": OAuthProviderConfig(
        slug="notion", name="Notion",
        authorize_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        client_auth="basic",
        token_accept="application/json",
    ),
    "github": OAuthProviderConfig(
        slug="github", name="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["repo", "read:user"],
        token_accept="application/json",
    ),
    "slack": OAuthProviderConfig(
        slug="slack", name="Slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=[
            "team:read",
            "channels:read",
            "groups:read",
            "channels:history",
            "groups:history",
            "chat:write",
            "users:read",
            "search:read",
        ],
    ),
    "google": OAuthProviderConfig(
        slug="google", name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/documents.readonly",
        ],
        client_auth="basic",
    ),
}