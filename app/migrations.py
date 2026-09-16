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


def _columns(db, table: str) -> set[str]:
    if _dialect(db.get_bind()) == "sqlite":
        cols = {row[1] for row in db.execute(text(f"PRAGMA table_info({table})")).all()}
    else:
        cols = {r.column_name for r in inspect(db.get_bind()).get_columns(table)}
    return cols


def _add_column(db, table: str, column: str, dtype: str) -> None:
    dialect = _dialect(db.get_bind())
    if dialect == "sqlite":
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}"))
    else:
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}"))
    db.commit()
    print(f"[singularity] migration: added {table}.{column} ({dtype})")


def run(db: Session) -> None:
    try:
        tool_cols = _columns(db, "tools")
    except Exception:
        return

    migrations = [
        ("tools", "requires_credential", "BOOLEAN NOT NULL DEFAULT 0",
         "requires_credential BOOLEAN NOT NULL DEFAULT 0"),
        ("tools", "execution_tier", "VARCHAR(20) NOT NULL DEFAULT 'unknown'",
         "execution_tier VARCHAR(20) NOT NULL DEFAULT 'unknown'"),
    ]

    for table, col, _dtype, full_def in migrations:
        if col not in tool_cols:
            _add_column(db, table, col, full_def)
