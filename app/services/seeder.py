"""Index seeding — reusable from scripts and from app startup (seed-on-boot)."""

import json
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import func, select

from app.database import SessionLocal, engine
from app.models import Base, Tool, log_event
from app.services.embeddings import embed_texts
from app.services.scanner import scan_tool

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "seed_tools.json"

COPY_FIELDS = ("name", "publisher", "publisher_verified", "category", "description",
               "mcp_available", "pricing_tier", "source")


def _load_entries() -> list[dict]:
    payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return payload["tools"] if isinstance(payload, dict) else payload


def tool_count() -> int:
    db = SessionLocal()
    try:
        return int(db.scalar(select(func.count()).select_from(Tool)) or 0)
    finally:
        db.close()


def seed(recompute_embeddings: bool = True) -> dict:
    """Ingest seed_tools.json into the DB; returns {indexed, flagged}."""
    entries = _load_entries()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        texts_to_embed: list[tuple[str, str]] = []
        for entry in entries:
            flags = scan_tool(entry)
            tool = db.scalars(select(Tool).where(Tool.slug == entry["slug"])).first()
            if not tool:
                tool = Tool(id=str(uuid.uuid4()), slug=entry["slug"])
                db.add(tool)
            for field in COPY_FIELDS:
                if field in entry:
                    setattr(tool, field, entry[field])
            tool.trust_score = float(entry.get("trust_score", 0.5))
            tool.integrations_json = json.dumps(entry.get("integrations", []))
            tool.permissions_requested_json = json.dumps(entry.get("permissions_requested", []))
            tool.permissions_needed_json = json.dumps(entry.get("permissions_needed", []))
            old_flags = set(tool.trust_flags)
            tool.trust_flags_json = json.dumps(flags)

            texts_to_embed.append((tool.id, f"{tool.name}. {tool.category}. {tool.description}"))

            log_event(
                db,
                "tool_ingested",
                tool_id=tool.id,
                slug=tool.slug,
                flags=flags,
                new_flags=sorted(set(flags) - old_flags),
                cleared_flags=sorted(old_flags - set(flags)),
                trust_score=tool.trust_score,
            )
        db.commit()

        if recompute_embeddings:
            vectors = embed_texts([text for _, text in texts_to_embed])
            for (tool_id, _), vec in zip(texts_to_embed, vectors):
                tool = db.get(Tool, tool_id)
                tool.embedding_dim = int(vec.shape[0])
                tool.embedding = vec.astype(np.float32).tobytes()
            db.commit()

        rows = db.scalars(select(Tool)).all()
        flagged = sum(1 for t in rows if t.trust_flags)
        return {"indexed": len(rows), "flagged": flagged}
    finally:
        db.close()


def seed_if_empty() -> bool:
    """Seed only when the tools table is empty (first boot on a fresh database)."""
    if tool_count() > 0:
        return False
    summary = seed(recompute_embeddings=True)
    print(f"[compass] fresh database detected — auto-seeded {summary['indexed']} tools "
          f"({summary['flagged']} carry trust flags)")
    return True


if __name__ == "__main__":
    result = seed()
    print(f"Indexed {result['indexed']} tools ({result['flagged']} flagged)")
