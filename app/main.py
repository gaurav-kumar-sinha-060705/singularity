from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.mcp_server import build_mcp_asgi_app
from app.middleware.rate_limit import RateLimitMiddleware
from app.migrations import run as run_migrations
from app.routers import auth, connections, execute, oauth, recommend
from app.services.embeddings import get_model
from app.services.recommendation_index import index
from app.services.seeder import backfill_evolving_fields, seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        run_migrations(db)
    finally:
        db.close()
    backfill_evolving_fields()
    seed_if_empty()
    db = SessionLocal()
    try:
        count = index.load(db)
    finally:
        db.close()
    get_model()
    async with AsyncExitStack() as stack:
        mcp_app = app.state.mcp_asgi_app
        await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        yield {"index_size": count}


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description=(
            "Singularity — MCP Discovery & Security Gateway. Recommends the right "
            "tool for a problem, labels trust before an agent relies on it, and "
            "gates execution through a secure, audited gateway."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(RateLimitMiddleware, limit_per_min=settings.rate_limit_per_min)
    app.include_router(recommend.router, prefix=settings.api_v1_prefix)
    app.include_router(execute.router, prefix=settings.api_v1_prefix)
    app.include_router(auth.router, prefix=settings.api_v1_prefix)
    app.include_router(connections.router, prefix=settings.api_v1_prefix)
    app.include_router(oauth.router, prefix=settings.api_v1_prefix)

    @app.get("/health")
    def health():
        return {"status": "ok", "indexed_tools": index.size}

    @app.get("/.well-known/glama.json")
    def glama_claim():
        return JSONResponse({
            "$schema": "https://glama.ai/mcp/schemas/connector.json",
            "claim": get_settings().glama_claim,
        })

    # Native MCP endpoint (streamable HTTP, stateless): http://host:port/mcp
    # Mounted LAST so explicit routes above always win.
    mcp_asgi_app = build_mcp_asgi_app()
    app.state.mcp_asgi_app = mcp_asgi_app
    app.mount("/", mcp_asgi_app)

    return app


app = create_app()
