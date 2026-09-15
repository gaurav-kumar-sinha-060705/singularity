import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from singularity.models import Tool

PRICING_ORDER = {"free": 0, "freemium": 1, "paid": 2, "enterprise": 3}


class RecommendationIndex:
    def __init__(self) -> None:
        self._tools: list[Tool] = []
        self._matrix: np.ndarray | None = None

    @property
    def size(self) -> int:
        return len(self._tools)

    def load(self, db: Session) -> int:
        self._tools = list(db.scalars(select(Tool)).all())
        vectors = []
        for tool in self._tools:
            if tool.embedding:
                vectors.append(np.frombuffer(tool.embedding, dtype=np.float32))
            else:
                vectors.append(np.zeros(tool.embedding_dim or 384, dtype=np.float32))
        self._matrix = np.vstack(vectors) if vectors else np.empty((0, 384), dtype=np.float32)
        return len(self._tools)

    def search(self, query_embedding: np.ndarray, category: str | None = None,
               max_pricing_tier: str | None = None, require_mcp: bool = True,
               limit: int = 10) -> list[tuple[Tool, float]]:
        candidates: list[int] = []
        for i, tool in enumerate(self._tools):
            if require_mcp and not tool.mcp_available:
                continue
            if category and tool.category != category:
                continue
            if max_pricing_tier and PRICING_ORDER.get(tool.pricing_tier, 99) > PRICING_ORDER[max_pricing_tier]:
                continue
            if tool.trust_score < 0.2:
                continue
            candidates.append(i)

        results: list[tuple[Tool, float]] = []
        if candidates:
            sub = self._matrix[candidates]
            scores = sub @ query_embedding
            order = np.argsort(scores)[::-1][:limit]
            results = [(self._tools[candidates[j]], float(scores[j])) for j in order]
        return results


index = RecommendationIndex()
