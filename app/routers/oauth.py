"""OAuth 2.0 endpoints (Phase 4.3).

GET  /api/v1/oauth/{provider}/authorize     — start the redirect flow
GET  /api/v1/oauth/{provider}/callback      — exchange code for tokens, store in vault

Callback requires the resource owner to be the same intraday user that started
the flow, enforced via a signed, expiring `state` token carrying user_id.
Exchanged tokens are encrypted at rest in the credentials vault immediately.
"""

import asyncio
import json
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import Connection, User, log_event
from app.oauth import (
    OAuthStateError,
    build_state,
    get_provider,
    new_code_verifier,
    providers,
    resolve_state,
)
from app.vault import encrypt

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/providers")
def list_providers():
    configured = []
    for slug in providers():
        p = get_provider(slug)
        configured.append({
            "slug": p.slug,
            "name": p.name,
            "configured": p.configured,
            "scopes": sorted(p.scopes),
        })
    return configured


@router.get("/{provider}/authorize")
def oauth_authorize(
    provider: str,
    user: User = Depends(get_current_user),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown oauth provider")
    if not p.configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{p.name} OAuth is not configured on this instance "
            f"(set SINGULARITY_{p.slug.upper()}_CLIENT_ID / _CLIENT_SECRET)",
        )
    settings = get_settings()
    verifier = new_code_verifier()
    state = build_state(user.id, p.slug, verifier, settings.jwt_secret)
    redirect_uri = f"{settings.oauth_public_base}/api/v1/oauth/{p.slug}/callback"
    return RedirectResponse(url=p.authorize_query(redirect_uri, state, verifier), status_code=302)


@router.get("/{provider}/callback")
def oauth_callback(
    provider: str,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    p = get_provider(provider)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown oauth provider")

    settings = get_settings()
    try:
        user_id, state_provider, verifier = resolve_state(state, settings.jwt_secret)
    except OAuthStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if state_provider != p.slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state/provider mismatch")

    redirect_uri = f"{settings.oauth_public_base}/api/v1/oauth/{p.slug}/callback"
    payload = {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "client_id": p.client_id,
        "client_secret": p.client_secret,
        "code_verifier": verifier,
    }

    try:
        async def _exchange():
            async with httpx.AsyncClient(timeout=15.0) as client:
                headers = {"Accept": p.token_accept or "application/json"}
                resp = await client.post(p.token_url, data=payload, headers=headers)
                return resp

        resp = asyncio.run(_exchange())
        resp.raise_for_status()
        token_data = resp.json()
    except Exception as exc:
        log_event(db, "oauth_exchange_failed", channel="rest", user_id=user_id, provider=p.slug)
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"token exchange failed: {exc}") from exc

    credential = {"token_type": "bearer", "oauth_provider": p.slug, **token_data}

    db.query(Connection).filter(
        Connection.user_id == user_id, Connection.provider_slug == f"{p.slug}-mcp"
    ).delete()
    conn = Connection(
        id=str(uuid.uuid4()),
        user_id=user_id,
        provider_slug=f"{p.slug}-mcp",
        credential_json=encrypt(json.dumps(credential)),
    )
    db.add(conn)
    log_event(db, "connection_created", channel="oauth", user_id=user_id, provider=f"{p.slug}-mcp")
    db.commit()
    return {"ok": True, "provider": p.slug, "status": f"Connected. Tokens stored in the vault."}