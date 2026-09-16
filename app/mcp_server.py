"""Singularity exposed as a native MCP server.

Three agent-facing tools over streamable HTTP at /mcp:
  - find_solutions(problem)      -> ranked, trust-scored recommendations
  - get_trust_report(slug)       -> full security card for one tool
  - compare_tools(slugs[])       -> side-by-side trust table

Singularity recommends, connects, and gates execution through a secure gateway.
"""

from typing import Annotated, Any
import os

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.database import SessionLocal
from app.mcp_auth import current_user_id as mcp_current_user_id
from app.mcp_auth import resolve_access_user, reset_user, scope_user
from app.models import Tool, log_event
from app.providers import registry
from app.services.discovery_engine import parse_intent
from app.services.embeddings import embed_query
from app.services.gateway import execute_via_gateway
from app.services.ranking import FLAG_EXPLANATIONS, rank_candidates
from app.services.recommendation_index import PRICING_ORDER, index

mcp = MCPServer(
    name="singularity",
    title="Singularity - MCP Discovery & Security Gateway",
    description=(
        "Finds the best-fit software platform or MCP-connectable tool for a user's "
        "problem and vets every candidate for tool-poisoning / prompt-injection risk "
        "before an agent relies on it."
    ),
    instructions=(
        "Use find_solutions when the user describes a task or need in natural language "
        "and you want to know which tool/platform fits best. Every result carries a fit "
        "score (semantic match), trust score (security vetting), and flags. Read flags "
        "aloud to the user before they adopt a tool. Use get_trust_report to inspect one "
        "candidate deeply, compare_tools to shortlist. Singularity only advises on "
        "selection - execution is gated through its secure gateway."
    ),
)

TRUST_BANDS = ((0.85, "strong"), (0.60, "acceptable"), (-1.0, "weak"))


def _trust_band(score: float) -> str:
    for threshold, label in TRUST_BANDS:
        if score >= threshold:
            return label
    return "weak"


def _flags_block(flags: list[str], indent: str = "   ") -> str:
    if not flags:
        return f"{indent}Flags: none"
    lines = [f"{indent}Flags: {len(flags)}"]
    for flag in flags:
        reason = FLAG_EXPLANATIONS.get(flag, flag)
        lines.append(f"{indent}  - {flag}: {reason}")
    return "\n".join(lines)


def _format_recommendation(pos: int, item, fit: float, rank: float, rationale: str) -> str:
    head = (
        f"{pos}. **{item.name}** (`{item.slug}`) - fit {fit:.2f} | "
        f"trust {item.trust_score:.2f} ({_trust_band(item.trust_score)}) | rank {rank:.2f}"
    )
    meta = f"   {item.publisher}{' [verified]' if item.publisher_verified else ''} | {item.category} | {item.pricing_tier} pricing"
    return "\n".join([head, meta, _flags_block(item.trust_flags), f"   Why: {rationale}"])


@mcp.tool(
    description=(
        "Given a user's problem in natural language, return ranked solution "
        "recommendations (software platforms and MCP-connectable tools). Each result "
        "includes a semantic fit score, a trust/security score, any security flags, and "
        "a short rationale. Surface the flags to the user transparently."
    ),
)
def find_solutions(
    problem: Annotated[str, Field(min_length=3, max_length=1000,
                                  description="The user's problem or need, in their own words.")],
    top_k: Annotated[int, Field(ge=1, le=10, description="How many recommendations to return.")] = 5,
    max_pricing_tier: Annotated[str | None, Field(
        description="Optional ceiling: free, freemium, paid, or enterprise.")] = None,
) -> str:
    if max_pricing_tier is not None and max_pricing_tier not in PRICING_ORDER:
        return f"Unknown pricing tier '{max_pricing_tier}'. Use one of: {', '.join(PRICING_ORDER)}."

    intent = parse_intent(problem)
    query_embedding = embed_query(problem)
    hits = index.search(query_embedding, max_pricing_tier=max_pricing_tier,
                        require_mcp=True, limit=top_k * 3)

    ranked = rank_candidates(hits, intent["category_hint"])[:top_k] if hits else []

    db = SessionLocal()
    try:
        log_event(db, "recommendation_served", channel="mcp", problem=problem,
                  returned=[tool.slug for tool, *_ in ranked],
                  top_rank=ranked[0][2] if ranked else None)
        db.commit()
    finally:
        db.close()

    if not ranked:
        intent_line = f"Detected intent: {intent['category_hint'] or 'general'}"
        return (
            f"No strong matches found.\n{intent_line}\n\n"
            "The tool index is currently limited (Phase 1). "
            "Try broadening your query or check back as more tools are indexed."
        )

    intent_line = f"Detected intent: {intent['category_hint'] or 'general'}"
    if intent["matched_keywords"]:
        intent_line += f" (keywords: {', '.join(intent['matched_keywords'])})"
    body = "\n\n".join(
        _format_recommendation(i, item, fit, rank, rationale)
        for i, (item, fit, rank, rationale) in enumerate(ranked, start=1)
    )
    legend = (
        "Scores: fit = semantic match to your problem (set-relative); trust = security "
        "vetting quality; rank = weighted blend penalized by flags. Singularity advises "
        "on selection - approved calls execute through the gateway under audit."
    )
    return f"### Top solutions\n{intent_line}\n\n{body}\n\n{legend}"


