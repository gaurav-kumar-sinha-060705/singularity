# Deploying Singularity — Render + Supabase (free tier friendly)

Total time: ~20 minutes of clicking. You need: a GitHub account, a Supabase account, a Render account.

---

## Step 1 — Push the repo to GitHub

```powershell
cd "D:\Gaurav\Projects\Secure MCP Gateway\singularity"
git init
git add .
git commit -m "Singularity: MCP Discovery & Security Gateway"
# Create an empty repo named `singularity` on github.com first, then:
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/singularity.git
git push -u origin main
```

## Step 2 — Create the Supabase database

1. https://supabase.com → **New project** (pick any name, region close to you)
2. Save your DB password somewhere safe
3. Go to **Project Settings → Database → Connection string → URI** and copy it. It looks like:
   ```
   postgresql://postgres:YOUR-PASSWORD@db.abcdefghijklmnop.supabase.co:5432/postgres
   ```
4. Modify it into the app's format — add the `+psycopg` driver segment and SSL:
   ```
   postgresql+psycopg://postgres:YOUR-PASSWORD@db.abcdefghijklmnop.supabase.co:5432/postgres?sslmode=require
   ```
   Keep this modified string for step 3.

> No schema setup needed — Singularity creates its tables and seeds itself on first boot.

## Step 3 — Create the Render service

1. https://dashboard.render.com → **New +** → **Blueprint**
2. Connect your GitHub account and select the `singularity` repo
3. Render reads `render.yaml`. When prompted for `SINGULARITY_DATABASE_URL`,
   paste the modified Supabase string from step 2
4. Click **Apply / Create** — build takes a few minutes (installs deps + bakes the embedding model)
5. Note your service URL, e.g. `https://singularity-xxxx.onrender.com`

## Step 4 — Update server.json and smoke-test

Replace `XXXX` in `server.json` and `smithery.yaml` with your actual Render app name, then:

```powershell
$u = "https://singularity-xxxx.onrender.com"
Invoke-RestMethod "$u/health"                                   # expect status ok, indexed_tools 21+
Invoke-RestMethod "$u/.well-known/oauth-authorization-server"   # expect issuer = your Render URL
Invoke-RestMethod "$u/authorize/stripe-mcp"                     # expect OAuth UI (challenge flow for /mcp)
```

`/mcp` requires an OAuth access token (Authorization popup at `/authorize/<slug>`), so a bare
handshake smoke script no longer applies; regression coverage lives in `python -m pytest tests/`.
The OAuth issuer
base auto-derives from `RENDER_EXTERNAL_URL` (no env var needed). If you want to
override it (e.g., custom domain), set `SINGULARITY_OAUTH_PUBLIC_BASE` in the
Render dashboard.

The smoke script runs a full MCP handshake against the public URL. If it passes,
connect your editor:

```json
{ "servers": { "singularity": { "type": "http", "url": "https://singularity-xxxx.onrender.com/mcp" } } }
```

## Step 5 — Publish to registries

1. Commit the updated `server.json` and `smithery.yaml`, push
2. Official registry:
   ```powershell
   npx mcp-publisher@latest init      # validates/scaffolds server.json
   npx mcp-publisher@latest login     # choose GitHub auth → proves io.github.<you> ownership
   npx mcp-publisher@latest publish
   ```
   Then search "singularity" at https://registry.modelcontextprotocol.io
3. Smithery: https://smithery.ai/new → submit repo URL
4. Glama: submit your `/mcp` URL at glama.ai → then claim the auto-created listing
5. Repo settings → Topics → add: `mcp-server`, `mcp`, `ai-security`, `agent-safety`

PulseMCP/MCP.so crawl the GitHub topic automatically within 1–2 weeks.

## Free-tier caveats

- Render free services sleep after 15 min idle → first visitor waits ~30–60s.
  Registries re-crawl weekly; sleeping during a re-crawl can strip verified badges.
  Upgrade to Starter ($7/mo) before publishing if you want always-on reliability.
- Supabase free projects pause after 7 days of inactivity — demo traffic keeps it awake,
  or restore from the dashboard.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/mcp` returns 421 Misdirected Request | Host not allowlisted. On Render this is automatic via `RENDER_EXTERNAL_URL`; locally set `SINGULARITY_MCP_ALLOWED_HOSTS` |
| Health OK but tools empty | DB was seeded without embeddings; run `python scripts/seed_index.py` locally against the same `SINGULARITY_DATABASE_URL`, or delete tables and reboot |
| Build fails on model download | Transient network in build container — Retry deploy |
| 429 responses | Rate limit hit; raise `SINGULARITY_RATE_LIMIT_PER_MIN` in dashboard env vars |
