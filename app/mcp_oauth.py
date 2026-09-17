"""Native MCP OAuth server (RFC 9745 / RFC 8414 / RFC 7591) provider.

Milestone 4.6. Implements the MCP SDK's `OAuthAuthorizationServerProvider` so
the streamable-HTTP endpoint at /mcp speaks standard server-side OAuth:

  - dynamic client registration (RFC 7591)  -> Claude.ai "Register automatically"
  - authorization code + PKCE (RFC 7636)     -> the /authorize -> consent -> /token flow
  - refresh token rotation (OAuth 2.1)       -> long-lived client sessions
  - access token verification at the resource server via `load_access_token`

Persisted state lives in `oauth_clients`, `oauth_auth_codes`, `oauth_tokens`
(tokens are SHA-256 hashed at rest; opaque 32-byte random values on the wire).

Access tokens carry `subject = user_id`, so tool handlers resolve the acting
user (and their vault credentials) through the SDK's auth context instead of a
custom Bearer JWT.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthToken
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import OAuthAuthCode, OAuthClient, OAuthToken as TokenRow, User
from app.vault import decrypt, encrypt

ACCESS_TOKEN_TTL_SECONDS = 900  # 15 minutes
REFRESH_TOKEN_TTL_DAYS = 7
AUTH_CODE_TTL_SECONDS = 600

DEFAULT_SCOPES = ["mcp:tools"]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _iso(value) -> int | None:
    """Epoch-seconds timestamp (int) for a stored datetime, or None.

    SQLite drops tzinfo, so naive datetimes are interpreted as UTC (not local)
    to keep comparisons correct across timezones.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if value is not None:
        return int(float(value))
    return None


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _to_client_full(row: OAuthClient) -> OAuthClientInformationFull:
    client_secret = decrypt(row.client_secret_hash) if row.client_secret_hash else None
    data = {
        "client_id": row.client_id,
        "client_name": row.client_name,
        "redirect_uris": [u for u in json.loads(row.redirect_uris_json)] if row.redirect_uris_json else [],
        "scope": row.scope,
        "grant_types": json.loads(row.grant_types_json) if row.grant_types_json else ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": row.token_endpoint_auth_method or "none",
        "client_secret": client_secret,
    }
    if row.jwks_json:
        try:
            data["jwks"] = json.loads(row.jwks_json)
        except json.JSONDecodeError:
            pass
    if client_secret:
        data["client_secret_expires_at"] = 0
    return OAuthClientInformationFull.model_validate(data)


def _resolve_jwks(client_info: OAuthClientInformationFull) -> str | None:
    """Inline `jwks` from the DCR request, falling back to a one-time fetch of
    `jwks_uri` (a client public-key document; fetched at registration time so the
    token endpoint can verify private_key_jwt assertions offline)."""
    if client_info.jwks is not None:
        return json.dumps(client_info.jwks)
    if client_info.jwks_uri is not None:
        import httpx

        try:
            resp = httpx.get(str(client_info.jwks_uri), timeout=10.0)
            resp.raise_for_status()
            return json.dumps(resp.json())
        except Exception as exc:  # pragma: no cover - degraded, logs and registers anyway
            print(f"[singularity] oauth: could not fetch jwks_uri {client_info.jwks_uri}: {exc}")
            return None
    return None


class SingularityOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """DB-backed OAuth authorization server provider for the /mcp endpoint."""

    def __init__(self) -> None:
        pass

    # ---- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        db = SessionLocal()
        try:
            row = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()
            return _to_client_full(row) if row else None
        finally:
            db.close()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_secret = client_info.client_secret
        db = SessionLocal()
        try:
            row = OAuthClient(
                client_id=client_info.client_id,
                client_secret_hash=encrypt(client_secret) if client_secret else None,
                client_name=client_info.client_name,
                redirect_uris_json=json.dumps([str(u) for u in (client_info.redirect_uris or [])]),
                scope=client_info.scope,
                grant_types_json=json.dumps(client_info.grant_types or ["authorization_code", "refresh_token"]),
                token_endpoint_auth_method=client_info.token_endpoint_auth_method or "none",
                jwks_json=_resolve_jwks(client_info),
            )
            db.add(row)
            db.commit()
        finally:
            db.close()

    # ---- authorization ---------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        settings = get_settings()
        query = urlencode({
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "state": params.state or "",
            "scope": " ".join(params.scopes or DEFAULT_SCOPES),
            "resource": params.resource or f"{settings.oauth_public_base}/mcp",
        })
        return f"{settings.oauth_public_base}/mcp-auth/consent?{query}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        db = SessionLocal()
        try:
            row = db.query(OAuthAuthCode).filter(OAuthAuthCode.code == authorization_code).first()
            if not row:
                return None
            return AuthorizationCode(
                code=row.code,
                scopes=row.scopes,
                expires_at=_iso(row.expires_at),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=True,
                resource=row.redirect_uri,
                subject=row.user_id,
            )
        finally:
            db.close()

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        if authorization_code.subject is None:
            raise TokenError("invalid_grant", "authorization code not bound to a user")
        return await self._mint_tokens(
            client.client_id,
            authorization_code.subject,
            authorization_code.scopes,
            db_hint=authorization_code.code,
        )

    # ---- tokens ----------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        db = SessionLocal()
        try:
            row = db.query(TokenRow).filter(TokenRow.token_hash == _hash_token(token)).first()
            if not row:
                return None
            # Row stores the *refresh* token expiry (7 days).  The access token itself is
            # short-lived; derive its real expiry from now.
            access_expires_at = int(time.time()) + ACCESS_TOKEN_TTL_SECONDS
            if _iso(row.expires_at) and _iso(row.expires_at) < access_expires_at:
                access_expires_at = _iso(row.expires_at)
            return AccessToken(
                token=token,
                client_id=row.client_id,
                scopes=row.scopes,
                expires_at=access_expires_at,
                resource=None,
                subject=row.user_id,
                claims={"sid": _hash_token(token)[:16]},
            )
        finally:
            db.close()

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        db = SessionLocal()
        try:
            row = db.query(TokenRow).filter(TokenRow.refresh_token_hash == _hash_token(refresh_token)).first()
            if not row:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=row.scopes,
                expires_at=_iso(row.expires_at),
                subject=row.user_id,
            )
        finally:
            db.close()

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if refresh_token.subject is None:
            raise TokenError("invalid_grant", "refresh token not bound to a user")
        access_token = await self._mint_tokens(
            client.client_id,
            refresh_token.subject,
            scopes,
            db_hint=refresh_token.token,
        )
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        raw = getattr(token, "token", None)
        if not raw:
            return
        digest = _hash_token(raw)
        db = SessionLocal()
        try:
            row = (
                db.query(TokenRow)
                .filter((TokenRow.token_hash == digest) | (TokenRow.refresh_token_hash == digest))
                .first()
            )
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()

    # ---- internal --------------------------------------------------------

    async def _mint_tokens(
        self,
        client_id: str,
        user_id: str,
        scopes: list[str],
        *,
        db_hint: str | None = None,
    ) -> OAuthToken:
        access_raw = _new_token()
        refresh_raw = _new_token()
        now = _now_dt()
        db = SessionLocal()
        try:
            if db_hint:
                # rotate: drop any prior token row for the exchanged code / refresh token
                db.query(TokenRow).filter(
                    (TokenRow.refresh_token_hash == _hash_token(db_hint))
                    | (TokenRow.token_hash == _hash_token(db_hint))
                ).delete()
            # Store the *refresh* token expiry (7 days) — the row's expires_at serves
            # as the refresh token's own TTL, while load_access_token derives a shorter
            # window for the access token.
            row = TokenRow(
                token_hash=_hash_token(access_raw),
                refresh_token_hash=_hash_token(refresh_raw),
                client_id=client_id,
                user_id=user_id,
                scopes_json=json.dumps(scopes),
                expires_at=now + timedelta(days=REFRESH_TOKEN_TTL_DAYS),
                created_at=now,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_raw,
        )


def issue_authorization_code(
    client_id: str,
    user_id: str,
    redirect_uri: str,
    code_challenge: str,
    scope: str,
    db: Session,
) -> str:
    """Mint and persist a one-time authorization code for a consented user."""
    code = _new_token()
    row = OAuthAuthCode(
        code=code,
        client_id=client_id,
        user_id=user_id,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        scopes_json=json.dumps(scope.split() or DEFAULT_SCOPES),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
    )
    db.add(row)
    db.commit()
    return code


def get_oauth_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email.lower().strip()).first()