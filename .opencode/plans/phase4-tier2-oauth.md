# Phase 4 — Hosted MCP Execution (Tier 2) + User Auth + OAuth

Status: BUILD (auth-first approach)
Companion: `ROADMAP.md` §3 (three-tier execution model), §6 Phase 4.

## Context / constraint

A hosted gateway (Render) can only reach MCP servers over HTTP. stdio servers are
local processes — unreachable from the cloud. Three-tier model:
- Tier 1 (DONE): keyless REST providers (weather, npms_lookup, pypi_lookup, web_search).
- Tier 2 (THIS PHASE): proxy vetted hosted `streamable-http` remotes as an MCP client.
- Tier 3: stdio-only servers = recommend + user connects locally (never executed remotely).

**Key architectural constraint (user-identified):** To store credentials for hosted remotes,
we need **user accounts first**. The correct build order:
1. User auth (signup/signin) → foundational for credential storage
2. Credential vault (per-user encrypted storage)
3. OAuth flows → connect Stripe/Notion/GitHub
4. Remote providers use user's credentials

## Milestone 4.0 — Remote MCP provider adapter (✅ DONE)

- `app/providers/remote.py` — `RemoteMcpProvider` with SSRF allowlist, async bridge,
  cached `list_tools`, auth_required gate.
- `registry.py` — Tier-2 catalog (stripe-mcp → `https://mcp.stripe.com`), `list_public_tools`
  surfaces `hosted` + `auth_required`.
- `scripts/enrich_remotes.py` — enrichment script to curate hosted remotes from registry.
- 59 pytest green; live smoke: list_public_tools works, call_tool refuses with auth reason.

## Milestone 4.1 — User Authentication (BUILD NOW)

**Goal:** Enable users to create accounts and sign in, so we can store their credentials.

### Schema
```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,  -- uuid5 from email
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,  -- bcrypt
    created_at TIMESTAMPTZ DEFAULT now()
);
```

### Endpoints
- `POST /api/v1/auth/signup` — email + password → returns JWT (access + refresh tokens)
- `POST /api/v1/auth/signin` — email + password → returns JWT
- `GET /api/v1/auth/me` — JWT required → returns user info

### Security
- Passwords: bcrypt with 12 rounds, never stored in plaintext
- Tokens: JWT with 15-min access, 7-day refresh, signed with `SINGULARITY_JWT_SECRET`
- Rate limiting: 5 signups/min per IP, 10 signins/min per IP

### Files
- `app/models.py` — add `User` model
- `app/auth.py` — JWT helpers (create/verify tokens), password hashing
- `app/routers/auth.py` — signup/signin/me endpoints
- `app/middleware/auth.py` — `get_current_user` dependency for protected routes
- `data/singularity.db` — users table auto-created via `create_all`
- `tests/test_auth.py` — signup, signin, token expiry, invalid credentials

## Milestone 4.2 — Credential Vault (BUILD AFTER 4.1)

**Goal:** Store user's API keys/OAuth tokens for hosted remotes, encrypted at rest.

### Schema
```sql
CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    provider_slug TEXT NOT NULL,  -- e.g. "stripe-mcp"
    credential_json TEXT NOT NULL,  -- encrypted with Fernet
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

### Encryption
- Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256)
- Key from env `SINGULARITY_VAULT_KEY` (generated at first boot if missing)
- Credential format: `{"api_key": "sk_..."}` or `{"access_token": "..."}`

### Endpoints
- `POST /api/v1/connections` — store encrypted credential for a provider
- `GET /api/v1/connections` — list user's connections (masked secrets)
- `DELETE /api/v1/connections/{id}` — revoke connection

### Files
- `app/models.py` — add `Connection` model
- `app/vault.py` — Fernet encrypt/decrypt helpers
- `app/routers/connections.py` — CRUD endpoints
- `tests/test_vault.py` — encrypt/decrypt, connection CRUD

## Milestone 4.3 — OAuth Flows (BUILD AFTER 4.2)

**Goal:** One-click connect for Stripe, GitHub, Notion, etc.

### Flow
1. User clicks "Connect Stripe" → redirect to Stripe OAuth authorize URL
2. Stripe redirects back with auth code → Singularity exchanges for access token
3. Access token encrypted + stored in `connections` table
4. Remote provider uses stored token when calling hosted MCP

### Supported (Phase 4.3)
- Stripe: OAuth2 PKCE (https://dashboard.stripe.com/settings/app_apikeys)
- GitHub: OAuth2 PKCE (https://github.com/settings/applications/new)
- Notion: OAuth2 (https://www.notion.so/my-integrations)
- Slack: OAuth2 (https://api.slack.com/apps)
- Google: OAuth2 (https://console.cloud.google.com/apis/credentials)

### Files
- `app/oauth.py` — OAuth2 PKCE helpers (authorize URL, exchange code, refresh)
- `app/routers/oauth.py` — callback endpoints per provider
- `app/services/oauth_providers.py` — provider-specific configs (client IDs, scopes, endpoints)
- `tests/test_oauth.py` — mock OAuth flows

## Milestone 4.4 — Remote Providers Use Credentials (BUILD AFTER 4.3)

**Goal:** `RemoteMcpProvider` fetches user's stored credential and passes it to hosted MCP.

### Integration
- `execute_via_gateway` gains `user_id` parameter (from JWT)
- `RemoteMcpProvider.execute()` checks `connections` table for user's credential
- If credential exists: passes it as HTTP header to `streamable_http_client`
- If no credential: returns "connection required" (not "Phase 4.5 not built")

### Acceptance
- User signs up → connects Stripe → calls `call_tool("stripe-mcp")` → executes successfully
- User without Stripe connection → call_tool fails with "connect your Stripe account"
- All calls audited with user_id + connection_id

## Files modified (summary)

- `app/models.py` — add User, Connection tables
- `app/auth.py` — NEW: JWT + password hashing
- `app/vault.py` — NEW: Fernet encryption
- `app/oauth.py` — NEW: OAuth2 PKCE helpers
- `app/routers/auth.py` — NEW: signup/signin/me
- `app/routers/connections.py` — NEW: credential CRUD
- `app/routers/oauth.py` — NEW: OAuth callbacks
- `app/middleware/auth.py` — NEW: get_current_user dependency
- `app/services/oauth_providers.py` — NEW: provider configs
- `app/config.py` — add JWT_SECRET, VAULT_KEY, OAuth client IDs
- `app/providers/remote.py` — inject credential from vault
- `app/services/gateway.py` — add user_id parameter
- `app/main.py` — mount auth routers, create tables on startup
- `tests/test_auth.py` — NEW
- `tests/test_vault.py` — NEW
- `tests/test_oauth.py` — NEW
- `tests/test_connections.py` — NEW
- `tests/test_remote.py` — update for credential injection

## Decisions resolved

- [x] Which hosted remotes to seed first → stripe-mcp (official https://mcp.stripe.com)
- [x] Auth template before vault → auth_required=True (refuses until Phase 4.1+4.2)
- [x] Build order → users first, then vault, then OAuth, then remote providers

## Acceptance (Phase 4 complete)

- User can sign up / sign in / view profile
- User can store API key for a provider (encrypted at rest)
- User can connect Stripe via OAuth (one-click)
- `call_tool("stripe-mcp")` uses user's stored credential → executes successfully
- User without connection → clear error "connect your Stripe account"
- 65+ pytest green; live smoke on Render after deploy
