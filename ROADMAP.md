# Singularity — Build Roadmap

> Companion to `../Singularity_Blueprint.md`.
> This document is the executable plan: what we've shipped, what we build now, next,
> and after that — with acceptance criteria so "done" is never ambiguous.

---

## 1. Program Principles (non-negotiable)

1. **Singularity recommends, connects, and gates — it never hands over keys to unvetted tools.**
   Every call runs through the gateway under audit.
2. **Transparency over silent filtering.** Low-trust tools are labeled, not hidden.
   Every score carries its reasons (`flags`, `rationale`).
3. **Auditable by default.** Score changes, ingests, and served recommendations are
   logged with context. A trust rating is an argument, not a number.
4. **Runnable beats perfect.** Each phase ends with something you can demo live.

---

## 2. Where We Are Today

| Asset | Status |
|---|---|
| Blueprint document | ✅ `Singularity_Blueprint.md` (merged discovery + execution blueprints) |
| Phase 0 decision: stack locked | ✅ Python 3.11 + FastAPI + SQLAlchemy + fastembed (ONNX, no torch) |
| Phase 1 + 1.5 code | ✅ Curated recommender + native MCP server (`app/`) |
| Phase 2 rename (Compass → Singularity) | ✅ Complete: package `app/`, env `SINGULARITY_*`, repo `singularity`, zero `compass` traces in code |
| Live deployment | ✅ Render + Supabase (`https://singularity-osd2.onrender.com`), `/health` → `indexed_tools: 19`; MCP at `/mcp` (`find_solutions`, `get_trust_report`, `compare_tools`, `list_public_tools`, `call_tool`) |
| Database hardening | ✅ Postgres advisory lock + deterministic uuid5 IDs + embedding auto-heal backfill (`prepare_threshold=None` for Supabase pooler) |
| Official MCP Registry | ✅ `io.github.gaurav-kumar-sinha-060705/singularity` v0.2.0 **active**; old `compass-mcp-gateway` **deleted** |
| Smithery / Arcade | ✅ `gaurav060705/singularity` published (3 tools detected); cosmetic gap: "No description" in listing |
| Glama | ✅ Ownership verified (HTTP challenge), Status Healthy, Last Tested 2026-09-15; search index still crawling (24–48h) |
| GitHub repo | ✅ **Public** (`gaurav-kumar-sinha-060705/singularity`), discovery topics + description set, visible to crawlers |
| Execution gateway (Phase 3) | ✅ 4 keyless providers (weather, npms_lookup, pypi_lookup, web_search) execute through scope+trust+audit enforcer; MCP `call_tool` + `list_public_tools` live |
| Tests | ✅ 51 pytest green (API + scanner + MCP tools & endpoint + glama claim + providers + execute) |

---

## 3. Target Architecture

```
                         ┌──────────────────────────────────────────────┐
                         │                SINGULARITY                   │
  User problem ──► Intent ───────► Recommendation Index ──► Ranking ───┼──► Ranked, trust-labeled
  (natural         Parsing        (embeddings + filters)    Layer      │      suggestions + gateway
   language)       Engine                                              │
                         ▲                                             │
  Curated index ─────────┘                                             │
  Public registries ──► Crawler ──► Trust & Provenance Engine ─────────┘
  (Phase 5)                          • static injection scanner
                                     • LLM injection classifier (Phase 5)
                                     • publisher reputation (Phase 5)
                                     • permission-overreach check
                                         │
                                         ▼  approve ──► Gateway proxy (execution + audit)
```

### Execution reach (three-tier model) — the load-bearing constraint

A hosted gateway can only reach servers **over HTTP**. local stdio servers are
processes on the user's machine and are physically unreachable from Render — for
Singularity, for Claude.ai, for every hosted client. Execution is therefore tiered:

| Tier | What executes | Reach from Render | Auth |
|---|---|---|---|
| **1 — Keyless APIs** | Free public REST APIs wrapped as first-party providers (weather, npm, PyPI, web search) | ✅ HTTP | none |
| **2 — Hosted MCP remotes** | Vetted, hosted `streamable-http`/SSE MCP servers we proxy as an **MCP client** (Stripe, Notion, Smithery-hosted, Pipeworx-hosted…) | ✅ HTTP | user API key/OAuth (Phase 4 vault) |
| **3 — Local stdio** | stdio-only servers (the long tail: most of the registry) | ❌ not reachable | user connects locally |
| — **Run-stdio-ourselves** | Singularity containers the server (Smithery/Glama-style) | future infra decision | — |

Every executable surface advertises a `hosted_variant` (remote URL) when one exists;
stdio-only tools remain **recommend + vet + user-connects-locally**.

---

## 4. Shipment Log

