# Deploying Compass — Render + Supabase (free tier friendly)

Total time: ~20 minutes of clicking. You need: a GitHub account, a Supabase account, a Render account.

---

## Step 1 — Push the repo to GitHub

```powershell
cd "D:\Gaurav\Projects\Secure MCP Gateway\compass"
git init
git add .
git commit -m "Compass MVP: discovery + trust scoring MCP server"
# Create an empty repo named e.g. `compass` on github.com first, then:
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/compass.git
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

> No schema setup needed — Compass creates its tables and seeds itself on first boot.

## Step 3 — Create the Render service

1. https://dashboard.render.com → **New +** → **Blueprint**
2. Connect your GitHub account and select the `compass` repo
3. Render reads `render.yaml`. When prompted for `COMPASS_DATABASE_URL`,
   paste the modified Supabase string from step 2
4. Click **Apply / Create** — build takes a few minutes (installs deps + bakes the embedding model)
5. Note your service URL, e.g. `https://compass-gateway-xxxx.onrender.com`

## Step 4 — Verify the deployment

```powershell
$u = "https://compass-gateway-xxxx.onrender.com"
Invoke-RestMethod "$u/health"                                   # expect status ok, indexed_tools 15
powershell -ExecutionPolicy Bypass -File scripts\smoke_mcp.ps1 -BaseUrl $u
```

The smoke script runs a full MCP handshake against the public URL. If it passes,
connect your editor:

```json
{ "servers": { "compass": { "type": "http", "url": "https://compass-gateway-xxxx.onrender.com/mcp" } } }
```

## Step 5 — Publish to registries

1. Fill placeholders in `server.json` and `smithery.yaml`
   (`<YOUR_GITHUB_USERNAME>`, `<YOUR_REPO_NAME>`, `<YOUR_RENDER_APP>`), commit, push
2. Official registry:
   ```powershell
   npx mcp-publisher@latest init      # validates/scaffolds server.json
   npx mcp-publisher@latest login     # choose GitHub auth → proves io.github.<you> ownership
   npx mcp-publisher@latest publish
   ```
   Then search "compass" at https://registry.modelcontextprotocol.io
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
| `/mcp` returns 421 Misdirected Request | Host not allowlisted. On Render this is automatic via `RENDER_EXTERNAL_URL`; locally set `COMPASS_MCP_ALLOWED_HOSTS` |
| Health OK but tools empty | DB was seeded without embeddings; run `python scripts/seed_index.py` locally against the same `COMPASS_DATABASE_URL`, or delete tables and reboot |
| Build fails on model download | Transient network in build container — Retry deploy |
| 429 responses | Rate limit hit; raise `COMPASS_RATE_LIMIT_PER_MIN` in dashboard env vars |
