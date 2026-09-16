import json
from pathlib import Path

from app.providers import npms_lookup, pypi_lookup, weather, web_search
from app.providers.base import Provider
from app.providers.remote import RemoteMcpProvider

_HOSTED_REMOTES_PATH = Path(__file__).resolve().parents[2] / "data" / "hosted_remotes.json"


def _load_remote_catalog() -> list[RemoteMcpProvider]:
    """Load curated hosted remotes from data/hosted_remotes.json at boot."""
    if not _HOSTED_REMOTES_PATH.exists():
        return []
    data = json.loads(_HOSTED_REMOTES_PATH.read_text(encoding="utf-8"))
    providers = []
    for entry in data.values():
        providers.append(RemoteMcpProvider(
            slug=entry["slug"],
            name=entry.get("server_name", entry["slug"]),
            remote_url=entry["remote_url"],
            description=entry.get("server_description", ""),
            category="hosted",
            auth_required=entry.get("auth_required", True),
        ))
    return providers


# Tier 2 — vetted hosted MCP remotes (streamable-http) loaded from curated asset.
REMOTE_CATALOG: list[RemoteMcpProvider] = _load_remote_catalog()

_PROVIDERS: dict[str, Provider] = {
    p.slug: p
    for p in (
        weather.provider,
        npms_lookup.provider,
        pypi_lookup.provider,
        web_search.provider,
    )
}

_REMOTE_PROVIDERS: dict[str, RemoteMcpProvider] = {
    p.slug: p for p in REMOTE_CATALOG
}

_ALL = {**_PROVIDERS, **_REMOTE_PROVIDERS}


def get_provider(slug: str) -> Provider | None:
    return _ALL.get(slug)


def list_public_tools() -> list[dict]:
    tools = [
        {
            "slug": p.slug,
            "name": p.name,
            "version": p.version,
            "category": p.category,
            "description": p.description,
            "scopes": sorted(p.scopes),
        }
        for p in sorted(_PROVIDERS.values(), key=lambda p: p.slug)
    ]
    remotes = [
        {
            "slug": p.slug,
            "name": p.name,
            "version": p.version,
            "category": p.category,
            "description": p.description,
            "scopes": sorted(p.scopes),
            "hosted": True,
            "remote_url": p.remote_url,
            "auth_required": p.requires_auth,
        }
        for p in sorted(_REMOTE_PROVIDERS.values(), key=lambda p: p.slug)
    ]
    return tools + remotes