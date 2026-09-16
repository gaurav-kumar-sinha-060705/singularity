"""Connection endpoints: store / list / delete a user's credentials for a
hosted provider. Credentials are encrypted at rest via the vault; the API
never returns secret material (masked metadata only).
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Connection, User, log_event
from app.schemas import ConnectionIn, ConnectionOut
from app.vault import encrypt

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