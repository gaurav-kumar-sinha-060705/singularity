"""First-party execution providers.

Providers are the only things the ExecGateway will ever execute. Every provider
here is:

  - first-party (publisher = "Singularity", publisher_verified=True),
  - keyless (public read-only APIs, no secrets),
  - scoped to a small allowlist (default: only "public:read").

Each provider subclasses Provider and lives in its own module. `execute` is the
single network-touching path; `validate` runs first and raises ProviderError on
bad args; both are pure-vs-network so tests can mock http_get_json only.
"""

import time

import httpx

from app.config import get_settings


class ProviderError(Exception):
    """User-facing error: bad args, upstream miss, or unsupported scope."""


class Provider:
    slug: str = ""
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    category: str = ""
    scopes: set[str] = {"public:read"}

    def validate(self, args: dict) -> dict:
        return args

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        raise NotImplementedError

    def run(self, args: dict, scope: str, credential: dict | None = None) -> dict:
        """Gate + run one call. Never raises; downstream failures become
        {"ok": False, "error": ...} outcomes so the gateway can audit them.

        `credential` is the decrypted per-user secret (None for Tier-1 keyless
        providers / unauthenticated calls)."""
        t0 = time.monotonic()
        try:
            if scope not in self.scopes:
                raise ProviderError(f"scope '{scope}' is not granted to '{self.slug}'")
            validated = self.validate(args)
            result = self.execute(validated, credential)
            return {
                "ok": True,
                "name": self.slug,
                "version": self.version,
                "result": result,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except ProviderError as exc:
            return {
                "ok": False,
                "name": self.slug,
                "version": self.version,
                "error": str(exc),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except Exception as exc:  # upstream/network failure — audit, don't crash
            return {
                "ok": False,
                "name": self.slug,
                "version": self.version,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except BaseExceptionGroup as exc:  # anyio wraps SDK failures in a group
            detail = exc
            while isinstance(detail, BaseExceptionGroup):
                detail = detail.exceptions[0] if detail.exceptions else detail
            return {
                "ok": False,
                "name": self.slug,
                "version": self.version,
                "error": f"{type(detail).__name__}: {detail}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }


def http_get_json(url: str, params: dict | None = None,
                  timeout: float | None = None) -> dict:
    """Single HTTP entrypoint shared by all providers — easy to mock in tests."""
    settings = get_settings()
    resp = httpx.get(url, params=params,
                     timeout=timeout or settings.provider_http_timeout,
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.json()