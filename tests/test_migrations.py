"""Regression tests for boot-time schema migrations.

The critical Postgres path (inspect().get_columns()) returns *dicts* whose
column name lives under the "name" key — not attribute access. A prior bug
used `r.column_name`, which silently aborted migrations on Postgres (and
thus never added execution_tier/requires_credential to an existing tools
table).
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect

from app.migrations import _columns, run as run_migrations

from sqlalchemy.orm import Session


class _PostgresBind:
    """Fake engine whose dialect reports postgresql."""

    dialect = SimpleNamespace(name="postgresql")


class _Sessionish:
    def __init__(self, inspector):
        self._inspector = inspector
        self._bind = _PostgresBind()

    def get_bind(self):
        return self._bind


def _fake_inspector(column_names):
    """Inspector whose get_columns returns dict-shaped column rows (as on PG)."""
    return SimpleNamespace(get_columns=lambda table: [{"name": n} for n in column_names])


def test_columns_reads_name_key_from_dict_rows(monkeypatch):
    """Regression: get_columns returns dicts; _columns must read ['name']."""
    inspector = _fake_inspector(["id", "slug", "execution_tier"])
    monkeypatch.setattr("app.migrations.inspect", lambda _bind: inspector)
    db = _Sessionish(inspector)
    assert _columns(db, "tools") == {"id", "slug", "execution_tier"}


def test_sqlite_branch_still_works():
    """SQLite PRAGMA path unchanged (row[1] = column name)."""
    from sqlalchemy import Column, Integer, MetaData, String, Table

    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("tools", metadata, Column("id", Integer), Column("slug", String))
    metadata.create_all(engine)

    from sqlalchemy.orm import Session

    with Session(engine) as db:
        assert _columns(db, "tools") == {"id", "slug"}


def test_postgres_boolean_default_uses_false(monkeypatch):
    """Regression: ALTER on Postgres must use `DEFAULT false`, not `DEFAULT 0`
    (PG rejects boolean DEFAULT 0 with DatatypeMismatch).
    """
    executed = []

    class _PgDialect:
        name = "postgresql"

    class _PgEngine:
        dialect = _PgDialect()

    class _PgSession:
        def get_bind(self):
            return _PgEngine()

        def execute(self, stmt):
            executed.append(str(stmt))
            return None

        def commit(self):
            pass

    inspector = _fake_inspector(["id", "slug", "name"])  # missing the two cols
    inspector.get_table_names = lambda: ["tools", "oauth_clients", "oauth_auth_codes", "oauth_tokens"]

    def fake_inspect(bind):
        return inspector

    monkeypatch.setattr("app.migrations.inspect", fake_inspect)
    run_migrations(_PgSession())
    alts = [e for e in executed if e.startswith("ALTER")]
    assert any("requires_credential" in e and "DEFAULT 0" in e for e in alts) is False
    assert any("requires_credential BOOLEAN NOT NULL DEFAULT false" in e for e in alts)


def test_run_migration_adds_columns_on_missing_drifted_schema():
    """End-to-end: run() ALTERs requires_credential + execution_tier when the
    tools table is missing them (simulates production Postgres schema drift
    that the previous duplicate-column-name SQL bug caused).
    """
    engine = create_engine("sqlite://")
    # Create a minimal tools table WITHOUT the two evolving columns — the
    # way the Supabase DB looked before the migration ever ran.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tools ("
            "  id TEXT PRIMARY KEY, slug TEXT, name TEXT, publisher TEXT,"
            "  publisher_verified BOOLEAN, category TEXT, description TEXT,"
            "  mcp_available BOOLEAN, pricing_tier TEXT, integrations TEXT,"
            "  permissions_requested TEXT, permissions_needed TEXT,"
            "  trust_score REAL, trust_flags TEXT, source TEXT,"
            "  embedding_dim INTEGER, embedding BLOB,"
            "  created_at TIMESTAMP, updated_at TIMESTAMP"
            ")"
        ))
    with Session(engine) as db:
        run_migrations(db)
    # Columns should now exist
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(tools)")).all()}
    assert "requires_credential" in cols
    assert "execution_tier" in cols