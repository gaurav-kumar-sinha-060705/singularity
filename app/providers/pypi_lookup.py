from urllib.parse import quote

from app.providers.base import Provider, ProviderError, http_get_json

PYPI_URL = "https://pypi.org/pypi/{}/json"


class PypiLookupProvider(Provider):
    slug = "pypi_lookup"
    name = "PyPI Package Lookup"
    version = "1.0.0"
    category = "developer-tools"
    description = (
        "Resolve any package on PyPI: latest version, summary, Python requirement, "
        "license, project links, and release history from the official PyPI JSON "
        "API. Keyless and read-only."
    )
    scopes = {"public:read"}

    def validate(self, args: dict) -> dict:
        package = str(args.get("package") or "").strip()
        if not package:
            raise ProviderError("package is required (PyPI project name)")
        if len(package) > 200:
            raise ProviderError("package name is too long")
        return {"package": package}

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        body = http_get_json(PYPI_URL.format(quote(args["package"])))
        info = body.get("info") or {}
        releases = body.get("releases") or {}
        return {
            "name": info.get("name") or args["package"],
            "version": info.get("version"),
            "summary": info.get("summary"),
            "requires_python": info.get("requires_python"),
            "license": info.get("license_expression") or info.get("license"),
            "homepage": info.get("home_page"),
            "project_urls": info.get("project_urls") or {},
            "num_versions": len(releases),
            "source": "PyPI JSON API",
        }


provider = PypiLookupProvider()
