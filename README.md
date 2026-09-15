# Singularity — MCP Discovery & Security Gateway

**Perplexity for MCP.** Discover the right tool for a problem, see its trust card
(prompt-injection risk, permission overreach, publisher status), connect it in one
click, and execute it through a secure, audited gateway — one product, one config.

See [ROADMAP.md](ROADMAP.md) for the full plan. Blueprint: `../Singularity_Blueprint.md`.

## Quickstart (local)

```powershell
pip install -r requirements.txt
copy .env.example .env          # optional, defaults are fine (SQLite)
python scripts/seed_index.py    # builds index + embeddings (first run downloads ~35MB model)
python -m uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
```

A fresh boot with an empty database auto-seeds itself — `seed_index.py` is only needed
for manual re-seeding.

## Use Singularity as an MCP server

Singularity is a native MCP server (streamable HTTP, stateless) at `/mcp`.

**VS Code / Copilot Chat** — `.vscode/mcp.json`:
```json
{ "servers": { "singularity": { "type": "http", "url": "http://127.0.0.1:8000/mcp" } } }
```

**Cursor** — `~/.cursor/mcp.json`:
```json
{ "mcpServers": { "singularity": { "url": "http://127.0.0.1:8000/mcp" } } }
```

**Claude Code CLI**:
```powershell
claude mcp add --transport http singularity http://127.0.0.1:8000/mcp
```

Then just ask your agent: *"my team needs expense tracking"* — it calls `find_solutions`
and gets ranked options with trust cards and security flags.

One-click install (replace the URL after deploying):
[Install in VS Code](vscode:mcp/install?name=singularity&url=http%3A%2F%2F127.0.0.1%3A8000%2Fmcp)

Smoke-check any endpoint without an editor:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke_mcp.ps1 -BaseUrl http://127.0.0.1:8000
```

## Tools

| Tool | What it does |
|---|---|
| `find_solutions(problem, top_k?, max_pricing_tier?)` | Ranked, trust-scored recommendations with plain-language security flags |
| `get_trust_report(slug)` | Full security card: permissions requested vs needed, injection-scan verdicts, publisher status |
| `compare_tools(slugs[])` | Side-by-side trust table for shortlisted candidates |

Every result carries `fit_score`, `trust_score`, `rank_score`, `trust_flags`, and a
human-readable `rationale`. The seed index includes `quickledger-pro`, an intentionally
poisoned fictional tool — ask about expenses and watch it get flagged
(`suspicious_description_imperative`, `hidden_unicode_characters`, `permission_overreach`)
and pushed down the ranking.

## Deploy your own instance (Render + Supabase)

Full walkthrough in [DEPLOY.md](DEPLOY.md). Summary:

1. Create a free Supabase project → copy the connection string → add `+psycopg` driver segment + `?sslmode=require`
2. Push this repo to GitHub → Render "New +" → "Blueprint" → it reads `render.yaml`
3. Paste the Supabase URL into the `SINGULARITY_DATABASE_URL` env var when prompted
4. First boot auto-creates tables and seeds the index; `https://<app>.onrender.com/mcp` is live

## Publish to MCP registries

After deploying (public HTTPS required), list Singularity so others can find it:

1. **Official MCP Registry** — edit placeholders in [`server.json`](server.json)
   (replace `XXXX` with your Render app name), then:
   ```powershell
   npx mcp-publisher@latest init      # scaffolds/validates server.json
   npx mcp-publisher@latest login     # prove io.github.<you> ownership via GitHub
   npx mcp-publisher@latest publish   # listed at https://registry.modelcontextprotocol.io within minutes
   ```
2. **Smithery** — submit repo at https://smithery.ai/new (uses [`smithery.yaml`](smithery.yaml))
3. **Glama** — submit remote URL at https://glama.ai, then claim the listing
4. **GitHub topics** — add `mcp-server`, `mcp`, `ai-security` topics to your repo; PulseMCP/MCP.so crawlers pick it up within 1–2 weeks

Tip: registries re-crawl weekly and strip verified badges from sleeping servers —
Render's `$7/mo` Starter plan keeps Singularity always-on during listing/investor periods.

## Tests

```powershell
pytest -v
```

## Layout

```
singularity/
  main.py                     FastAPI app factory + lifespan (auto-seed, mounts MCP)
  mcp_server.py               native MCP server: find_solutions / get_trust_report / compare_tools
  config.py                   env-driven settings (SINGULARITY_* prefix)
  database.py                 SQLAlchemy engine/session (SQLite dev, Postgres prod)
  models.py                   Tool + AuditLog tables
  schemas.py                  request/response contracts
  middleware/
    rate_limit.py             per-IP token-bucket limiter (X-Forwarded-For aware)
  services/
    discovery_engine.py       heuristic intent parsing
    recommendation_index.py   in-memory vector search over seeded tools
    ranking.py                fit/trust blend + rationale builder
    scanner.py                static injection/overreach scan (Phase 2 preview)
    seeder.py                 seed logic shared by CLI and boot-time auto-seed
    embeddings.py             fastembed provider (bge-small-en-v1.5)
  routers/
    recommend.py              POST /recommend, GET /tools
data/seed_tools.json          curated 15-tool index incl. demo attack sample
scripts/seed_index.py         manual seeding CLI
scripts/smoke_mcp.ps1         end-to-end MCP handshake smoke test (works on remote URLs too)
Dockerfile / render.yaml      one-command Render deployment
server.json / smithery.yaml   registry publishing manifests (fill placeholders first)
tests/                        pytest suite (API + scanner + MCP tools & endpoint)
```
