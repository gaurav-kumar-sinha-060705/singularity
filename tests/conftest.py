import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["SINGULARITY_DATABASE_URL"] = "sqlite:///./data/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from scripts.seed_index import seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def seeded_index():
    seed(recompute_embeddings=True)
    from app.database import SessionLocal
    from app.services.recommendation_index import index

    db = SessionLocal()
    try:
        index.load(db)
    finally:
        db.close()
    yield


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
