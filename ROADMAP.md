# Singularity — Build Roadmap

> Companion to `../Singularity_Blueprint.md`.
> This document is the executable plan: what we build now, next, and after that — with
> acceptance criteria for each phase so "done" is never ambiguous.

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
| Phase 1 + 1.5 code | ✅ Curated recommender + native MCP server (`singularity/`) |
| Phase 2 rename | ✅ Full product rename complete; old listings deprecated |

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
                                     • publisher reputation
                                     • permission-overreach check
                                         │
                                         ▼  approve ──► Gateway proxy (execution + audit)
```

---

## 4. Phase 1 + 1.5 — Curated Recommender as MCP *(shipped)*

Curated index, semantic search, calibrated confidence floor, trust scoring, audit
logging. Deployed as a native MCP server; listed on official MCP Registry, Glama, Smithery.

- ✅ `pip install -r requirements.txt && python scripts/seed_index.py && uvicorn singularity.main:app`
- ✅ Poisoned sample tool (`quickledger-pro`) auto-flagged at ingest, excluded from top-3
- ✅ Native MCP server at `/mcp` (streamable HTTP, DNS-rebinding protected)
- ✅ pytest green

---

## 5. Phase 2 — Execution Gateway *(next)*

### 5.1 Scope

**Providers (public, zero-setup tools):**
`web_search`, `weather`, `npm_lookup`, `pypi_lookup`, `wikipedia`, `hacker_news`,
`currency`, `ip_geo`, `arxiv`, `news_rss`, `url_metadata`.

**Gateway (`POST /api/v1/execute`, MCP tools `call_tool` / `list_public_tools` / `request_access`):**
- Scope enforcer: refuses out-of-scope calls (e.g. `github:read` cannot do `github:write`)
- Rate limiter: per-user, per-tool, per-scope rolling windows (in-memory + Redis for prod)
- Audit table: `tool_id, user_hash, scopes_granted, scopes_requested, decision, latency_ms, error`
- **Acceptance:** an agent calls `weather.get_forecast("London")` through the gateway and
  gets a result. A low-trust tool call is denied with audit trail.

### 5.2 Open questions

- Auth: local-only first, or API-key from the start?
- Execution sandbox: container (Fly.io) or same-process (for well-known tools)?
- Public tool registry: self-hosted or piggyback on existing APIs?

---

## 6. Phase 3 — One-Click OAuth (personal tools)

Slack, GitHub, Gmail, Google Calendar — one-click connect like Zapier.
Capability tokens (short-TTL, scoped) issued per connection.
Gateway enforces scope on every call.

---

## 7. Phase 4 — Trust Depth + Scale

- LLM injection classifier + labeled dataset (poisoned vs clean descriptions)
- Registry crawlers feed new candidates through the vetting pipeline
- Index past 200+ vetted tools
- Enterprise dashboard/billing (API keys, usage analytics)

---

## 8. Funding Gates

| Gate | When | What exists | Ask |
|---|---|---|---|
| **A — Pre-seed/grants** | Phase 1 done | Live demo + caught-poisoning moment | Classifier dataset + index to 200+ tools |
| B | Phase 3 mid | Execution gateway + OAuth working | Scale vetting ops, design partners |
| C | Phase 4 early | Enterprise features | Infra + GTM |

---

## 9. Risk Register

| Risk | Mitigation |
|---|---|
| No off-the-shelf poisoned-descriptions dataset | Phase 4 item 1 is exactly this deliverable |
| False positives annoy legit publishers | Two-stage: static rules cheap+noisy → classifier confirms; humans review borderline |
| Registry formats drift | Adapter isolation; schema tests per adapter |
| Descriptions mutate post-vetting | Scheduled re-scan (Phase 4), not just ingest-time |
| SQLite ceiling | SQLAlchemy seam → pgvector swap is config-level |
