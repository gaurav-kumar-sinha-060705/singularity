"""Index seeding — reusable from scripts and from app startup (seed-on-boot).

Two hardening guarantees for multi-instance/cloud deployments:
1. Deterministic tool IDs: uuid5 derived from the slug, so every boot/deploy
   generates identical IDs — audit-log foreign keys can never dangle.
2. A Postgres advisory transaction lock serializes concurrent seeds, so two
   instances booting simultaneously (e.g. during a Render deploy overlap)
   cannot interleave writes against the same database.
"""

import json
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import func, select, text

from app.database import SessionLocal, engine
from app.models import Base, Tool, log_event
from app.services.embeddings import embed_texts
from app.services.scanner import scan_tool

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "seed_tools.json"

# Fixed bigint key for pg_advisory_xact_lock ('SING' in hex).
_SEED_ADVISORY_LOCK_KEY = 0x53494E47

COPY_FIELDS = ("name", "publisher", "publisher_verified", "category", "description",
               "mcp_available", "pricing_tier", "source")
EVOLVING_FIELDS = ("execution_tier", "requires_credential")


def _tool_id(slug: str) -> str:
    """Stable ID: same slug always maps to the same UUID on every machine/boot."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://singularity.gateway/tool/{slug}"))


def _load_entries() -> list[dict]:
    payload = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return payload["tools"] if isinstance(payload, dict) else payload


def tool_count() -> int:
    db = SessionLocal()
    try:
        return int(db.scalar(select(func.count()).select_from(Tool)) or 0)
    finally:
        db.close()


def _backfill_embeddings(db) -> int:
    """Compute embeddings for any tool that lacks them (heals half-seeded states).

    Runs inside the caller's advisory lock. Failures are logged and non-fatal:
    startup must never hard-fail on embedding compute — a tools table without
    embeddings still boots; recommendations just degrade until reseeded.
    """
    targets = list(db.scalars(select(Tool).where(Tool.embedding.is_(None))))
    if not targets:
        return 0
    texts = [(t, f"{t.name}. {t.category}. {t.description}") for t in targets]
    try:
        vectors = embed_texts([text for _, text in texts])
        for (tool, _), vec in zip(texts, vectors):
            tool.embedding_dim = int(vec.shape[0])
            tool.embedding = vec.astype(np.float32).tobytes()
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"[singularity] embedding backfill skipped: {exc}")
        return 0
    print(f"[singularity] backfilled embeddings for {len(targets)} tool(s)")
    return len(targets)


def _populate(db, tool, entry) -> list[str]:
    """Copy one seed entry onto a Tool row (new or existing), returning flags."""
    flags = scan_tool(entry)
    for field in COPY_FIELDS:
        if field in entry:
            setattr(tool, field, entry[field])
    for field in EVOLVING_FIELDS:
        if field in entry:
            setattr(tool, field, entry[field])
    tool.trust_score = float(entry.get("trust_score", 0.5))
    tool.integrations_json = json.dumps(entry.get("integrations", []))
    tool.permissions_requested_json = json.dumps(entry.get("permissions_requested", []))
    tool.permissions_needed_json = json.dumps(entry.get("permissions_needed", []))
    tool.trust_flags_json = json.dumps(flags)
    db.flush()
    return flags


def seed(recompute_embeddings: bool = True, skip_if_nonempty: bool = False) -> dict:
    """Ingest seed_tools.json into the DB; returns {indexed, flagged}.

    skip_if_nonempty: when True (used by seed-on-boot), an existing tools table
    short-circuits full re-seeding but still ingests any *missing* slugs from
    the seed file (e.g. newly added providers), so deployed databases pick up
    new seed entries on boot without a manual reseed. Runs inside the advisory
    lock so concurrent booting instances cannot double-seed.
    """
    entries = _load_entries()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if engine.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                       {"key": _SEED_ADVISORY_LOCK_KEY})
        if skip_if_nonempty and tool_count() > 0:
            existing = set(db.scalars(select(Tool.slug)).all())
            missing = [e for e in entries if e["slug"] not in existing]
            if missing:
                for entry in missing:
                    tool = Tool(id=_tool_id(entry["slug"]), slug=entry["slug"])
                    db.add(tool)
                    flags = _populate(db, tool, entry)
                    log_event(
                        db, "tool_ingested", tool_id=tool.id, slug=tool.slug,
                        flags=flags, new_flags=flags, cleared_flags=[],
                        trust_score=tool.trust_score,
                    )
                db.commit()
                print(f"[singularity] database had {len(existing)} tools — "
                      f"ingested {len(missing)} new seed entry/entries: "
                      f"{', '.join(e['slug'] for e in missing)}")
            else:
                print(f"[singularity] database already has {len(existing)} tools — skipping seed")
            if recompute_embeddings:
                _backfill_embeddings(db)
            rows = db.scalars(select(Tool)).all()
            return {"indexed": len(rows), "flagged": sum(1 for t in rows if t.trust_flags)}
        for entry in entries:
            flags = scan_tool(entry)
            tool = db.scalars(select(Tool).where(Tool.slug == entry["slug"])).first()
            if not tool:
                tool = Tool(id=_tool_id(entry["slug"]), slug=entry["slug"])
                db.add(tool)
            old_flags = set(tool.trust_flags)
            flags = _populate(db, tool, entry)
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
            _backfill_embeddings(db)

        rows = db.scalars(select(Tool)).all()
        flagged = sum(1 for t in rows if t.trust_flags)
        return {"indexed": len(rows), "flagged": flagged}
    finally:
        db.close()


def backfill_evolving_fields() -> None:
    """Sync execution_tier / requires_credential from seed file onto all rows.

    Runs after boot migrations so pre-existing rows pick up the new columns'
    curated values without a full reseed (which would re-log ingest events).
    """
    entries = _load_entries()
    by_slug = {e["slug"]: e for e in entries}
    db = SessionLocal()
    try:
        rows = db.scalars(select(Tool)).all()
        changed = 0
        for tool in rows:
            entry = by_slug.get(tool.slug)
            if not entry:
                continue
            for field in EVOLVING_FIELDS:
                cur = getattr(tool, field)
                new = entry.get(field)
                if new is not None and cur != new:
                    setattr(tool, field, new)
                    changed += 1
        if changed:
            db.commit()
            print(f"[singularity] backfilled tier metadata for {changed} field(s)")
    finally:
        db.close()


def seed_if_empty() -> bool:
    """Seed only when the tools table is empty (first boot on a fresh database).

    The emptiness check runs inside the advisory lock (skip_if_nonempty=True),
    so a second instance booting while the first is mid-seed will wait, then
    correctly skip instead of double-seeding.
    """
    result = seed(recompute_embeddings=True, skip_if_nonempty=True)
    if tool_count() == 0:
        return False
    print(f"[singularity] seed state settled — {result['indexed']} tools "
          f"({result['flagged']} carry trust flags)")
    return True


if __name__ == "__main__":
    result = seed()
    print(f"Indexed {result['indexed']} tools ({result['flagged']} flagged)")
