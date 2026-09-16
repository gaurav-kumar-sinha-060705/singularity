"""The execution gateway: the single gate every provider call must pass through.

Policy:
  - only registered first-party providers execute (others: not_found)
  - requested scope must be in the provider's granted scopes (else: denied)
  - the provider's indexed trust score must meet execute_min_trust (else: denied)
Every decision — allowed, denied, failed, not_found — is written to the audit log.

Shared by the REST router (post /api/v1/execute) and the MCP tool call_tool.
"""

import json
import time

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import Connection, Tool, log_event
from app.providers import registry
from app.vault import decrypt

MAIN_SCOPE = "public:read"


def _resolve_credential(db: Session, user_id: str | None, provider_slug: str) -> dict | None:
    """Return the decrypted credential for (user, provider), or None."""
    if not user_id:
        return None
    conn = (
        db.query(Connection)
        .filter(Connection.user_id == user_id, Connection.provider_slug == provider_slug)
        .first()
    )
    if not conn:
        return None
    try:
        return json.loads(decrypt(conn.credential_json))
    except Exception:
        return None


def execute_via_gateway(provider_slug: str, arguments: dict, scope: str | None = None,
                        channel: str = "rest", db: Session | None = None,
                        ip_hash: str | None = None, user_id: str | None = None) -> dict:
    """Run one provider call through the gateway. Never raises for policy
    failures — returns a decision dict the caller maps to HTTP/MCP semantics."""
    settings = get_settings()
    close = db is None
    if close:
        db = SessionLocal()

    def _outcome(decision: str, reason: str | None, **extra) -> dict:
        return {"decision": decision, "reason": reason, "scope": scope or MAIN_SCOPE,
                "provider": provider_slug, **extra}

    try:
        if not settings.execute_enabled:
            return _outcome("disabled", "execution gateway is disabled")

        provider = registry.get_provider(provider_slug)
        if not provider:
            return _outcome("not_found", f"no gateway provider registered for '{provider_slug}'")

        tool = db.query(Tool).filter(Tool.slug == provider_slug).first()
        if not tool:
            return _outcome("not_found", f"provider not indexed under slug '{provider_slug}'")

        effective_scope = scope or next(iter(sorted(provider.scopes)))
        if effective_scope not in provider.scopes:
            log_event(db, "tool_executed", tool_id=tool.id, provider=provider_slug,
                      decision="denied", reason=f"scope '{effective_scope}' not granted",
                      channel=channel, ip_hash=ip_hash, scope_requested=effective_scope,
                      user_id=user_id)
            db.commit()
            return _outcome("denied", f"scope '{effective_scope}' is not granted to '{provider_slug}'")

        if tool.trust_score < settings.execute_min_trust:
            log_event(db, "tool_executed", tool_id=tool.id, provider=provider_slug,
                      decision="denied",
                      reason=f"trust score {tool.trust_score:.2f} below {settings.execute_min_trust}",
                      channel=channel, ip_hash=ip_hash, scope_requested=effective_scope,
                      user_id=user_id)
            db.commit()
            return _outcome("denied", "tool is blocked from execution (below trust threshold)")

        credential = _resolve_credential(db, user_id, provider_slug)
        t0 = time.monotonic()
        outcome = provider.run(arguments, effective_scope, credential=credential)
        latency = outcome.get("latency_ms") or int((time.monotonic() - t0) * 1000)
        decision = "allowed" if outcome.get("ok") else "failed"
        log_event(db, "tool_executed", tool_id=tool.id, provider=provider_slug,
                  version=provider.version, decision=decision,
                  reason=outcome.get("error") if not outcome.get("ok") else None,
                  latency_ms=latency, channel=channel, ip_hash=ip_hash,
                  scope_requested=effective_scope, user_id=user_id,
                  authenticated=bool(credential))
        db.commit()
        return {
            **outcome,
            "decision": decision,
            "reason": outcome.get("error") if not outcome.get("ok") else None,
            "scope": effective_scope,
            "provider": provider_slug,
            "version": provider.version,
        }
    finally:
        if close:
            db.close()