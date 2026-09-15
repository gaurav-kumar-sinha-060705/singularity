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


@lru_cache
def get_settings() -> Settings:
    return Settings()


def parse_allowed_hosts(raw: str) -> list[str]:
    return [host.strip() for host in raw.split(",") if host.strip()]
