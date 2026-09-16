# Singularity — Deployment Status & Open Problems (resume log)

> Written 2026-09-16. This is the source of truth for where the Render deploy +
> Claude.ai connector integration stands. Continue from here.

## TL;DR

- Render deploy `https://singularity-osd2.onrender.com` is **live with Phase 4.6
  native MCP OAuth** (RFC 9745). `/.well-known/oauth-authorization-server` returns
  200 with the correct issuer; `indexed_tools: 21`; unauthenticated `POST /mcp`
  correctly returns 401.
- The SDK smoke handshake (`scripts/smoke_oauth.py`) passes end-to-end
  (register → authorize → consent signup → approve → token → initialize →
  tools/list).
- **BLOCKER: Claude.ai connector cannot register.** Error in Claude.ai UI:
  *"Couldn't register with Singularity's sign-in service."* (ref `ofid_f35fad5a057c89b6`)
- Root cause is 2 registration-endpoint failures (see below).

## Open problems

### P1 — Claude.ai `private_key_jwt` registration is rejected (400)
- Claude.ai sends `token_endpoint_auth_method: "private_key_jwt"`.
- The MCP SDK 2.0.0 `RegistrationHandler`
  (`mcp/server/auth/handlers/register.py:57`) **hard-codes a rejection** of
  `private_key_jwt` ("token_endpoint_auth_method 'private_key_jwt' is not supported"),
  because it cannot verify a client assertion (no JWKS storage/verification).
- Verified live: `POST /register` with `private_key_jwt` → 400.
- This alone fully explains Claude.ai's failure, since Claude.ai uses
  private_key_jwt by default.

### P1 — `client_secret_post` registration returns 500 (bug in our provider)
- Verified live: `POST /register` with `token_endpoint_auth_method:
  "client_secret_post"` (a normal confidential client) → **500 Internal Server Error**.
  It should be 201.
- Suspects to check in `app/mcp_oauth.py:register_client` (line ~112):
  - `encrypt(client_secret)` may raise on the vault (dev key vs production
    `SINGULARITY_VAULT_KEY`), or
  - the `OAuthClient` insert path may fail (e.g. `client_secret_expires_at` /
    `client_id_issued_at` not persisted — model has no such columns, but that
    shouldn't crash), or
  - a NULL/type mismatch on `redirect_uris_json` / `grant_types_json`.
- This must be fixed regardless — it's a real defect and blocks any confidential
  client. It may be the *actual* 500 Claude hits if it ever falls back to
  client_secret_post; check Render logs for the traceback.

### P2 — `private_key_jwt` full support (if we want a clean SDK path)
Options (unresearched, decide later):
1. **Custom `/register` route** that wraps the SDK route but rewrites
   `token_endpoint_auth_method: "private_key_jwt"` → `"client_secret_post"`
   (downstream: Claude stores the secret and still sends it). Simplest, keeps the
   201. Risk: Claude may send a `client_assertion` at token time instead of the
   secret → must also patch the SDK `ClientAuthenticator`, or convince Claude to
   use the secret (it won't hurt to try storing/returning a secret).
2. **Fork/vendor the SDK `RegistrationHandler`** and implement real RFC 7523
   `client_assertion` verification (store JWKS, verify `iss`/`aud`/`exp`) — more
   work, most correct.
3. Investigate whether Claude.ai accepts `client_secret_post` if we stop
   advertising `private_key_jwt`-incompatible metadata; or whether a newer MCP SDK
   (2.x) removed the hard block. **Check for MCP SDK updates first.**

### P3 — metadata gaps (informational)
- `/.well-known/oauth-authorization-server` does not advertise `scopes_supported`
  (SDK omits it; our `valid_scopes` is unset). `required_scopes=["mcp:tools"]`
  still enforces scope at the resource. Consider setting
  `ClientRegistrationOptions(valid_scopes=["mcp:tools"])` so metadata advertises it.
- Also consider `validate_token_resource` (SDK 3.0 will default it True) — set
  `validate_token_resource=True` now to refuse tokens issued for another resource.

## Current state of the codebase

### Committed & pushed (Render redeployed)
- `3dd29d5` Phase 4.5 stdio bridge (old)
- `b5c0312` Phase 4.6 native MCP OAuth (SDK auth server provider)
- `6619354` docs: Milestone 4.6 plan
- `db7d371` oauth_public_base auto-derives from RENDER_EXTERNAL_URL
- `b1c80d0` migrations: read column names from dict rows (Postgres crash fix #1)
- `e699e15` migrations: pass dtype only, not dtype+colname (crash fix #2)
- `130da15` migrations: Postgres boolean `DEFAULT false` (crash fix #3)

### Not yet committed (working tree)
- `scripts/smoke_oauth.py` — new full OAuth handshake smoke test (works vs live
  URL).
- Nothing else outstanding.

### Test suite
- `python -m pytest tests/ -q` → **112 passed, 1 warning** (pre-existing
  Starlette deprecation) as of last run.

## How to reproduce/verify

```powershell
# full OAuth handshake vs live prod
python scripts/smoke_oauth.py https://singularity-osd2.onrender.com

# DCR failure repros
python - <<PY
import httpx
c = httpx.Client(base_url='https://singularity-osd2.onrender.com', timeout=15)
print(c.post('/register', json={'client_name':'C',
  'redirect_uris':['https://claude.ai/api/mcp/auth_callback'],
  'grant_types':['authorization_code','refresh_token'],
  'response_types':['code'],
  'token_endpoint_auth_method':'private_key_jwt'}).status_code)
print(c.post('/register', json={'client_name':'C',
  'redirect_uris':['https://claude.ai/api/mcp/auth_callback'],
  'grant_types':['authorization_code','refresh_token'],
  'response_types':['code'],
  'token_endpoint_auth_method':'client_secret_post'}).status_code)
PY
```

## Next steps (in order)
1. **Fix the 500 on `client_secret_post`** — reproduce locally with a confidential
   DCR request, get the traceback, fix `register_client`/vault. Add a regression
   test (register with client_secret_post → 201 and token exchange works).
2. **Unblock Claude.ai `private_key_jwt`** — test whether mocking/rewriting to
   client_secret_post works against Claude.ai; if not, implement proper
   private_key_jwt support. Check newer MCP SDK first.
3. Settings polish (optional): add `valid_scopes=["mcp:tools"]` to
   ClientRegistrationOptions; consider `validate_token_resource=True`.
4. Commit the P1/P2 fixes + `scripts/smoke_oauth.py`, run full suite, push →
   Render auto-deploy → re-run live smoke.
5. Re-publish registry (`npx mcp-publisher@latest publish`) once Claude.ai
   connects — bump `server.json` version (already 0.3.0).
6. Re-test Claude.ai connector (Sign in now mode) end-to-end.

## Reference — key files
- `app/mcp_oauth.py` — `SingularityOAuthProvider` (register_client ~line 112).
- `app/mcp_server.py` — MCPServer auth wiring, `AuthSettings`.
- `app/routers/mcp_auth_pages.py` — consent/sign-in page.
- `app/migrations.py` — boot-time schema migration (3 Postgres fixes applied).
- `scripts/smoke_oauth.py` — live handshake tester (uncommitted).
- `tests/test_mcp_oauth.py`, `tests/oauth_helpers.py` — OAuth integration tests.
- SDK: `mcp/server/auth/handlers/register.py` (private_key_jwt block at :57),
  `mcp/server/auth/settings.py` (ClientRegistrationOptions/valid_scopes).