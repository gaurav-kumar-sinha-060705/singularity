"""Connection endpoints: store / list / delete a user's credentials for a
hosted provider. Credentials are encrypted at rest via the vault; the API
never returns secret material (masked metadata only).
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import Connection, User, log_event
from app.providers import registry
from app.schemas import ConnectionIn, ConnectionOut
from app.vault import decrypt, encrypt

router = APIRouter(prefix="/connections", tags=["connections"])


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: ConnectionIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not payload.credential:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "credential is empty")

    existing = (
        db.query(Connection)
        .filter(Connection.user_id == user.id, Connection.provider_slug == payload.provider_slug)
        .first()
    )
    if existing:
        existing.credential_json = encrypt(json.dumps(payload.credential))
        db.commit()
        db.refresh(existing)
        log_event(db, "connection_updated", channel="rest", user_id=user.id,
                  provider=payload.provider_slug)
        db.commit()
        return ConnectionOut(
            id=existing.id,
            provider_slug=existing.provider_slug,
            created_at=existing.created_at.isoformat(),
            updated_at=existing.updated_at.isoformat(),
        )

    conn = Connection(
        id=str(uuid.uuid4()),
        user_id=user.id,
        provider_slug=payload.provider_slug,
        credential_json=encrypt(json.dumps(payload.credential)),
    )
    db.add(conn)
    log_event(db, "connection_created", channel="rest", user_id=user.id,
              provider=payload.provider_slug)
    db.commit()
    db.refresh(conn)
    return ConnectionOut(
        id=conn.id,
        provider_slug=conn.provider_slug,
        created_at=conn.created_at.isoformat(),
        updated_at=conn.updated_at.isoformat(),
    )


@router.get("", response_model=list[ConnectionOut])
def list_connections(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(Connection).filter(Connection.user_id == user.id).all()
    return [
        ConnectionOut(
            id=c.id,
            provider_slug=c.provider_slug,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
        )
        for c in rows
    ]


@router.get("/{provider_slug}/verify")
def verify_connection(
    provider_slug: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Probe the remote MCP with the stored credential and report tool count.

    Verifies exactly the path Claude uses: decrypt the credential -> build auth
    headers -> initialize + tools/list against the hosted MCP endpoint. A 200
    means a subsequent call_tool(provider_slug, ...) from the same account will
    authenticate.
    """
    provider = registry.get_provider(provider_slug)
    if not provider:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no gateway provider '{provider_slug}'")

    conn = (
        db.query(Connection)
        .filter(Connection.user_id == user.id, Connection.provider_slug == provider_slug)
        .first()
    )
    if not conn:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"no credentials stored for '{provider_slug}' — connect the account first",
        )

    try:
        credential = json.loads(decrypt(conn.credential_json))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "stored credential could not be decrypted"
        ) from exc

    list_tools = getattr(provider, "_async_list_tools", None)
    if list_tools is None:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            f"auto-verify is only supported for hosted remote MCP providers, not '{provider_slug}'",
        )

    headers = None
    build_headers = getattr(provider, "_auth_headers", None)
    if build_headers is not None:
        headers = build_headers(credential)
        if provider.requires_auth and not headers:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "stored credential yields no auth headers — store an access_token/api_key or headers",
            )

    settings = get_settings()
    try:
        tools = asyncio.run(
            asyncio.wait_for(
                list_tools(headers),
                timeout=settings.remote_mcp_timeout,
            )
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"verification failed against remote MCP: {exc}"
        ) from exc

    log_event(db, "connection_verified", channel="rest", user_id=user.id, provider=provider_slug)
    db.commit()
    return {
        "verified": True,
        "provider_slug": provider_slug,
        "provider_name": getattr(provider, "name", provider_slug),
        "remote_url": getattr(provider, "remote_url", None),
        "authenticated": bool(headers),
        "tool_count": len(tools),
        "tools": [t["name"] for t in tools],
    }


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conn = (
        db.query(Connection)
        .filter(Connection.id == connection_id, Connection.user_id == user.id)
        .first()
    )
    if not conn:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    db.delete(conn)
    log_event(db, "connection_deleted", channel="rest", user_id=user.id,
              provider=conn.provider_slug)
    db.commit()
    return None