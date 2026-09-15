"""CLI wrapper around singularity.services.seeder — run: python scripts/seed_index.py"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.seeder import seed  # noqa: E402


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the Singularity recommendation index")
    parser.add_argument("--no-embeddings", action="store_true", help="skip embedding computation")
    args = parser.parse_args()
    result = seed(recompute_embeddings=not args.no_embeddings)
    print(f"Indexed {result['indexed']} tools ({result['flagged']} flagged):")
    from sqlalchemy import select  # noqa: E402
    from app.database import SessionLocal  # noqa: E402
    from app.models import Tool  # noqa: E402

    db = SessionLocal()
    try:
        for t in sorted(db.scalars(select(Tool)).all(), key=lambda x: x.trust_score):
            flag_note = f"  FLAGS: {', '.join(t.trust_flags)}" if t.trust_flags else ""
            print(f"  {t.slug:<22} trust={t.trust_score:<5} category={t.category:<20}{flag_note}")
    finally:
        db.close()
