# Singularity — Deployment Status & Open Problems (resume log)

> Updated 2026-09-17. Source of truth for where the Render deploy + Claude.ai
> connector integration stands. Continue from here.

## TL;DR

- Render deploy `https://singularity-osd2.onrender.com` is **live with Phase 4.6
  native MCP OAuth** (RFC 9745). `/.well-known/oauth-authorization-server` returns
  200 with the correct issuer; `indexed_tools: 21`; unauthenticated `POST /mcp`
  correctly returns 401.
- The SDK smoke handshake (`scripts/smoke_oauth.py`) passes end-to-end
  (register → authorize → consent signup → approve → token → initialize →
  tools/list).
- **DONE (2026-09-17): the two registration blockers are fixed + new helpers:**
  - `client_secret_post` 500 **fixed**: the `OAuthClient.client_secret_hash`
    column was `VARCHAR(128)` — Fernet tokens of a 64-char secret are ~184 chars,
    so Postgres raised `value too long` → 500 (SQLite ignored the limit, which is
    why local repros passed). Column widened to `String(512)` + boot migration.
  - Claude.ai `private_key_jwt` **unblocked**: `app/mcp_auth_ext.py` swaps the
    SDK's registration/auth handlers so `private_key_jwt` DCR is accepted, the
    client's public JWKS is stored, and `/token` verifies RFC 7523
    `client_assertion` JWTs (iss/sub == client_id, aud check, exp enforced).
    RFC 8414 metadata now advertises `private_key_jwt`.
  - Settings polish: `valid_scopes=["mcp:tools"]` publishes `scopes_supported`
    in metadata and rejects out-of-scope DCR requests.
- **Test suite: 122 passed, 1 warning** (10 new regression/integration tests).

## Open problems

- **None known for registration.** Real-world Claude.ai E2E still to confirm once
  this build is deployed (Claude's own `private_key_jwt` flow + redirect UI).
- P3 (low): `validate_token_resource` is not set (SDK 3.0 will default it True) —
  consider setting it later.

## Current state of the codebase

### Committed & pushed (Render redeployed)
- `3dd29d5` Phase 4.5 stdio bridge (old)
- `b5c0312` Phase 4.6 native MCP OAuth (SDK auth server provider)
- `6619354` docs: Milestone 4.6 plan
- `db7d371` oauth_public_base auto-derives from RENDER_EXTERNAL_URL
- `b1c80d0` migrations: read column names from dict rows
- `e699e15` migrations: pass dtype only, not dtype+colname
- `130da15` migrations: Postgres boolean `DEFAULT false`
- `69eb67a` added STATUS.md + scripts/smoke_oauth.py

### Uncommitted working tree (2026-09-17)
- `app/models.py` — `client_secret_hash` → `String(512)`; added `jwks_json`.
- `app/migrations.py` — widen `client_secret_hash` on Postgres (idempotent);
  add `oauth_clients.jwks_json`; guard tools ALTERs on table existence.
- `app/mcp_oauth.py` — persist/load client `jwks` (inline or via `jwks_uri`);
  `_resolve_jwks` helper.
- `app/mcp_auth_ext.py` — NEW: private_key_jwt registration + token-auth +
  metadata advertisement (`install_oauth_extensions()`).
- `app/mcp_server.py` — `valid_scopes=["mcp:tools"]`; calls
  `install_oauth_extensions()` before building the /mcp app.
- `tests/` — RSA/JWKS helpers + 10 new tests (client_secret_post round-trip &
  full flow, private_key_jwt full flow + bad-signature/aud rejects, metadata,
  migrations).

### Test suite
- `python -m pytest tests/ -q` → **122 passed, 1 warning** (pre-existing
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
1. Deploy this working tree to Render (commit + push).
2. Re-run live smoke + confirm Claude.ai connector (Sign in now mode) wraps.
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