| Phase | Scope | Status |
|---|---|---|
| 1 | Curated index, semantic search, calibrated confidence floor, trust scoring | ✅ |
| 1.5 | Native MCP server at `/mcp` (`find_solutions`, `get_trust_report`, `compare_tools`) | ✅ |
| 2a | Full rename Compass → Singularity; old listings deprecated + deleted | ✅ |
| 2b | Live deploy (Render + Supabase) hardened: pooler-safe prepared statements, seeded embeddings auto-heal | ✅ |
| 2c | Registry published under `io.github.gaurav-kumar-sinha-060705/singularity` | ✅ |
| 2d | Distribution verified across all channels: official registry active, Smithery/Arcade live, Glama claimed + healthy, GitHub public with discovery topics | ✅ |
| 3 | Execution gateway: provider framework + 4 keyless providers, `POST /api/v1/execute`, scope+trust+audit enforcer, MCP `call_tool`/`list_public_tools`, seeder sync-missing → `indexed_tools: 19` | ✅ |

---

## 5. Phase 3 — Execution Gateway ✅ *(shipped, verified live)*

### 5.1 Shipped scope

**Providers (first-party, public, zero-setup tools — no API keys).** Decisions locked:
- Only **first-party, keyless, read-only** providers execute through the gateway
  (`publisher: "Singularity"`, `publisher_verified: true`, `trust_score: 0.98`).
- Third-party tools stay **advisory** (no provider → refused: "no gateway provider").
- All providers share the single read-only scope `public:read`.

**Milestone 3.0 delivered: 4 keyless providers.**

1. **Provider adapter framework** — `app/providers/` + base `Provider` abstraction
   (`name`, `scopes[]`, `validate(args)`, `execute(args)`, docs URL). One file per provider.
2. **First providers:**
   - `weather` (Open-Meteo geocode → forecast, no key)
   - `npms_lookup` (npms.io package score + metadata, no key)
   - `pypi_lookup` (PyPI JSON API, no key)
   - `web_search` (DuckDuckGo Instant Answer API, no key)
3. **`POST /api/v1/execute`** — validates provider + scope, executes, writes
   `AuditLog(event="tool_executed", scopes_requested, scopes_granted, decision, latency_ms)`.
4. **Scope enforcer** — refuse calls outside granted scopes (`public:read`); denied calls
   land in the audit trail with `decision="denied"` + reason. Blocks low-trust tools
   (`trust_score < execute_min_trust`).
5. **Seed additions** — 4 providers registered in `seed_tools.json`;
   `find_solutions` returns them; execution flows recommendation → approve → gate → execute.
   - **Seeder behavior change:** boot path ingests *missing slugs* so deployed prod DB
     picks up new providers without a manual reseed. Existing rows untouched; advisory lock retained.
6. **MCP tools** — `call_tool(provider_slug, arguments, scope?)` gates through the same
   enforcer; `list_public_tools()` exposes what is executable.

**Config additions:** `execute_enabled`, `execute_min_trust=0.6`, `provider_http_timeout=10.0`.

**Verified live (acceptance passed):**
- Agent executed **all 4 providers through Claude** against the live deployment
  (`list_public_tools` → `call_tool`), all audited; `web_search` timed out on one
  user-network attempt (timeout config handles it as a non-fatal gateway failure).
- Weather executed live (London 14.3°C); pypi_lookup live via MCP `call_tool`.
- Denied scope → 403 + audit row; local health `indexed_tools: 19`.

**Result:** Tier 1 (keyless execution) is live end-to-end. `list_public_tools` +
`call_tool` published to the official registry / Smithery / Glama.

### 5.2 Resolved decisions (Phase 3)

- Auth: local-only first (no API keys for first-party providers) + terse gateway token later.
- Sandbox: same-process for well-known keyless tools, HTTPS + timeout only.
- Provider registry: self-hosted manifest; one provider class per file.
- Re-publish cadence: after each phase verified live.

---

## 6. Phase 4 — Hosted MCP Execution (Tier 2) + User Auth + OAuth

**Architecture constraint:** To store credentials for hosted remotes, we need user accounts first.
Build order: users → vault → OAuth → remote providers.

### 6.1 User Authentication (build now)

Signup/signin with JWT tokens. Users table, bcrypt passwords, 15-min access + 7-day refresh.
Protected routes via `get_current_user` dependency.

### 6.2 Credential Vault

Fernet-encrypted credential storage per user + per provider. `connections` table.
User stores API keys or OAuth tokens; vault encrypts at rest with `SINGULARITY_VAULT_KEY`.

### 6.3 OAuth Flows (one-click connect)

Stripe, GitHub, Notion, Slack, Google — OAuth2 PKCE flows. User authorizes, Singularity
exchanges code for access token, encrypts + stores in vault. Remote providers use stored
credentials when calling hosted MCP servers.

### 6.4 Remote Providers Use Credentials

`RemoteMcpProvider` fetches user's stored credential from vault, passes it as HTTP header
to `streamable_http_client`. User without connection → "connect your Stripe account" error.
All calls audited with user_id + connection_id.

