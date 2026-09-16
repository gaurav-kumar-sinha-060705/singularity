from app.providers import npms_lookup, pypi_lookup, weather, web_search
from app.providers.base import Provider
from app.providers.remote import RemoteMcpProvider

# Tier 2 — vetted hosted MCP remotes (streamable-http) that this gateway can
# proxy as an MCP client. Each entry is curated at seed time (allowlist + vet):
# never derived from user input. auth_required=True until Phase 4.5 provides a
# credential vault, so listing is advisory and execute() refuses with a reason.
REMOTE_CATALOG: list[RemoteMcpProvider] = [
    RemoteMcpProvider(
        slug="stripe-mcp",
        name="Stripe (hosted MCP)",
        remote_url="https://mcp.stripe.com",
        description=(
            "Stripe's official hosted MCP server: look up charges, customers, "
            "and payment links. Proxied by Singularity as an MCP client. "
            "Requires a Stripe credential (Phase 4.5 vault)."
        ),
        category="finance",
        auth_required=True,
    ),
]

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