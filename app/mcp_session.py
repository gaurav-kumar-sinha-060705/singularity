"""Signed browser-session cookie + short-lived pending tokens for the MCP/popup
consent flow (shared by app.routers.mcp_auth_pages and app.routers.oauth).

Both the /mcp-auth consent page and the provider-OAuth hop are driven by a
browser that has no REST Bearer JWT, so identity and one-time pending state ride
in HMAC-signed cookie values (jwt_secret is the shared key).

  - session cookie `mcp_session`: {u: user_id, exp} -> keeps a human signed in
    across connector connects.
  - pending cookie `nxt_pending`: {u, c:client_id, r:redirect_uri, v:verifier,
    s:state, sc:scope, res:resource, exp} -> carries the in-flight MCP
    authorization-code request across the provider-OAuth round trip so the
    callback can finish the code handoff to the MCP client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from starlette.responses import Response

from app.config import get_settings

SESSION_COOKIE = "mcp_session"
PENDING_COOKIE = "nxt_pending"
MCP_SESSION_TTL_SECONDS = 1800
PENDING_TTL_SECONDS = 600


def _secret() -> str:
    return get_settings().jwt_secret


def sign(raw: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(_secret().encode(), raw.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()


def _wrap(payload: dict, ttl: int) -> str:
    data = {**payload, "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"{raw}.{sign(raw)}"


def _read(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        raw, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, sign(raw)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def wrap_session_cookie(user_id: str) -> str:
    return _wrap({"u": user_id}, MCP_SESSION_TTL_SECONDS)


def read_session(cookies: dict) -> str | None:
    payload = _read(cookies.get(SESSION_COOKIE))
    return payload.get("u") if payload else None


def set_session_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        wrap_session_cookie(user_id),
        max_age=MCP_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def wrap_pending_cookie(user_id, client_id, redirect_uri, verifier, state, scope, resource) -> str:
    return _wrap({
        "u": user_id,
        "c": client_id,
        "r": redirect_uri,
        "v": verifier,
        "s": state,
        "sc": scope,
        "res": resource,
    }, PENDING_TTL_SECONDS)


def read_pending(cookies: dict) -> dict | None:
    return _read(cookies.get(PENDING_COOKIE))


def clear_pending_cookie(response: Response) -> None:
    response.delete_cookie(PENDING_COOKIE)