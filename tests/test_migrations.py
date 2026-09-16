"""Regression tests for boot-time schema migrations.

The critical Postgres path (inspect().get_columns()) returns *dicts* whose
column name lives under the "name" key — not attribute access. A prior bug
used `r.column_name`, which silently aborted migrations on Postgres (and
thus never added execution_tier/requires_credential to an existing tools
table).
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect

from app.migrations import _columns


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