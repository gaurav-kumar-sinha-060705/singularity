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
