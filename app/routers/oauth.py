"""OAuth 2.0 endpoints for provider connection (Phase 4.3 + popup consent).

Provider OAuth (uses real GitHub/Slack/Notion OAuth apps when configured):

    GET /api/v1/oauth/{provider}/authorize_url  — JSON {url} for the popup's JS
    GET /api/v1/oauth/{provider}/authorize      — 302 into the provider's consent
    GET /api/v1/oauth/{provider}/callback       — code exchange -> vault store

Identity for the start of a flow is either the REST Bearer JWT (dashboard /
popup) or the signed `mcp_session` cookie (an /mcp-auth consent popup started
from an MCP tools/call challenge). The callback requires the resource owner to
be the same user that started the flow (signed expiring `state`).

When the flow was started from an MCP provider popup, the browser also carries
a `nxt_pending` cookie; the callback then finishes the in-flight MCP
authorization-code consent (mint code, redirect to the client) instead of only
returning the popup's success page.
"""

import asyncio
import json
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from urllib.parse import urlencode

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.mcp_oauth import issue_authorization_code
from app.mcp_session import clear_pending_cookie, read_pending, read_session
from app.models import Connection, OAuthClient, User, log_event
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


def current_user_or_mcp_session(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """The REST Bearer user, falling back to the signed mcp_session cookie that
    the /mcp-auth consent browser carries."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw = auth_header[7:]
        try:
            payload = jwt.decode(raw, get_settings().jwt_secret, algorithms=["HS256"])
            if payload.get("type") == "access" and payload.get("sub"):
                user = db.query(User).filter(User.id == payload["sub"]).first()
                if user:
                    return user
        except JWTError:
            pass
    user_id = read_session(request.cookies)
    if user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")


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


def _provider_or_404(slug: str):
    p = get_provider(slug)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown oauth provider")
    return p


def _checked_provider(p) -> str:
    if not p.configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{p.name} OAuth is not configured on this instance "
            f"(set SINGULARITY_{p.slug.upper()}_CLIENT_ID / _CLIENT_SECRET)",
        )
    return p


@router.get("/{provider}/authorize_url")
def oauth_authorize_url(
    provider: str,
    user: User = Depends(get_current_user),
):
    """JSON authorize URL so a popup can navigate to the provider's login
    (Reading a 302 Location header cross-origin is impossible, so the popup is
    not allowed to follow the redirect itself)."""
    p = _checked_provider(_provider_or_404(provider))
    settings = get_settings()
    verifier = new_code_verifier()
    state = build_state(user.id, p.slug, verifier, settings.jwt_secret)
    redirect_uri = f"{settings.oauth_public_base}/api/v1/oauth/{p.slug}/callback"
    return {"url": p.authorize_query(redirect_uri, state, verifier), "name": p.name}


@router.get("/{provider}/authorize")
def oauth_authorize(
    provider: str,
    request: Request,
    user: User = Depends(current_user_or_mcp_session),
):
    p = _checked_provider(_provider_or_404(provider))
    settings = get_settings()
    verifier = new_code_verifier()
    state = build_state(user.id, p.slug, verifier, settings.jwt_secret)
    redirect_uri = f"{settings.oauth_public_base}/api/v1/oauth/{p.slug}/callback"
    return RedirectResponse(url=p.authorize_query(redirect_uri, state, verifier), status_code=302)


def _complete_mcp_pending(db: Session, request: Request, user_id: str) -> "RedirectResponse | None":
    """Finish an MCP provider popup: the browser arrived back with a `nxt_pending`
    cookie carrying the in-flight authorization-code request. Mint the code and
    hand it to the MCP client."""
    pending = read_pending(request.cookies)
    if not pending:
        return None
    client_row = db.query(OAuthClient).filter(OAuthClient.client_id == pending["c"]).first()
    if not client_row:
        return None
    allowed = json.loads(client_row.redirect_uris_json) if client_row.redirect_uris_json else []
    if pending["r"] not in allowed:
        return None
    redirect_uri = pending["r"]
    code = issue_authorization_code(
        client_id=pending["c"],
        user_id=user_id,
        redirect_uri=redirect_uri,
        code_challenge=pending["v"],
        scope=pending.get("sc") or "mcp:tools",
        db=db,
    )
    sep = "&" if "?" in redirect_uri else "?"
    resp = RedirectResponse(
        f'{redirect_uri}{sep}{urlencode({"code": code, "state": pending.get("s") or ""})}',
        status_code=303,
    )
    clear_pending_cookie(resp)
    return resp


@router.get("/{provider}/callback")
def oauth_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    db: Session = Depends(get_db),
):
    p = _provider_or_404(provider)
    settings = get_settings()
    try:
        user_id, state_provider, verifier = resolve_state(state, settings.jwt_secret)
    except OAuthStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if state_provider != p.slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state/provider mismatch")

    if not p.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{p.name} OAuth is not configured")

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

    access_token = (token_data.get("access_token")
                    or (token_data.get("authed_user") or {}).get("access_token")
                    or "")
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

    pending_done = _complete_mcp_pending(db, request, user_id)
    if pending_done is not None:
        return pending_done

    settings = get_settings()
    return RedirectResponse(
        f"{settings.oauth_public_base}/authorize/{p.slug}-mcp?connected=1",
        status_code=303,
    )