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

    for table in ("oauth_clients", "oauth_auth_codes", "oauth_tokens", "remote_oauth_clients"):
        if table not in already:
            if not getattr(engine, "_run_ddl_visitor", None):
                continue  # inert stub engines in tests: skip DDL (no dialect driver)
            Base.metadata.tables[table].create(bind=engine)
            db.commit()
            print(f"[singularity] migration: created table {table}")

    dialect = _dialect(db.get_bind())

    if "tools" in already:
        try:
            tool_cols = _columns(db, "tools")
        except Exception as exc:
            print(f"[singularity] migration: cannot inspect tools table — {exc}")
            tool_cols = set()

        migrations = [
            ("tools", "requires_credential", "BOOLEAN NOT NULL DEFAULT false" if dialect == "postgresql" else "BOOLEAN NOT NULL DEFAULT 0"),
            ("tools", "execution_tier", "VARCHAR(20) NOT NULL DEFAULT 'unknown'"),
            ("tools", "auth_mode", "VARCHAR(20) NOT NULL DEFAULT 'none'"),
            ("tools", "client_id_required", "BOOLEAN NOT NULL DEFAULT false" if dialect == "postgresql" else "BOOLEAN NOT NULL DEFAULT 0"),
        ]

        for table, col, dtype in migrations:
            if col not in tool_cols:
                _add_column(db, table, col, dtype)

    # OAuth client records: Fernet tokens for a 64-char secret are ~184 chars and
    # Postgres enforces VARCHAR length, so a column born as VARCHAR(128) must be
    # widened or client_secret_post registration 500s. Also adds jwks_json for
    # private_key_jwt clients. Idempotent (ALTER .. TYPE to the same width is a
    # no-op; ADD COLUMN is guarded by column presence).
    try:
        oauth_cols = _columns(db, "oauth_clients")
    except Exception as exc:
        print(f"[singularity] migration: cannot inspect oauth_clients table — {exc}")
        return

    if "client_secret_hash" in oauth_cols and dialect == "postgresql":
        db.execute(text("ALTER TABLE oauth_clients ALTER COLUMN client_secret_hash TYPE VARCHAR(512)"))
        db.commit()
        print("[singularity] migration: widened oauth_clients.client_secret_hash (VARCHAR(128) -> VARCHAR(512))")
    if "jwks_json" not in oauth_cols:
        _add_column(db, "oauth_clients", "jwks_json", "TEXT")

    # Outbound dynamic clients: remember the token-endpoint auth method chosen at
    # registration so token exchange/refresh re-authenticate the same way.
    if "remote_oauth_clients" in already:
        try:
            remote_cols = _columns(db, "remote_oauth_clients")
        except Exception as exc:
            print(f"[singularity] migration: cannot inspect remote_oauth_clients table — {exc}")
            remote_cols = set()
        if "token_auth_method" not in remote_cols:
            _add_column(db, "remote_oauth_clients", "token_auth_method", "VARCHAR(32)")
