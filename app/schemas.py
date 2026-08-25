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


class FeedbackRequest(BaseModel):
    problem: str = Field(min_length=1, max_length=1000)
    slug: str
    rating: Literal["up", "down"]


class ToolOut(BaseModel):
    slug: str
    name: str
    publisher: str
    publisher_verified: bool
    category: str
    description: str
    mcp_available: bool
    pricing_tier: str
    integrations: list[Any]
    permissions_requested: list[Any]
    permissions_needed: list[Any]
    trust_score: float
    trust_flags: list[Any]
    source: str
    created_at: str
    updated_at: str
