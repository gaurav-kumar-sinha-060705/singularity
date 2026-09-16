from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolFilters(BaseModel):
    category: str | None = None
    max_pricing_tier: Literal["free", "freemium", "paid", "enterprise"] | None = None
    require_mcp: bool = True


class RecommendRequest(BaseModel):
    problem: str = Field(min_length=3, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    filters: ToolFilters = Field(default_factory=ToolFilters)


class IntentOut(BaseModel):
    category_hint: str | None
    constraints: dict[str, bool]
    matched_keywords: list[str]


class RecommendationItem(BaseModel):
    slug: str
    name: str
    publisher: str
    publisher_verified: bool
    category: str
    mcp_available: bool
    pricing_tier: str
    integrations: list[Any]
    trust_score: float
    trust_flags: list[Any]
    fit_score: float
    rank_score: float
    rationale: str


class RecommendResponse(BaseModel):
    query: str
    intent: IntentOut
    recommendations: list[RecommendationItem]
    message: str | None = None


class ExecuteRequest(BaseModel):
    provider_slug: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    scope: str | None = Field(default=None, max_length=40)


class ExecuteResponse(BaseModel):
    provider: str
    version: str
    scope: str
    audited: bool = True
    ok: bool
    result: Any | None = None
    error: str | None = None
    latency_ms: int


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)


class SigninRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    created_at: str


class ConnectionIn(BaseModel):
    provider_slug: str = Field(min_length=1, max_length=120)
    credential: dict[str, Any] = Field(min_length=1)


class ConnectionOut(BaseModel):
    id: str
    provider_slug: str
    created_at: str
    updated_at: str
