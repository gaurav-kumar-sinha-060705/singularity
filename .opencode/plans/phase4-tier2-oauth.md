# Phase 4 — Hosted MCP Execution (Tier 2) + OAuth

Status: PLAN (build in progress)
Companion: `ROADMAP.md` §3 (three-tier execution model), §6 Phase 4.

## Context / constraint

A hosted gateway (Render) can only reach MCP servers over HTTP. stdio servers are
local processes — unreachable from the cloud. Three-tier model:
- Tier 1 (DONE): keyless REST providers (weather, npms_lookup, pypi_lookup, web_search).
- Tier 2 (THIS PHASE): proxy vetted hosted `streamable-http` remotes as an MCP client.
- Tier 3: stdio-only servers = recommend + user connects locally (never executed remotely).

Registry verified facts:
- `https://registry.modelcontextprotocol.io/v0/servers?search=<name>` returns
  `servers[].server.remotes[]` with `type: streamable-http` + `url` for hosted servers;
  `packages[].transport.type` is `stdio` for local ones.
- Hosted examples: Stripe `https://mcp.stripe.com` (official), Notion
  `https://mcp.notion.com/mcp` (official), Smithery `https://server.smithery.ai/…`,
  McParMory `https://mcp.mcparmory.com/github`, Pipeworx
  `https://gateway.pipeworx.io/firecrawl/mcp`, WayStation `https://waystation.ai/slack/mcp`.
- SDK availability (installed): `mcp.client.streamable_http.streamable_http_client`
  and `mcp.ClientSession` with `initialize()`, `list_tools()`, `call_tool()`.

## Milestone 4.0 — Remote MCP provider adapter (first cut)

1. **Enrichment script** (`scripts/enrich_remotes.py`): for each curated slug, query a
   pre-vetted set of hosts (official registry + known proxies) and record a
   `managed_remote[]` on the tool row: `{type, url, host_gate, auth_note}` when a
   hosted `streamable-http` variant exists. Stdio-only → no remote → stays advisory.
   Idempotent; runs at seed/boot sync (offline, retries, cached), never in request path.
2. **Schema**: `Tool` gains `hosted_variant_json` (JSONB, nullable) +
   `executable` derived (provider OR hosted remote AND trust >= threshold AND scan clean).
3. **`RemoteMcpProvider`** in `app/providers/` implementing the same `Provider` ABC:
   - `execute()` → `streamable_http_client(url)` → `ClientSession.list_tools()` (cached
     per remote, short TTL) → `call_tool(name, arguments)` → returns
     `content[{type:text}]`-style result normalized like Tier-1 providers.
   - Headers: auth template placeholder (Phase 4.5 fills it from vault).
4. **Safety guards (blockers if not met):**
   - SSRF: connect only to allowlisted hostnames from seed data. No arbitrary
     user-supplied URL reaches `streamable_http_client`.
   - Trust gate unchanged: `trust_score >= execute_min_trust`, scan-clean required.
   - Timeouts + `follow_redirects = False`; audit every remote call via shared helper.
5. **MCP surface**: `list_public_tools()` merges hosted-remote tools (name collision
   policy: `hosted:<tool_slug>`); `call_tool` routes provider-backed vs remote-backed
   through the same enforcer (scope = `public:read` until vault exists).
6. **Tests**: fake `streamable_http_client` (in-process stub) to assert
   allowlist-denial (SSRF), trust-denial, tool listing merge, audit row.

## Milestone 4.5 — Credential vault + one-click OAuth

- OAuth PKCE flows (Stripe, GitHub, Slack, Notion, Google) → encrypted token store
  (Fernet/age-style envelope; key from env `SINGULARITY_VAULT_KEY`).
- `connections` table; gateway attaches `Authorization`/token to remote calls from
  the user's active connection. Scope enforcement per connection on every call.
- No raw API keys persisted in plaintext; short-TTL capability tokens.

## Decisions needed from user

- [ ] Which hosted remotes to seed first for the 15 typed-index tools
      (official-first, proxy hosts second)?
- [ ] Auth template for remote calls before vault: none (public remotes only) vs
      env-provided shared keys?
- [ ] Approval to run `scripts/enrich_remotes.py` hits against the registry API
      (public, read-only).

## Acceptance (Milestone 4.0)

- `list_public_tools` returns at least the keyless-4 + every curated tool that has a
  hosted variant; others show `hosted_variant: null`.
- A Vega-hosted Stripe-equivalent (stubbed) executes through `call_tool` under audit.
- Arbitrary URL in arguments is refused (SSRF test) with audit denial.
- 55+ pytest green; live smoke on Render after deploy.