---

## 7. Phase 5 — Trust Depth + Scale

- LLM injection classifier + labeled dataset (poisoned vs clean descriptions)
- Registry crawlers feed new candidates through the vetting pipeline — and record
  each candidate's `remotes[]`/hosted-variant so the executable (Tier 2) set grows
  automatically as servers publish HTTP endpoints
- Index past 200+ vetted tools; recommend hosted variants for the long stdio tail
- (Decision deferred) **Run-stdio-ourselves**: containerize stdio servers
  (Smithery/Glama-style) to make the entire catalog executable — infra + security
  cost vs. value; revisit once credentials + vault + Tier-2 demand are proven
- Enterprise dashboard/billing (API keys, usage analytics)

---

## 8. Use Cases / Future Cases

Product scenarios that drive roadmap priorities. Each carries the phase it unlocks.

| # | Use case | What it means for the agent | Phase |
|---|---|---|---|
| 1 | **"My team needs expense tracking"** | `find_solutions` returns finance tools ranked by fit + trust; flags read aloud; agent adopts a vetted tool | ✅ now |
| 2 | **"Look up the weather / an npm package / a PyPI library / research a topic"** | Same discovery path, but the agent then **executes** through the gateway under audit | 3 ✅ |
| 2b | **"Call my hosted Stripe/Notion/GitHub MCP through Singularity"** | Gateway proxies the hosted remote (MCP client) under scope+trust+audit | 4 |
| 3 | **"Use my Slack / GitHub / Gmail through Singularity"** | One-click OAuth connect; scoped capability tokens; gateway enforces scope on every call | 4 |
| 4 | **"Is this plugin safe to install?"** | Deep trust report: permissions overreach, injection verdicts, publisher reputation, LLM-classifier reading | 5 |
| 5 | **"Monitor every tool call my team's agents make"** | Enterprise audit dashboard: who asked, what was served, what executed, latency, denials | 5 |
| 6 | **"Monetize my verified tool"** | Clinical-grade vetting pipeline gives publishers a badge; marketplace-style listing + billing | 5 (post-visibility) |
| 7 | **"Keep my private tools private, vetted the same way"** | BYO tool ingestion: internal tools run through the same scanner + gateway on private infra | 5 (post-enterprise) |
| 8 | **"Sync my index across registries"** | Multi-registry sync: pull from official registry + Smithery + Glama; dedupe + re-vet candidates | 5 (post-crawler) |

---

## 9. Funding Gates

| Gate | When | What exists | Ask |
|---|---|---|---|
| **A — Pre-seed/grants** | Phase 3 shipped (gateway live) | Live demo + caught-poisoning + denied-call moment + audit trail | Classifier dataset + index to 200+ tools |
| **B** | Phase 4 mid | Execution gateway + hosted-remote proxy (Tier 2) + OAuth working | Scale vetting ops, design partners |
| **C** | Phase 5 early | Enterprise features | Infra + GTM |

---

## 10. Risk Register

| Risk | Mitigation |
|---|---|
| No off-the-shelf poisoned-descriptions dataset | Phase 5 item 1 is exactly this deliverable |
| False positives annoy legit publishers | Two-stage: static rules cheap+noisy → classifier confirms; humans review borderline |
| Registry formats drift | Adapter isolation; schema tests per adapter |
| Descriptions mutate post-vetting | Scheduled re-scan (Phase 5), not just ingest-time |
| PgBouncer / pooler prepared-statement crashes | `prepare_threshold=None`; verified live on Supabase session pooler |
| Keyless upstream outages (Open-Meteo, npms.io, PyPI, DDG) | Timeout + non-fatal gateway failures; result embeds error, audit still written |
| Seeder sync changes boot behavior | Ingest missing slugs only; existing rows untouched; advisory lock retained; full pytest gate |
| Free-tier cold starts delay the first request (~50s) | Starter plan ($7/mo) during listing/investor periods; already documented |
| Remote MCP endpoints are third-party surfaces | Allowlist of pre-vetted hostnames only; no arbitrary URLs from user input (SSRF guard); timeouts; audit every remote call |
| stdio-only servers not executable from hosted gateway | Three-tier model is explicit: stdio = recommend + user connects locally; only hosted remotes execute; run-stdio-ourselves deferred to Phase 5 |
| Third-party hosted proxies (Smithery/McParMory/Pipeworx) may gate on their own tokens | Tier-2 auth flows through Phase 4 vault (OAuth/encrypted keys); tools without a hosted variant stay advisory |
| Registry search API is slow/flaky (timeouts seen) | Enrichment runs offline (seeder/sync), resilient with retry + cache; never on the request hot path |

---

## 11. Standalone Docs

- Deployment walkthrough: [DEPLOY.md](DEPLOY.md)
- Product blueprint: `../Singularity_Blueprint.md`