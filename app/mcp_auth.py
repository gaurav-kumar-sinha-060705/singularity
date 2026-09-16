"""MCP-endpoint auth: resolve the requesting user from the Authorization header.

The streamable-HTTP MCP endpoint at /mcp is wrapped by this middleware. When an
MCP client (claude.ai, Claude Desktop, Claude Code) supplies a valid Singularity
access token it:

  1. validates the JWT (type=access),
  2. auto-provisions a users row keyed on the token's sub when absent (the
     "first tokened call provisions" path — run only after the human consented
     to the OAuth flow that issued the JWT),
  3. publishes the user_id on a contextvar so call_tool() can resolve vault
     credentials while streaming back on the same request task.

Anonymous callers keep working (public:read, no credential).
"""

from contextvars import ContextVar

from jose import JWTError, jwt

from app.config import get_settings
from app.database import SessionLocal
from app.models import User, log_event

_current_user_id: ContextVar[str | None] = ContextVar("mcp_user_scope", default=None)


def current_user_id() -> str | None:
    """User_id bound to the current MCP request, or None (anonymous)."""
    return _current_user_id.get()


def scope_user(user_id: str | None) -> ContextVar:
    """Set the MCP-request user scope; returns the token to reset after."""
    return _current_user_id.set(user_id)


def reset_user(token) -> None:
    _current_user_id.reset(token)


def _provision_user(payload: dict) -> str | None:
    sub = payload.get("sub")
    if not sub:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == sub).first()
        if not user:
            email = payload.get("email")
            if not email:
                return sub
            user = User(id=sub, email=email, password_hash="!oauth-provisioned")
            db.add(user)
            log_event(db, "user_auto_provisioned", channel="mcp", user_id=sub, email=email)
            db.commit()
        return sub
    finally:
        db.close()


def resolve_access_user(raw_authorization: str | None) -> str | None:
    """Validate a Bearer access token; return user_id. Invalid -> None (anonymous)."""
    if not raw_authorization:
        return None
    scheme, _, token = raw_authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    return _provision_user(payload)