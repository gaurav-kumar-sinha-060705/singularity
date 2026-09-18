"""Outbound one-click MCP OAuth engine (RFC 9728 discovery + RFC 7591 dynamic
client registration + PKCE).

Used by the gateway to one-click connect to hosted MCP servers (Stripe, Notion,
Firecrawl, etc.) that publish OAuth authorization-server metadata. No env vars
needed for these — the client registers dynamically, user signs in once in the
browser, tokens live in the vault and refresh silently.

Public API (consumed by app/routers/auth.py):
  discover(cap)                → ASMetadata (cached)
  register_client(cap, db)     → (client_id, client_secret | None)  persisted
  authorize_start(cap, verifier, state, redirect_uri) → str (full AS authorize URL)
  exchange_token(cap, code, verifier, redirect_uri, db) → dict (token payload)
  refresh_if_needed(cap, credential, db) → dict (merged credential or unchanged)
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RemoteOAuthClient
from app.services.oauth_caps import OAuthCapability
from app.vault import decrypt, encrypt

TTL_SECONDS = 120  # discovery result cache


class DiscoveryError(Exception):
    pass


class RegistrationError(Exception):
    pass


class OAuthRefreshError(Exception):
    pass


@dataclass
class ASMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str] = None
    code_challenge_methods_supported: list[str] = field(default_factory=list)
    token_endpoint_auth_methods_supported: list[str] = field(default_factory=list)
    scopes_supported: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Network helpers (test-patchable)
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: Optional[dict] = None, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "SingularityOAuth/1.0", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DiscoveryError(f"{url}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise DiscoveryError(f"{url}: {exc}") from exc


def _http_post(url: str, payload: dict, headers: Optional[dict] = None, timeout: int = 10) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SingularityOAuth/1.0",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RegistrationError(f"{url}: HTTP {exc.code}: {raw}") from exc
    except Exception as exc:
        raise RegistrationError(f"{url}: {exc}") from exc


def _http_post_form(url: str, data: dict, headers: Optional[dict] = None, timeout: int = 10) -> dict:
    """OAuth token-endpoint request: RFC 6749 bodies are application/x-www-
    form-urlencoded. JSON bodies 4xx against real ASes (e.g. Notion's
    Cloudflare workers-oauth-provider), so token exchange/refresh use form."""
    body = urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "SingularityOAuth/1.0",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RegistrationError(f"{url}: HTTP {exc.code}: {raw}") from exc
    except Exception as exc:
        raise RegistrationError(f"{url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Discovery cache
# ---------------------------------------------------------------------------

_DISCOVERY: dict[str, tuple[float, ASMetadata]] = {}


def discover(cap: OAuthCapability, *, force: bool = False) -> ASMetadata:
    now = time.time()
    if not force:
        hit = _DISCOVERY.get(cap.slug)
        if hit and hit[0] + TTL_SECONDS > now:
            return hit[1]

    meta = _discover_as(cap)
    _DISCOVERY[cap.slug] = (time.time(), meta)
    return meta


def _discover_as(cap: OAuthCapability) -> ASMetadata:
    # 1) Direct well-known on the protected resource origin.
    origin = _origin(cap.resource or cap.server_url or cap.as_metadata_url)
    for base in [cap.as_metadata_url or _well_known_url(origin, "oauth-authorization-server")]:
        try:
            data = _http_get(base)
            return _parse_metadata(data)
        except DiscoveryError:
            pass

    # 2) Two-hop: protected-resource metadata → authorization_servers[0] → AS metadata.
    if cap.resource or cap.server_url:
        pr_url = _well_known_url(cap.resource or cap.server_url, "oauth-protected-resource")
        try:
            pr = _http_get(pr_url)
            servers = pr.get("authorization_servers", [])
            if servers:
                as_url = _well_known_url(servers[0], "oauth-authorization-server")
                try:
                    data = _http_get(as_url)
                    return _parse_metadata(data)
                except DiscoveryError:
                    pass
        except DiscoveryError:
            pass

    raise DiscoveryError(f"No OAuth AS metadata found for {cap.slug}")


def _parse_metadata(data: dict) -> ASMetadata:
    return ASMetadata(
        authorization_endpoint=data.get("authorization_endpoint", ""),
        token_endpoint=data.get("token_endpoint", ""),
        registration_endpoint=data.get("registration_endpoint"),
        code_challenge_methods_supported=data.get("code_challenge_methods_supported", []),
        token_endpoint_auth_methods_supported=data.get("token_endpoint_auth_methods_supported", []),
        scopes_supported=data.get("scopes_supported", []),
    )


def _origin(url: Optional[str]) -> str:
    if not url:
        return ""
    parts = url.split("/", 3)
    return "/".join(parts[:3]) if len(parts) >= 3 else url


def _well_known_url(server_or_origin: str, suffix: str) -> str:
    origin = _origin(server_or_origin)
    return f"{origin}/.well-known/{suffix}"


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------

def get_client_registration(db: Session, slug: str) -> Optional[RemoteOAuthClient]:
    return db.query(RemoteOAuthClient).filter_by(slug=slug).first()


def register_client(cap: OAuthCapability, db: Session) -> tuple[str, str | None]:
    existing = get_client_registration(db, cap.slug)
    if existing:
        secret = decrypt(existing.client_secret) if existing.client_secret else None
        return existing.client_id, secret

    meta = discover(cap)
    redirect_uri = f"{get_settings().oauth_public_base}/api/v1/auth/{cap.slug}/callback"
    auth_method = _pick_auth_method(meta.token_endpoint_auth_methods_supported)
    payload: dict = {
        "client_name": f"Singularity Gateway ({cap.name or cap.slug})",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if cap.resource:
        payload["resource"] = cap.resource

    if not meta.registration_endpoint:
        raise RegistrationError(f"No registration_endpoint for {cap.slug}")

    result = _http_post(meta.registration_endpoint, payload)
    client_id = result.get("client_id", "")
    client_secret_raw = result.get("client_secret") or None
    if not client_id:
        raise RegistrationError(f"Missing client_id in registration response for {cap.slug}")

    row = RemoteOAuthClient(
        slug=cap.slug,
        client_id=client_id,
        client_secret=encrypt(client_secret_raw) if client_secret_raw else None,
        token_auth_method=auth_method,
        registration_json=json.dumps(result),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return client_id, client_secret_raw


def _pick_auth_method(supported: list[str]) -> str:
    if "none" in supported:
        return "none"
    if "client_secret_post" in supported:
        return "client_secret_post"
    if "client_secret_basic" in supported:
        return "client_secret_basic"
    return "none"


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------

def _make_verifier() -> str:
    return secrets.token_urlsafe(43)[:128]


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(cap: OAuthCapability, client_id: str, verifier: str, state: str) -> str:
    meta = discover(cap)
    redirect_uri = f"{get_settings().oauth_public_base}/api/v1/auth/{cap.slug}/callback"
    params: dict = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    if cap.resource:
        params["resource"] = cap.resource
    return f"{meta.authorization_endpoint}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def _effective_auth_method(client: RemoteOAuthClient) -> str:
    if client.token_auth_method:
        return client.token_auth_method
    return "client_secret_post" if client.client_secret else "none"


def _token_request(
    client: RemoteOAuthClient, secret: str | None, base: dict
) -> tuple[dict, dict]:
    """Headers + body (form-encoded) for a token-endpoint call, authenticated the
    way the client was registered: none, client_secret_post (form field) or
    client_secret_basic (Basic header)."""
    method = _effective_auth_method(client)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    data = dict(base)
    if secret:
        if method == "client_secret_basic":
            creds = base64.b64encode(f"{client.client_id}:{secret}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        elif method == "client_secret_post":
            data["client_secret"] = secret
    return headers, data


def exchange_token(cap: OAuthCapability, code: str, verifier: str, db: Session) -> dict:
    meta = discover(cap)
    client = get_client_registration(db, cap.slug)
    if not client:
        raise RegistrationError(f"No client registration for {cap.slug}")

    redirect_uri = f"{get_settings().oauth_public_base}/api/v1/auth/{cap.slug}/callback"
    secret = decrypt(client.client_secret) if client.client_secret else None
    headers, payload = _token_request(client, secret, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": client.client_id,
    })
    token_data = _http_post_form(meta.token_endpoint, payload, headers=headers)
    if "access_token" not in token_data:
        raise RegistrationError(f"token exchange failed: {token_data}")
    now = time.time()
    token_data["expires_at"] = now + int(token_data.get("expires_in", 3600))
    token_data["auth_method"] = "mcp_oauth"
    return token_data


# ---------------------------------------------------------------------------
# Silent refresh
# ---------------------------------------------------------------------------

def refresh_if_needed(cap: OAuthCapability, credential: dict, db: Session) -> dict:
    refresh_token = credential.get("refresh_token")
    if not refresh_token:
        return credential
    expires_at = credential.get("expires_at")
    skew = get_settings().token_refresh_skew
    if expires_at and time.time() + skew < expires_at:
        return credential

    client = get_client_registration(db, cap.slug)
    if not client:
        return credential

    meta = discover(cap)
    secret = decrypt(client.client_secret) if client.client_secret else None
    headers, payload = _token_request(client, secret, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client.client_id,
    })

    try:
        token_data = _http_post_form(meta.token_endpoint, payload, headers=headers)
    except RegistrationError:
        raise OAuthRefreshError("refresh failed")
    if "access_token" not in token_data:
        raise OAuthRefreshError("no access_token in refresh response")

    now = time.time()
    merged = dict(credential)
    merged["access_token"] = token_data["access_token"]
    merged["refresh_token"] = token_data.get("refresh_token", refresh_token)
    merged["expires_at"] = now + int(token_data.get("expires_in", 3600))
    return merged


# ---------------------------------------------------------------------------
# Helpers for tests
# ---------------------------------------------------------------------------

def _clear_discovery_cache() -> None:
    _DISCOVERY.clear()


def refresh_for_slug(slug: str, credential: dict, db: Session) -> dict:
    """Unified silent refresh: picks strategy from the stored credential shape.
    mcp_oauth tokens refresh via the remote AS; provider-OAuth tokens via the
    app that minted them (app/oauth.provider_refresh). Never raises for refresh
    failures — unchanged credential is returned."""
    if credential.get("auth_method") == "mcp_oauth":
        cap = _cap_for(slug)
        if cap:
            try:
                return refresh_if_needed(cap, credential, db)
            except (OAuthRefreshError, DiscoveryError, RegistrationError):
                return credential
        return credential
    if credential.get("oauth_provider"):
        from app.oauth import ProviderRefreshError, get_provider, provider_refresh

        provider = get_provider(credential["oauth_provider"])
        if provider:
            try:
                return provider_refresh(provider, credential)
            except ProviderRefreshError:
                return credential
    return credential


def _cap_for(slug: str):
    from app.services.oauth_caps import get_capability

    return get_capability(slug)