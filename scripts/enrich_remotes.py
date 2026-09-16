"""Enrich our catalog tools with hosted MCP remotes from the official registry.

For each tool slug in our catalog, query the registry API for hosted
streamable-http variants (official or third-party proxies) and write the
allowlisted set to `data/hosted_remotes.json` — a curated asset loaded
by the registry at boot (no DB migration required).

Usage:
    python scripts/enrich_remotes.py          # produces data/hosted_remotes.json
    python scripts/enrich_remotes.py --dry-run  # print candidates without writing
"""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_OUT_PATH = _DATA_DIR / "hosted_remotes.json"

# Curated mapping: slug → (search terms, brand keywords to match in server name)
# Keywords are checked case-insensitively; the FIRST result matching ALL keywords
# is treated as the candidate. Official-publisher results are ranked first.
CATALOG = {
    "github-mcp":     {"search": "github",      "brand": "github",      "prefer": ["com.mcparmory", "io.github"]},
    "slack-mcp":      {"search": "slack",       "brand": "slack",       "prefer": ["ai.waystation", "io.github"]},
    "notion-mcp":     {"search": "notion",      "brand": "notion",      "prefer": ["com.notion"]},
    "stripe-mcp":     {"search": "stripe",      "brand": "stripe",      "prefer": ["com.stripe"]},
    "sentry-mcp":     {"search": "sentry",      "brand": "sentry",      "prefer": ["com.sentry", "io.github"]},
    "postgres-mcp":   {"search": "postgres",    "brand": "postgres",    "prefer": []},
    "filesystem-mcp": {"search": "filesystem",  "brand": "filesystem",  "prefer": []},
    "browserbase-mcp":{"search": "browserbase", "brand": "browserbase", "prefer": []},
    "google-drive-mcp":{"search": "google drive","brand": "google",     "prefer": ["com.google"]},
    "jira-mcp":       {"search": "jira",        "brand": "jira",        "prefer": ["com.atlassian"]},
    "linear-mcp":     {"search": "linear",      "brand": "linear",      "prefer": []},
    "expensify-mcp":  {"search": "expensify",   "brand": "expensify",   "prefer": []},
    "zapier-mcp":     {"search": "zapier",      "brand": "zapier",      "prefer": ["com.zapier"]},
    "supabase-mcp":   {"search": "supabase",    "brand": "supabase",    "prefer": ["com.supabase"]},
}

TIMEOUT = 20


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _matches(server: dict, brand_keywords: list[str]) -> bool:
    name = server.get("name", "").lower()
    return all(kw in name for kw in brand_keywords)


def _is_hosted(server: dict) -> list[str]:
    """Return streamable-http URLs from a server entry."""
    remotes = server.get("remotes", [])
    return [
        r["url"]
        for r in remotes
        if r.get("type") == "streamable-http" and "url" in r
    ]


def _auth_required_heuristic(name: str, description: str) -> bool:
    """Conservative default: almost all hosted remotes require tokens."""
    return True


def enrich(slug: str, search: str, brand: str, prefer: list[str], dry_run: bool) -> dict | None:
    """Query the registry and return a curated remote entry for one slug."""
    try:
        resp = httpx.get(
            "https://registry.modelcontextprotocol.io/v0/servers",
            params={"search": search},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [{slug}] registry query failed: {exc}", file=sys.stderr)
        return None

    servers = resp.json().get("servers", [])
    candidates = []
    for s in servers:
        srv = s.get("server", {})
        name = (srv.get("name") or "").lower()
        if brand not in name:
            continue
        urls = _is_hosted(srv)
        if not urls:
            continue
        for url in urls:
            host = _host(url)
            priority = 0
            for i, prefix in enumerate(prefer):
                if prefix and prefix in srv.get("name", ""):
                    priority = i
                    break
            else:
                priority = 100  # unpreferenced
            candidates.append({
                "slug": slug,
                "remote_url": url,
                "host": host,
                "server_name": srv.get("name", ""),
                "server_description": (srv.get("description") or "")[:200],
                "auth_required": _auth_required_heuristic(srv.get("name", ""), srv.get("description") or ""),
                "priority": priority,
            })

    if not candidates:
        print(f"  [{slug}] no hosted remotes found", file=sys.stderr)
        return None

    candidates.sort(key=lambda c: c["priority"])
    pick = candidates[0]
    pick.pop("priority", None)
    print(f"  [{slug}] -> {pick['remote_url']}")
    return pick


def main():
    dry_run = "--dry-run" in sys.argv
    results = {}
    for slug, cfg in CATALOG.items():
        hit = enrich(slug, cfg["search"], cfg["brand"], cfg["prefer"], dry_run)
        if hit:
            results[slug] = hit
        time.sleep(0.4)  # be polite to the registry
    if dry_run:
        print(json.dumps(results, indent=2))
        return
    _OUT_PATH.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {len(results)} entries to {_OUT_PATH}")


if __name__ == "__main__":
    main()