@mcp.tool(
    description=(
        "Get a detailed trust and security report for one indexed tool by slug: "
        "publisher verification, permissions requested vs actually needed (with the "
        "overreach highlighted), injection-scan verdicts on the tool description, and "
        "the trust score interpretation."
    ),
)
def get_trust_report(
    slug: Annotated[str, Field(description="Tool slug, e.g. 'github-mcp'.")],
) -> str:
    db = SessionLocal()
    try:
        tool = db.query(Tool).filter(Tool.slug == slug).first()
    finally:
        db.close()
    if not tool:
        known = ", ".join(t.slug for t in index._tools) or "(index empty)"
        return (f"No tool indexed under slug '{slug}'. Known slugs: {known}. "
                f"Use find_solutions to discover valid slugs.")

    verified = "yes" if tool.publisher_verified else "NO"
    lines = [
        f"## Trust report: {tool.name} (`{tool.slug}`)",
        f"- Publisher: {tool.publisher} - verified: {verified}",
        f"- Category: {tool.category} | Pricing: {tool.pricing_tier} | "
        f"MCP-capable: {'yes' if tool.mcp_available else 'no'}",
        f"- Trust score: {tool.trust_score:.2f}/1.00 ({_trust_band(tool.trust_score)})",
        "",
        "**Permissions requested:** " + (", ".join(tool.permissions_requested) or "none"),
        "**Permissions actually needed:** " + (", ".join(tool.permissions_needed) or "none"),
    ]
    overreach = sorted(set(tool.permissions_requested) - set(tool.permissions_needed))
    if overreach:
        lines.append(f"- Overreach (requested beyond stated purpose): {', '.join(overreach)}")
    lines += ["", _flags_block(tool.trust_flags, indent="")]
    excerpt = tool.description[:280] + ("..." if len(tool.description) > 280 else "")
    lines += ["", f'Description scanned: "{excerpt}"']
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Compare several indexed tools side by side on trust signals: trust score, "
        "security flags, publisher verification, category, and pricing tier. Pass 2-6 "
        "slugs. Fit depends on the specific problem - call find_solutions for ranking."
    ),
)
def compare_tools(
    slugs: Annotated[list[str], Field(min_length=2, max_length=6,
                                      description="Tool slugs to compare.")],
) -> str:
    db = SessionLocal()
    try:
        tools = {t.slug: t for t in db.query(Tool).filter(Tool.slug.in_(slugs)).all()}
    finally:
        db.close()

    missing = [s for s in slugs if s not in tools]
    rows = []
    for s in slugs:
        t = tools.get(s)
        if not t:
            continue
        flags = ", ".join(t.trust_flags) if t.trust_flags else "none"
        verified = "yes" if t.publisher_verified else "no"
        rows.append(
            f"| `{t.slug}` | {t.name} | {t.trust_score:.2f} ({_trust_band(t.trust_score)}) "
            f"| {verified} | {t.category} | {t.pricing_tier} | {flags} |"
        )
    if len(rows) < 2:
        return ("Could not find at least two of those slugs. "
                + (f"Unknown: {', '.join(missing)}. " if missing else "")
                + "Use find_solutions to discover valid slugs.")

    note = f"\n\nNot found: {', '.join(missing)}" if missing else ""
    header = (
        "| slug | name | trust | verified pub. | category | pricing | flags |\n"
        "|---|---|---|---|---|---|---|"
    )
    return f"### Comparison\n{header}\n" + "\n".join(rows) + note


