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
| Live deployment | ✅ Render + Supabase (`https://singularity-osd2.onrender.com`), `/health` → `indexed_tools: 15` |
| Database hardening | ✅ Postgres advisory lock + deterministic uuid5 IDs + embedding auto-heal backfill |
| Registry | ✅ `io.github.gaurav-kumar-sinha-060705/singularity` v0.2.0 **active**; old `compass-mcp-gateway` **deleted** |
| Tests | ✅ 24 pytest green (API + scanner + MCP tools & endpoint) |

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
                                     • LLM injection classifier (Phase 4)
                                     • publisher reputation (Phase 4)
                                     • permission-overreach check
                                         │
                                         ▼  approve ──► Gateway proxy (execution + audit)
```

---

## 4. Shipment Log

| Phase | Scope | Status |
|---|---|---|
| 1 | Curated index, semantic search, calibrated confidence floor, trust scoring | ✅ |
| 1.5 | Native MCP server at `/mcp` (`find_solutions`, `get_trust_report`, `compare_tools`) | ✅ |
| 2a | Full rename Compass → Singularity; old listings deprecated + deleted | ✅ |
| 2b | Live deploy (Render + Supabase) hardened: pooler-safe prepared statements, seeded embeddings auto-heal | ✅ |
| 2c | Registry published under `io.github.gaurav-kumar-sinha-060705/singularity` | ✅ |

---

## 5. Phase 3 — Execution Gateway *(next build)*

### 5.1 Scope

**Providers (public, zero-setup tools — no API keys):**
`web_search`, `weather`, `npm_lookup`, `pypi_lookup`, `wikipedia`, `hacker_news`,
`currency`, `ip_geo`, `arxiv`, `news_rss`, `url_metadata`.

**Milestone 3.0 (first demo-able cut):** 3 keyless providers.

1. **Provider adapter framework** — `app/providers/` + base `Provider` abstraction
   (`name`, `scopes[]`, `call(args)`, docs URL). One file per provider.
2. **First providers**:
   - `weather` (Open-Meteo, no key)
   - `npms_lookup` (npm registry API, no key)
   - `pypi_lookup` (PyPI JSON API, no key)
3. **`POST /api/v1/execute`** — validates provider + scope, executes, writes
   `AuditLog(event="tool_executed", scopes_requested, scopes_granted, decision, latency_ms)`.
4. **Scope enforcer** — refuse calls outside granted scopes; denied calls land in the
   audit trail with `decision="denied"` + reason.
5. **Seed additions** — register the 3 providers in `seed_tools.json` so
   `find_solutions` can return them; execution then flows recommendation → approve → gate → execute.
6. **MCP `call_tool(name, arguments)`** — gates through the same enforcer.

**Acceptance:**
- An agent asks *"what's the weather in London"* → `find_solutions` returns the `weather`
  provider → `call_tool` executes through the gateway → audit row written.
- A denied call (e.g. `github:write` attempted on a read-only provider) is refused with
  an audit trail.
- A poisoned tool (existing `suspicious_*` flags) is **blocked from execution** even if
  it matches the query.

### 5.2 Open questions

- Auth: local-only first, or API-key from the start? (default: local-only + terse gateway token)
- Execution sandbox: same-process for well-known keyless tools? (default: yes, HTTPS + timeout only)
- Provider registry: self-hosted manifest, or scaffolds per public API?

---

## 6. Phase 4 — One-Click OAuth (personal tools)

Slack, GitHub, Gmail, Google Calendar — one-click connect like Zapier.
Capability tokens (short-TTL, scoped) issued per connection. Gateway enforces scope on every call.

---

## 7. Phase 5 — Trust Depth + Scale

- LLM injection classifier + labeled dataset (poisoned vs clean descriptions)
- Registry crawlers feed new candidates through the vetting pipeline
- Index past 200+ vetted tools
- Enterprise dashboard/billing (API keys, usage analytics)

---

## 8. Use Cases / Future Cases

Product scenarios that drive roadmap priorities. Each carries the phase it unlocks.

| # | Use case | What it means for the agent | Phase |
|---|---|---|---|
| 1 | **"My team needs expense tracking"** | `find_solutions` returns finance tools ranked by fit + trust; flags read aloud; agent adopts a vetted tool | ✅ now |
| 2 | **"Look up the weather / an npm package / a PyPI library"** | Same discovery path, but the agent then **executes** through the gateway under audit | 3 |
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
| **B** | Phase 4 mid | Execution gateway + OAuth working | Scale vetting ops, design partners |
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
| Free-tier cold starts delay the first request (~50s) | Starter plan ($7/mo) during listing/investor periods; already documented |

---

## 11. Standalone Docs

- Deployment walkthrough: [DEPLOY.md](DEPLOY.md)
- Product blueprint: `../Singularity_Blueprint.md`