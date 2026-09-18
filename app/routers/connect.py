"""Generic one-click connect for keyed MCP tools (data-driven, no per-tool code).

Every keyed tool slug resolves to a connect strategy from
data/oauth_capabilities.json at runtime:

    GET /api/v1/auth/catalog?slug=...        — capabilities + connected state
    GET /api/v1/auth/{slug}/start           — pick strategy; 302/JSON to the
                                              provider/AS consent, or {method: api_key}
    GET /api/v1/auth/{slug}/callback        — code exchange -> vault under the
                                              *tool slug*, then finish an in-flight
                                              MCP popup consent if present.

Strategies:
  - mcp_oauth   — one-click RFC 9728: discovery + dynamic client registration +
                  PKCE against the hosted server's own AS (Stripe, Notion,
                  Firecrawl). No env vars, no app creation.
  - provider_oauth — one-click through a configured OAuth app (GitHub/Slack/Google).
  - api_key     — paste a token (Browserbase, Sentry, ...); auto-fallback when
                  the mcp_oauth AS is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Connection, User, log_event
from app.oauth import (
    OAuthStateError,
    build_state,
    get_provider,
    new_code_verifier,
    resolve_state,
)
from app.routers.oauth import _complete_mcp_pending, current_user_or_mcp_session
from app.services import mcp_oauth as mcp_oauth_svc
from app.services.mcp_oauth import DiscoveryError, RegistrationError
from app.services.oauth_caps import all_capabilities, get_capability
from app.vault import encrypt

router = APIRouter(prefix="/auth", tags=["connect"])


def _cap_or_404(slug: str):
    cap = get_capability(slug)
    if not cap:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no connect capability for '{slug}'")
    return cap


def _optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        try:
            payload = jwt.decode(auth_header[7:], get_settings().jwt_secret, algorithms=["HS256"])
            if payload.get("type") == "access" and payload.get("sub"):
                user = db.query(User).filter(User.id == payload["sub"]).first()
                if user:
                    return user
        except JWTError:
            pass
    return None


def _connected(db: Session, user: User | None, slug: str) -> bool:
    if not user:
        return False
    return (
        db.query(Connection)
        .filter(Connection.user_id == user.id, Connection.provider_slug == slug)
        .first()
        is not None
    )


@router.get("/catalog")
def auth_catalog(
    slug: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    user = _optional_user(request, db)
    caps = [get_capability(slug)] if slug else list(all_capabilities().values())
    caps = [c for c in caps if c]
    return {
        "items": [
            {
                **c.public_dict(),
                "connected": _connected(db, user, c.slug),
            }
            for c in caps
        ]
    }


@router.get("/{slug}/start")
def auth_start(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    cap = _cap_or_404(slug)
    user = current_user_or_mcp_session(request, db)
    settings = get_settings()

    if cap.prefer == "mcp_oauth":
        verifier = mcp_oauth_svc._make_verifier()
        state = build_state(user.id, slug, verifier, settings.jwt_secret)
        try:
            client_id, _secret = mcp_oauth_svc.register_client(cap, db)
        except (DiscoveryError, RegistrationError) as exc:
            if cap.can("api_key"):
                return {"method": "api_key", "name": cap.name, "slug": slug,
                        "api_key_hint": cap.api_key_hint}
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"OAuth discovery failed for {cap.name}: {exc}",
            ) from exc
        url = mcp_oauth_svc.authorize_url(cap, client_id, verifier, state)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or "url" in request.query_params:
            return {"method": "mcp_oauth", "name": cap.name, "url": url}
        return RedirectResponse(url=url, status_code=302)

    if cap.prefer == "provider_oauth":
        p = get_provider(cap.provider)
        if not p or not p.configured:
            if cap.can("api_key"):
                return {"method": "api_key", "name": cap.name, "slug": slug,
                        "api_key_hint": cap.api_key_hint}
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"{cap.name} OAuth is not configured on this instance "
                f"(set SINGULARITY_{cap.provider.upper()}_CLIENT_ID / _CLIENT_SECRET)",
            )
        verifier = new_code_verifier()
        state = build_state(user.id, slug, verifier, settings.jwt_secret)
        redirect_uri = f"{settings.oauth_public_base}/api/v1/auth/{slug}/callback"
        url = p.authorize_query(redirect_uri, state, verifier)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"method": "provider_oauth", "name": p.name, "url": url}
        return RedirectResponse(url=url, status_code=302)

    return {"method": "api_key", "name": cap.name, "slug": slug, "api_key_hint": cap.api_key_hint}


@router.get("/{slug}/callback")
def auth_callback(
    slug: str,
    code: str,
    state: str,
    request: Request,
    db: Session = Depends(get_db),
):
    cap = _cap_or_404(slug)
    settings = get_settings()
    try:
        user_id, state_slug, verifier = resolve_state(state, settings.jwt_secret)
    except OAuthStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if state_slug != slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state/tool mismatch")

    try:
        token_data = _exchange_token(cap, slug, code, verifier, db)
    except mcp_oauth_svc.RegistrationError as exc:
        log_event(db, "oauth_exchange_failed", channel="rest", user_id=user_id, provider=slug)
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"token exchange failed: {exc}") from exc
    except Exception as exc:
        log_event(db, "oauth_exchange_failed", channel="rest", user_id=user_id, provider=slug)
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"token exchange failed: {exc}") from exc

    db.query(Connection).filter(
        Connection.user_id == user_id, Connection.provider_slug == slug
    ).delete()
    conn = Connection(
        id=str(uuid.uuid4()),
        user_id=user_id,
        provider_slug=slug,
        credential_json=encrypt(json.dumps(token_data)),
    )
    db.add(conn)
    log_event(db, "connection_created", channel="oauth", user_id=user_id, provider=slug)
    db.commit()

    pending_done = _complete_mcp_pending(db, request, user_id)
    if pending_done is not None:
        return pending_done

    return RedirectResponse(
        f"{settings.oauth_public_base}/authorize/{slug}?connected=1",
        status_code=303,
    )


def _exchange_token(cap, slug: str, code: str, verifier: str, db: Session) -> dict:
    settings = get_settings()
    if cap.prefer == "mcp_oauth":
        token_data = mcp_oauth_svc.exchange_token(cap, code, verifier, db)
        credential = {"token_type": "bearer", "auth_method": "mcp_oauth", **token_data}
        return credential

    provider_slug = cap.provider
    p = get_provider(provider_slug) if provider_slug else None
    if not p or not p.configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{cap.name} OAuth is not configured on this instance",
        )
    redirect_uri = f"{settings.oauth_public_base}/api/v1/auth/{slug}/callback"
    payload = {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "client_id": p.client_id,
        "client_secret": p.client_secret,
        "code_verifier": verifier,
    }

    async def _exchange():
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.post(
                p.token_url, data=payload, headers={"Accept": p.token_accept or "application/json"}
            )

    resp = asyncio.run(_exchange())
    resp.raise_for_status()
    token_data = resp.json()
    access_token = (
        token_data.get("access_token")
        or (token_data.get("authed_user") or {}).get("access_token")
        or ""
    )
    if not access_token:
        raise mcp_oauth_svc.RegistrationError("no access_token in response")
    return {"token_type": "bearer", "oauth_provider": provider_slug, **token_data}