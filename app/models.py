import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import bcrypt
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

_IP_SALT = os.environ.get("SINGULARITY_IP_SALT", "singularity-default-salt")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_ip(ip: str | None) -> str | None:
    if not ip or ip == "unknown":
        return None
    return hashlib.sha256(f"{_IP_SALT}:{ip}".encode()).hexdigest()[:16]


def _loads(value: str | None) -> list:
    return json.loads(value) if value else []


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    publisher: Mapped[str] = mapped_column(String(200), index=True)
    publisher_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(String(80), index=True)
    description: Mapped[str] = mapped_column(Text)
    mcp_available: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    pricing_tier: Mapped[str] = mapped_column(String(20), default="free")
    integrations_json: Mapped[str | None] = mapped_column("integrations", Text)
    permissions_requested_json: Mapped[str | None] = mapped_column("permissions_requested", Text)
    permissions_needed_json: Mapped[str | None] = mapped_column("permissions_needed", Text)
    trust_score: Mapped[float] = mapped_column(Float, default=0.5)
    trust_flags_json: Mapped[str | None] = mapped_column("trust_flags", Text)
    source: Mapped[str] = mapped_column(String(30), default="curated")
    execution_tier: Mapped[str] = mapped_column(String(20), default="unknown")
    requires_credential: Mapped[bool] = mapped_column(Boolean, default=False)
    # Auth connect semantics for a keyed tool (single-sourced from
    # data/oauth_capabilities.json via the seeder):
    #   mcp_oauth       - one-click dynamic sign-in (no client id needed)
    #   provider_oauth  - one-click via an OAuth app (client id + secret env)
    #   api_key         - paste a token (no OAuth at all, e.g. Browserbase)
    #   none            - anonymous / unauthenticated tool
    auth_mode: Mapped[str] = mapped_column(String(20), default="none")
    client_id_required: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def integrations(self) -> list:
        return _loads(self.integrations_json)

    @property
    def permissions_requested(self) -> list:
        return _loads(self.permissions_requested_json)

    @property
    def permissions_needed(self) -> list:
        return _loads(self.permissions_needed_json)

    @property
    def trust_flags(self) -> list:
        return _loads(self.trust_flags_json)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "publisher": self.publisher,
            "publisher_verified": self.publisher_verified,
            "category": self.category,
            "description": self.description,
            "mcp_available": self.mcp_available,
            "pricing_tier": self.pricing_tier,
            "integrations": self.integrations,
            "permissions_requested": self.permissions_requested,
            "permissions_needed": self.permissions_needed,
            "trust_score": self.trust_score,
            "trust_flags": self.trust_flags,
            "source": self.source,
            "execution_tier": self.execution_tier,
            "requires_credential": self.requires_credential,
            "auth_mode": self.auth_mode,
            "client_id_required": self.client_id_required,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tools.id"), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(50), index=True)
    detail_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # Declaring the relationship gives the unit of work an explicit dependency,
    # so Tool INSERTs are always emitted before their AuditLog rows (Postgres
    # enforces FK ordering; SQLite does not, which hid this for a long time).
    tool = relationship("Tool")


def log_event(db, event: str, tool_id: str | None = None, **detail) -> None:
    db.add(AuditLog(tool_id=tool_id, event=event, detail_json=json.dumps(detail)))


def _user_id(email: str) -> str:
    """Stable ID from email."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"singularity:user:{email.lower().strip()}"))


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("utf-8")

    def check_password(self, password: str) -> bool:
        return bcrypt.checkpw(
            password.encode("utf-8"), self.password_hash.encode("utf-8")
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "created_at": self.created_at.isoformat(),
        }


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    provider_slug: Mapped[str] = mapped_column(String(120), index=True)
    credential_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user = relationship("User")


class RemoteOAuthClient(Base):
    """Outbound dynamic-client registrations with hosted MCP OAuth servers
    (RFC 7591). One row per tool slug; client_secret may be absent (PKCE-only
    "none" servers). Tokens themselves stay in `connections` per user."""

    __tablename__ = "remote_oauth_clients"

    slug: Mapped[str] = mapped_column(String(120), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(512))
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OAuthClient(Base):
    """Dynamically registered MCP OAuth client (RFC 7591)."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Fernet-encrypted client secrets are ~184 chars for a 64-char secret; keep
    # headroom so Postgres (which enforces VARCHAR length, unlike SQLite) accepts
    # them. Postgres-only, boot-migrated from VARCHAR(128) — see app/migrations.py.
    client_secret_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    redirect_uris_json: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str | None] = mapped_column(String(500), nullable=True)
    grant_types_json: Mapped[str | None] = mapped_column(Text)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # public JWKS (JSON) the client presented via DCR (private_key_jwt auth) —
    # lets the token endpoint verify `client_assertion` JWTs offline.
    jwks_json: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def redirect_uris(self) -> list:
        return _loads(self.redirect_uris_json)

    @property
    def grant_types(self) -> list:
        return _loads(self.grant_types_json)


class OAuthAuthCode(Base):
    """One-time authorization code with PKCE binding (RFC 7636)."""

    __tablename__ = "oauth_auth_codes"

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    code_challenge: Mapped[str] = mapped_column(String(128))
    redirect_uri: Mapped[str] = mapped_column(Text)
    scopes_json: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def scopes(self) -> list:
        return _loads(self.scopes_json)


class OAuthToken(Base):
    """Issued access + refresh tokens for an MCP client on behalf of a user."""

    __tablename__ = "oauth_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    scopes_json: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def scopes(self) -> list:
        return _loads(self.scopes_json)
