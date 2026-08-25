import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Tool, log_event
from app.schemas import RecommendRequest, RecommendResponse, RecommendationItem
from app.services.discovery_engine import parse_intent
from app.services.embeddings import embed_query
from app.services.ranking import rank_candidates
from app.services.recommendation_index import index

router = APIRouter()


@router.post("/recommend", response_model=RecommendResponse)
def recommend(payload: RecommendRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    intent = parse_intent(payload.problem)

    query_embedding = embed_query(payload.problem)
    hits = index.search(
        query_embedding,
        category=payload.filters.category,
        max_pricing_tier=payload.filters.max_pricing_tier,
        require_mcp=payload.filters.require_mcp,
        limit=payload.top_k * 3,
    )

    # fit_score is min-max normalized within the retrieved set (set-relative strength),
    # then blended with trust minus per-flag penalties — see ROADMAP.md §4.3.
    scored = rank_candidates(hits, intent["category_hint"])

    if not scored:
        intent_line = f"Detected intent: {intent['category_hint'] or 'general'}"
        return RecommendResponse(
            query=payload.problem,
            intent=intent,
            recommendations=[],
            message=(
                f"No strong match found for this query. {intent_line}. "
                "The tool index is currently limited (Phase 1) — try broadening "
                "your query or check back as more tools are indexed."
            ),
        )

    recommendations = [
        RecommendationItem(
            slug=tool.slug,
            name=tool.name,
            publisher=tool.publisher,
            publisher_verified=tool.publisher_verified,
            category=tool.category,
            mcp_available=tool.mcp_available,
            pricing_tier=tool.pricing_tier,
            integrations=tool.integrations,
            trust_score=tool.trust_score,
            trust_flags=tool.trust_flags,
            fit_score=fit_score,
            rank_score=rank_score,
            rationale=rationale,
        )
        for tool, fit_score, rank_score, rationale in scored[: payload.top_k]
    ]

    log_event(
        db,
        "recommendation_served",
        problem=payload.problem,
        returned=[r.slug for r in recommendations],
        top_rank=recommendations[0].rank_score if recommendations else None,
    )
    db.commit()

    return RecommendResponse(query=payload.problem, intent=intent, recommendations=recommendations)


@router.get("/tools/{slug}")
def get_tool(slug: str, db: Session = Depends(get_db)):
    tool = db.scalars(select(Tool).where(Tool.slug == slug)).first()
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    return tool.to_dict()


@router.get("/tools")
def list_tools(category: str | None = None, pricing_tier: str | None = None,
               mcp_only: bool = False, db: Session = Depends(get_db)):
    stmt = select(Tool)
    if category:
        stmt = stmt.where(Tool.category == category)
    if pricing_tier:
        stmt = stmt.where(Tool.pricing_tier == pricing_tier)
    if mcp_only:
        stmt = stmt.where(Tool.mcp_available.is_(True))
    tools = db.scalars(stmt.order_by(Tool.trust_score.desc())).all()
    return {"count": len(tools), "tools": [t.to_dict() for t in tools]}
