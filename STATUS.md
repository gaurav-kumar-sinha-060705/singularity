# Singularity — Deployment Status & Open Problems (resume log)

> Updated 2026-09-17. Source of truth for where the Render deploy + Claude.ai
> connector integration stands. Continue from here.

## TL;DR

- Render deploy `https://singularity-osd2.onrender.com` is **live with Phase 4.6
  native MCP OAuth** (RFC 9745) + **Glama/Claude interop verified**:
  - `/.well-known/oauth-authorization-server` advertises `private_key_jwt` +
    `scopes_supported=["mcp:tools"]`.
  - Live matrix: DCR 201 for private_key_jwt / client_secret_post / none; the
    full register→authorize→consent→token→/mcp flow returns 200 for public,
    client_secret_post, and private_key_jwt clients.
  - Claude.ai connector is unblocked (private_key_jwt + client_secret_post both
    work). Glama's public unauth probe still 401s by design (OAuth required);
    the only interop gap left there is private_key_jwt without `client_id` in the
    /token form (our server 401s "Missing client_id").
- **NEW (2026-09-17): connect dashboard + live credential verify.**
  - `GET /dashboard` (and `/`) — self-contained page: sign in/sign up, connect
    hosted MCP tools via OAuth (if provider configured) or paste an API key,
    then **Verify** (probes the remote MCP with the stored credential and lists
    its tools). Guides the user to call via Claude afterwards.
  - `GET /api/v1/connections/{slug}/verify` — decrypt vault credential → build
    auth headers → initialize + tools/list against the hosted remote; 200 =
    `call_tool(slug, …)` will authenticate. Audited (`connection_verified`).
- **Test suite: 130 passed, 1 warning** (8 new dashboard/verify tests).

## Open problems

- Glama interop: accept `client_id` derived from the client_assertion `iss` when
  the /token form omits it (optionally harden `app/mcp_auth_ext.py`).
- P3 (low): `validate_token_resource` is not set (SDK 3.0 will default it True) —
  consider setting it later.

## Current state of the codebase

### Committed & pushed (Render redeployed)
- `845fe6e` unblock Claude OAuth: private_key_jwt DCR/token-auth + fix
  client_secret_post 500 on Postgres (widened client_secret_hash,
  jwks_json, valid_scopes). Live-verified on Render (2026-09-17).
- `3dd29d5` Phase 4.5 stdio bridge (old)
- `b5c0312` Phase 4.6 native MCP OAuth (SDK auth server provider)
- `6619354` docs: Milestone 4.6 plan
- `db7d371` oauth_public_base auto-derives from RENDER_EXTERNAL_URL
- `b1c80d0` migrations: read column names from dict rows
- `e699e15` migrations: pass dtype only, not dtype+colname
- `130da15` migrations: Postgres boolean `DEFAULT false`
- `69eb67a` added STATUS.md + scripts/smoke_oauth.py

### Uncommitted working tree (2026-09-17)
- `app/routers/connections.py` — NEW `GET /{provider_slug}/verify`: decrypt
  vault credential → probe remote MCP (initialize + tools/list) → tool count.
- `app/static/dashboard.html` — NEW connect page (sign-in, OAuth or API-key
  connect, verify, Claude call guidance, same-account note).
- `app/routers/dashboard.py` — NEW: serves `/dashboard` + `/` redirect.
- `app/main.py` — include dashboard router before the /mcp mount.
- `tests/test_dashboard_verify.py` — 8 tests (page, redirect, verify
  success/400/404/502/401/501).

### Test suite
- `python -m pytest tests/ -q` → **130 passed, 1 warning** (pre-existing
  Starlette deprecation).

## How to reproduce/verify

```powershell
# full OAuth handshake vs live prod
python scripts/smoke_oauth.py https://singularity-osd2.onrender.com

# metadata advertises private_key_jwt + scopes_supported
python - <<PY
import httpx
c = httpx.Client(base_url='https://singularity-osd2.onrender.com', timeout=15)
m = c.get('/.well-known/oauth-authorization-server').json()
print(m['token_endpoint_auth_methods_supported'])
print(m['scopes_supported'])
PY
```

## Next steps (in order)
1. Commit + push this working tree (dashboard + verify endpoint) → Render
   auto-deploys; re-verify `/dashboard` and a verify call live.
2. User connects a Tier 2 tool on `/dashboard` (OAuth if GitHub App envs set, or
   paste an API key), Save & verify → green, then call_tool from Claude.
3. Re-publish registry (`npx mcp-publisher@latest publish`) once Claude.ai
   connects — bump `server.json` version (already 0.3.0).
4. (Vision — unstarted) singular MCP endpoint that brokers all registered tools:
   Tier 1 built-ins (done), Tier 2 hosted remotes (curated catalog exists,
   execution gated), Tier 3 local stdio → dockerized streamable + auto-connect,
   OAuth creds stored in DB for reuse, first-connect sign-in enforced.

## Reference — key files
- `app/mcp_auth_ext.py` — private_key_jwt registration/token-auth extensions.
- `app/mcp_oauth.py` — `SingularityOAuthProvider` (register_client, jwks).
- `app/mcp_server.py` — MCPServer auth wiring, `AuthSettings`, ext install.
- `app/models.py` / `app/migrations.py` — schema + boot migrations.
- `app/routers/mcp_auth_pages.py` — consent/sign-in page.
- `scripts/smoke_oauth.py` — live handshake tester.
- `tests/test_mcp_oauth.py`, `tests/oauth_helpers.py` — OAuth integration tests.
- SDK: `mcp/server/auth/routes.py` (patched names),
  `mcp/server/auth/handlers/register.py`.