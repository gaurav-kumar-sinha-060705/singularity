from app.providers import npms_lookup, pypi_lookup, weather, web_search
from app.providers.base import Provider

_PROVIDERS: dict[str, Provider] = {
    p.slug: p
    for p in (
        weather.provider,
        npms_lookup.provider,
        pypi_lookup.provider,
        web_search.provider,
    )
}


def get_provider(slug: str) -> Provider | None:
    return _PROVIDERS.get(slug)


def list_public_tools() -> list[dict]:
    return [
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