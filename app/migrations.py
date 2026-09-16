"""Boot-time schema migrations: add new columns to existing tables if missing.

SQLite and Postgres behave differently — create_all only creates missing tables,
not columns. This module runs after create_all and adds any new columns safely.
Idempotent: checks column existence before ALTER.
"""

import sqlite3

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session


def _dialect(engine) -> str:
    return engine.dialect.name


def _table_names(engine) -> set[str]:
    if _dialect(engine) == "sqlite":
        return {row[0] for row in engine.connect().execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()}
    return set(inspect(engine).get_table_names())


def _columns(db, table: str) -> set[str]:
    if _dialect(db.get_bind()) == "sqlite":
        cols = {row[1] for row in db.execute(text(f"PRAGMA table_info({table})")).all()}
    else:
        cols = {r["name"] for r in inspect(db.get_bind()).get_columns(table)}
    return cols


def _add_column(db, table: str, column: str, dtype: str) -> None:
    db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}"))
    db.commit()
    print(f"[singularity] migration: added {table}.{column} ({dtype})")


def run(db: Session) -> None:
    # New tables are created by Base.metadata.create_all at boot; this run also
    # auto-creates them for dev databases that already executed create_all once.
    engine = db.get_bind()
    already = {t for t in _table_names(engine)}
    from app.database import Base
    from app import models  # noqa: F401  ensure models registered

    for table in ("oauth_clients", "oauth_auth_codes", "oauth_tokens"):
        if table not in already:
            Base.metadata.tables[table].create(bind=engine)
            db.commit()
            print(f"[singularity] migration: created table {table}")

    try:
        tool_cols = _columns(db, "tools")
    except Exception as exc:
        print(f"[singularity] migration: cannot inspect tools table — {exc}")
        return

    dialect = _dialect(db.get_bind())
    migrations = [
        ("tools", "requires_credential", "BOOLEAN NOT NULL DEFAULT false" if dialect == "postgresql" else "BOOLEAN NOT NULL DEFAULT 0"),
        ("tools", "execution_tier", "VARCHAR(20) NOT NULL DEFAULT 'unknown'"),
    ]

    for table, col, dtype in migrations:
        if col not in tool_cols:
            _add_column(db, table, col, dtype)
