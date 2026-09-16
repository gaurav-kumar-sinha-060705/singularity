from urllib.parse import quote

from app.providers.base import Provider, ProviderError, http_get_json

NPM_URL = "https://api.npms.io/v2/package/{}"


class NpmsLookupProvider(Provider):
    slug = "npms_lookup"
    name = "npm Package Lookup (npms.io)"
    version = "1.0.0"
    category = "developer-tools"
    description = (
        "Look up an npm package by name: published version, description, license, "
        "links, top maintainers, and an overall quality/popularity/maintenance "
        "score from npms.io. Keyless and read-only."
    )
    scopes = {"public:read"}

    def validate(self, args: dict) -> dict:
        package = str(args.get("package") or "").strip()
        if not package:
            raise ProviderError("package is required (npm package name)")
        if len(package) > 214:
            raise ProviderError("package name is too long")
        return {"package": package}

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        body = http_get_json(NPM_URL.format(quote(args["package"])))
        collected = body.get("collected") or {}
        metadata = collected.get("metadata") or {}
        links = metadata.get("links") or {}
        score = body.get("score") or {}
        detail = score.get("detail") or {}
        return {
            "name": metadata.get("name") or args["package"],
            "version": metadata.get("version"),
            "description": metadata.get("description"),
            "license": metadata.get("license"),
            "homepage": links.get("homepage"),
            "repository": links.get("repository"),
            "maintainers": [m.get("username") for m in metadata.get("maintainers", []) if m.get("username")],
            "score_final": round(score.get("final") or 0, 3),
            "score_quality": round(detail.get("quality") or 0, 3),
            "score_popularity": round(detail.get("popularity") or 0, 3),
            "score_maintenance": round(detail.get("maintenance") or 0, 3),
            "source": "npms.io",
        }


provider = NpmsLookupProvider()
