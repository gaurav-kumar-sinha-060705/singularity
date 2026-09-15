from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()

if settings.database_url.startswith("sqlite"):
    db_file = settings.database_url.split("///", 1)[-1]
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
else:
    # Managed Postgres (Supabase): survive idle disconnects, keep the pool small —
    # free-tier instances cap concurrent connections. prepare_threshold=None
    # disables psycopg3's auto-prepared statements, which collide on the Supabase
    # PgBouncer poolers ("DuplicatePreparedStatement _pg3_0 already exists").
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=280,
        connect_args={"prepare_threshold": None},
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
