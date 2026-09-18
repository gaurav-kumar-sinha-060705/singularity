"""Credential injection adapter: turn a vault credential + capability into the
runtime auth the provider needs.

  - tier1 REST / tier2 bearer remotes  -> Authorization: Bearer <token>
  - tier2 query remotes (Browserbase)  -> ?auth_param=<token>
  - tier2 custom-header remotes        -> <auth_header>: <token>
  - tier3 stdio bridges                -> env map (python dotenv format)

`inject(cap, credential)` returns an opaque plan; providers (remote.py, the
stdio launcher, connections verify) consume `build_headers` / `build_env`.
"""

from __future__ import annotations

import re

from app.services.oauth_caps import OAuthCapability

_ENV_NAME_RE = re.compile(r"[^A-Z0-9_]+")


def build_headers(cap: OAuthCapability, credential: dict) -> dict[str, str]:
    """HTTP auth headers for tier1/tier2 remote calls. Empty dict when auth is
    conveyed elsewhere (query string) or not applicable."""
    if cap.auth_kind != "bearer":
        return {}
    token = credential.get("access_token") or credential.get("api_key")
    if not token:
        return {}
    header = cap.auth_param or "Authorization"
    if header.lower() == "authorization":
        return {"Authorization": f"Bearer {token}"}
    return {header: token}


def build_url(cap: OAuthCapability, url: str, credential: dict) -> str:
    """Attach the query-string credential for auth_kind == 'query' (Browserbase)."""
    if cap.auth_kind != "query" or not cap.auth_param:
        return url
    token = credential.get("access_token") or credential.get("api_key")
    if not token:
        return url
    from urllib.parse import urlencode

    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode({cap.auth_param: token})}"


def build_env(cap: OAuthCapability, credential: dict) -> dict[str, str]:
    """Env vars for tier3 stdio bridges, mapped through the capability env_map
    (e.g. GOOGLE_DRIVE_ACCESS_TOKEN / GOOGLE_DRIVE_REFRESH_TOKEN)."""
    result: dict[str, str] = {}
    for key, env_name in cap.env_map.items():
        value = credential.get(key)
        if value is not None:
            result[env_name] = value
    return result


def pick_credential(cap: OAuthCapability, credential: dict) -> str | None:
    """The plain token to present (access_token preferred, api_key fallback)."""
    return credential.get("access_token") or credential.get("api_key") or None