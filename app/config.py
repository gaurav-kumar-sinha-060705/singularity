from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SINGULARITY_", extra="ignore")

    app_name: str = "Singularity"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./data/singularity.db"
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    fit_weight: float = 0.65
    trust_weight: float = 0.35
    flag_penalty: float = 0.07
    default_top_k: int = 5
    min_fit_threshold: float = 0.57
    general_intent_threshold: float = 0.85

    # MCP endpoint security: hosts allowed to call /mcp (DNS-rebinding guard).
    mcp_allowed_hosts: str = "localhost:*,127.0.0.1:*,[::1]:*,testserver"

    # HTTP protection
    rate_limit_per_min: int = 300

    # Glama ownership verification token (served at /.well-known/glama.json).
    glama_claim: str = "glama_claim_u7S6w2_iR_kGDr86GYa0az7UVK2nKuUy"

    # Execution gateway
    execute_enabled: bool = True
    execute_min_trust: float = 0.6
    provider_http_timeout: float = 10.0

    # Tier 2 — hosted MCP remotes (RemoteMcpProvider). Only hosts in this list
    # may be connected to; anything else (incl. arbitrary user URLs) is refused
    # as an SSRF guard.
    remote_mcp_timeout: float = 15.0
    remote_list_tools_ttl: float = 300.0
    remote_mcp_allowed_hosts: str = (
        "mcp.stripe.com,mcp.notion.com,server.smithery.ai,"
        "waystation.ai,mcp.mcparmory.com,gateway.pipeworx.io,"
        "mcp.mcparmory.com,slacking.biz,nexgendata-mcp-proxy.steve-corbett.com"
    )

    # Auth + credential vault
    jwt_secret: str = "singularity-dev-jwt-secret-change-me"
    vault_key: str = ""  # Fernet key; generated at boot if empty (dev/local)
    auth_signup_per_min: int = 5
    auth_signin_per_min: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()


def parse_allowed_hosts(raw: str) -> list[str]:
    return [host.strip() for host in raw.split(",") if host.strip()]
