"""Unit tests for the shared database engine helpers."""

from sqlalchemy import inspect

import aizk.conversion.datamodel  # noqa: F401 — registers conversion tables on SQLModel.metadata
from aizk.db.engine import create_db_and_tables, get_engine


def test_create_db_and_tables(tmp_path):
    db_path = tmp_path / "conversion.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_db_and_tables(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"sources", "conversion_jobs", "conversion_outputs"}.issubset(tables)
