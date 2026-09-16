"""JWT authentication helpers: create/verify access + refresh tokens, and a
FastAPI dependency that resolves the current user from the Authorization header.

Token layout (HS256, issued at, expiry):
  - access: 15 minutes, carries {sub: user_id, email, type: "access"}
  - refresh: 7 days,   carries {sub: user_id, email, type: "refresh"}

An access token alone is enough for /me and vault writes; refresh tokens
can be exchanged for new access tokens at POST /api/v1/auth/refresh.
"""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User

_bearer = HTTPBearer(auto_error=False)

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)


def _secret() -> str:
    return get_settings().jwt_secret


def _encode(payload: dict, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    body = {
        **payload,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(body, _secret(), algorithm="HS256")


def create_access_token(user: User) -> str:
    return _encode({"sub": user.id, "email": user.email, "type": "access"}, ACCESS_TTL)


def create_refresh_token(user: User) -> str:
    return _encode({"sub": user.id, "email": user.email, "type": "refresh"}, REFRESH_TTL)


def _decode(token: str, expect_type: str) -> dict:
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc
    if payload.get("type") != expect_type:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")
    return payload


def resolve_user(payload: dict, db: Session) -> User:
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token missing subject")
    user = db.query(User).filter(User.id == sub).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    payload = _decode(credentials.credentials, expect_type="access")
    return resolve_user(payload, db)


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    """Like get_current_user but anonymous callers are allowed (returns None).

    A present-but-invalid token still 401s so misconfiguration is visible.
    """
    if credentials is None:
        return None
    payload = _decode(credentials.credentials, expect_type="access")
    return resolve_user(payload, db)


def get_user_from_refresh(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    payload = _decode(credentials.credentials, expect_type="refresh")
    return resolve_user(payload, db)