@mcp.tool(
    description=(
        "List the public tools that can be executed through the Singularity "
        "gateway. Each entry shows its slug, category, granted scopes, and "
        "whether it is a hosted remote (auth_required). Use list_public_tools "
        "to discover what call_tool can run."
    ),
)
def list_public_tools() -> str:
    tools = registry.list_public_tools()
    if not tools:
        return "### Executable public tools\nnone"
    lines = []
    for t in tools:
        extra = " — hosted remote (auth required)" if t.get("hosted") and t.get("auth_required") else ""
        lines.append(
            f"- `{t['slug']}` — {t['name']} — {t['category']} — "
            f"scopes: {', '.join(t['scopes'])}{extra}"
        )
    return (
        "### Executable public tools\n" + "\n".join(lines) + "\n\n"
        "Run one with call_tool(provider_slug=\"<slug>\", arguments={...}, scope=\"public:read\")."
    )


@mcp.tool(
    description=(
        "Execute a tool through the audited Singularity gateway. First-party "
        "keyless tools: weather (current conditions for a city), npms_lookup "
        "(npm package info), pypi_lookup (PyPI package info), web_search "
        "(DuckDuckGo instant answers). Hosted remotes (e.g. stripe-mcp) are "
        "listed but require a credential connection (Phase 4.5) and currently "
        "return a refused reason. The call is scope-checked, trust-checked, "
        "and written to the audit log; denied or failed calls return the "
        "reason. Use list_public_tools to see what's available, and "
        "find_solutions to be told which slugs fit a problem."
    ),
)
def call_tool(
    provider_slug: Annotated[str, Field(
        description="Provider slug to execute, e.g. 'weather'.")],
    arguments: Annotated[dict[str, Any] | None, Field(
        description="Arguments for the provider, e.g. {\"location\": \"London\"}.")] = None,
    scope: Annotated[str | None, Field(
        description="Scope to request. Defaults to 'public:read'.")] = None,
) -> str:
    out = execute_via_gateway(
        provider_slug,
        arguments or {},
        scope=scope,
        channel="mcp",
        user_id=mcp_current_user_id(),
    )

    if out["decision"] == "not_found":
        known = ", ".join(p["slug"] for p in registry.list_public_tools()) or "(none)"
        return (
            f"Could not execute '{provider_slug}': {out['reason']}. "
            f"Executable providers: {known}. Use list_public_tools to see them."
        )
    if out["decision"] == "denied":
        return f"Execution denied for '{provider_slug}': {out['reason']}."
    if out["decision"] == "disabled":
        return f"Execution unavailable: {out['reason']}."
    if out["decision"] == "failed":
        return f"Execution of '{provider_slug}' failed: {out['reason']}."

    result = out.get("result") or {}
    lines = [f"### {provider_slug} — executed through the audited gateway (scope {out['scope']})"]
    for key, value in result.items():
        if isinstance(value, (dict, list)):
            import json as _json
            lines.append(f"- **{key}:** {_json.dumps(value)[:400]}")
        else:
            lines.append(f"- **{key}:** {value}")
    lines.append(f"- **version:** {out.get('version')}")
    lines.append(f"- **latency_ms:** {out.get('latency_ms')}")
    lines.append("Call recorded in the audit log (decision=allowed).")
    return "\n".join(lines)


def _configured_allowed_hosts() -> list[str]:
    """Hosts allowed by config plus, on Render, the injected external URL's host."""
    from urllib.parse import urlparse

    from app.config import get_settings, parse_allowed_hosts

    hosts = parse_allowed_hosts(get_settings().mcp_allowed_hosts)
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if external_url:
        hostname = urlparse(external_url).hostname
        if hostname and hostname not in hosts:
            hosts.append(hostname)
    return hosts


def build_mcp_asgi_app():
    """Streamable-HTTP ASGI sub-app served at /mcp (stateless, JSON mode).

    DNS-rebinding protection stays ON; the allowlist comes from settings plus,
    when deployed on Render, the platform-injected external URL.

    Wrapped by an auth-aware middleware: a valid Bearer access token on the
    request resolves the user (auto-provisioning the row on first use) and
    publishes user_id on a contextvar consumed by call_tool.
    """
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_configured_allowed_hosts(),
            allowed_origins=["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"],
        ),
    )

    async def _auth_middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = dict((k.decode("latin-1").lower(), v.decode("latin-1"))
                       for k, v in scope.get("headers", []))
        user_id = resolve_access_user(headers.get("authorization"))
        ctx = scope_user(user_id)
        try:
            await app(scope, receive, send)
        finally:
            reset_user(ctx)

    # Keep the underlying app's router reachable (main.py lifespan uses it).
    _auth_middleware.router = getattr(app, "router", None)
    return _auth_middleware
