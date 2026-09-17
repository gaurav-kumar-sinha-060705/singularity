import json
import shutil
from pathlib import Path

from app.providers import github, npms_lookup, pypi_lookup, slack_mcp, weather, web_search
from app.providers.base import Provider
from app.providers.remote import RemoteMcpProvider
from app.providers.stdio import ALLOWLISTED_STDIO, StdioMcpProvider

_HOSTED_REMOTES_PATH = Path(__file__).resolve().parents[2] / "data" / "hosted_remotes.json"


def _load_remote_catalog() -> list[RemoteMcpProvider]:
    """Load curated hosted remotes from data/hosted_remotes.json at boot."""
    if not _HOSTED_REMOTES_PATH.exists():
        return []
    data = json.loads(_HOSTED_REMOTES_PATH.read_text(encoding="utf-8"))
    providers = []
    for entry in data.values():
        if entry.get("deprecated"):
            continue
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
        github.provider,
        slack_mcp.provider,
    )
}

_REMOTE_PROVIDERS: dict[str, RemoteMcpProvider] = {
    p.slug: p for p in REMOTE_CATALOG
}


def _load_stdio_providers() -> dict[str, StdioMcpProvider]:
    """Tier-3 stdio bridges. Only enabled when the runtime command exists so an
    unprovisioned deployment doesn't advertise tools it can't actually run."""
    providers: dict[str, StdioMcpProvider] = {}
    for spec in ALLOWLISTED_STDIO:
        if shutil.which(spec["command"]):
            providers[spec["slug"]] = StdioMcpProvider(spec)
    return providers


_STDIO_PROVIDERS: dict[str, StdioMcpProvider] = _load_stdio_providers()

_ALL = {**_PROVIDERS, **_REMOTE_PROVIDERS, **_STDIO_PROVIDERS}


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
    bridges = [
        {
            "slug": p.slug,
            "name": p.name,
            "version": p.version,
            "category": p.category,
            "description": p.description,
            "scopes": sorted(p.scopes),
            "hosted": True,
            "bridge": "stdio",
            "auth_required": p.requires_auth,
        }
        for p in sorted(_STDIO_PROVIDERS.values(), key=lambda p: p.slug)
    ]
    return tools + remotes + bridges