"""Capability catalog for every keyed MCP tool (data-driven, no code branches).

Each tool slug maps to the connect methods it supports. `prefer` picks the UX:
`mcp_oauth`  — one-click RFC 9728 OAuth with the hosted server (discovery +
               dynamic registration done in app/services/mcp_oauth.py).
`provider_oauth` — one-click through a human app config (data/provider_oauth.json).
`api_key`    — paste a token into the vault (Browserbase, Sentry, Jira, ...).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CAPS_JSON = Path(__file__).resolve().parent.parent.parent / "data" / "oauth_capabilities.json"


@dataclass(frozen=True)
class OAuthCapability:
    slug: str
    prefer: str = "api_key"
    name: str = ""
    provider: Optional[str] = None
    resource: Optional[str] = None
    as_metadata_url: Optional[str] = None
    oauth_server_url: Optional[str] = None
    server_url: Optional[str] = None
    fallback_method: str = "api_key"
    api_key_hint: str = ""
    auth_kind: str = "bearer"
    auth_param: Optional[str] = None
    scopes: list[str] = field(default_factory=list)
    env_map: dict[str, str] = field(default_factory=dict)

    @property
    def methods(self) -> list[str]:
        if self.prefer == "mcp_oauth":
            methods = {"mcp_oauth", self.fallback_method or "api_key"}
        elif self.prefer == "provider_oauth":
            methods = {"provider_oauth", "api_key"}
        else:
            methods = {"api_key"}
        return sorted(methods)

    def can(self, method: str) -> bool:
        return method in self.methods

    def public_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name or self.slug,
            "prefer": self.prefer,
            "methods": self.methods,
            "api_key_hint": self.api_key_hint or "",
        }


_RAW: Optional[dict] = None


def _load() -> dict:
    global _RAW
    if _RAW is None:
        _RAW = json.loads(CAPS_JSON.read_text(encoding="utf-8"))
    return _RAW


def reload() -> None:
    """Discount the in-memory catalog (tests)."""
    global _RAW
    _RAW = None


def get_capability(slug: str) -> Optional[OAuthCapability]:
    entry = _load().get(slug)
    if not entry:
        return None
    return OAuthCapability(
        slug=slug,
        prefer=entry.get("prefer", "api_key"),
        name=entry.get("name", slug),
        provider=entry.get("provider"),
        resource=entry.get("resource"),
        as_metadata_url=entry.get("as_metadata_url"),
        oauth_server_url=entry.get("oauth_server_url") or entry.get("server_url"),
        server_url=entry.get("server_url"),
        fallback_method=entry.get("fallback_method", "api_key"),
        api_key_hint=entry.get("api_key_hint", ""),
        auth_kind=entry.get("auth_kind", "bearer"),
        auth_param=entry.get("auth_param"),
        scopes=list(entry.get("scopes", [])),
        env_map=dict(entry.get("env_map", {})),
    )


def all_capabilities() -> dict[str, OAuthCapability]:
    return {slug: get_capability(slug) for slug in _load()}


def slug_for_tool(tool) -> Optional[OAuthCapability]:
    """Resolve a CatalogTool to its connect capability by slug."""
    slug = getattr(tool, "slug", "") or getattr(tool, "provider", "")
    return get_capability(slug) or get_capability(f"{slug}-mcp") or get_capability(f"{slug}-stdio")