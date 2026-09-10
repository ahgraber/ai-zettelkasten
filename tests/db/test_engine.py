"""Unit tests for the shared database engine helpers."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlmodel import Session

import aizk.conversion.datamodel  # noqa: F401 — registers conversion tables on SQLModel.metadata
from aizk.db.engine import begin_immediate, begin_immediate_on, create_db_and_tables, get_engine


def test_create_db_and_tables(tmp_path):
    db_path = tmp_path / "conversion.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_db_and_tables(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"sources", "conversion_jobs", "conversion_outputs"}.issubset(tables)


@pytest.fixture
def notes_engine(tmp_path: Path) -> Engine:
    """A file-backed SQLite engine with one plain ``notes`` table."""
    engine = get_engine(f"sqlite:///{tmp_path / 'notes.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"))
    return engine


def _note_count(engine: Engine) -> int:
    """Count ``notes`` rows over a fresh connection, outside any open session."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM notes")).scalar_one()


def test_begin_immediate_commits_on_clean_exit(notes_engine: Engine) -> None:
    """Work staged inside the block is visible to other connections once the block exits."""
    with begin_immediate(notes_engine) as session:
        session.execute(text("INSERT INTO notes (body) VALUES ('kept')"))
        assert _note_count(notes_engine) == 0

    assert _note_count(notes_engine) == 1


def test_begin_immediate_rolls_back_on_exception(notes_engine: Engine) -> None:
    """An exception inside the block discards the staged work and propagates."""
    with pytest.raises(RuntimeError, match="boom"), begin_immediate(notes_engine) as session:
        session.execute(text("INSERT INTO notes (body) VALUES ('dropped')"))
        raise RuntimeError("boom")

    assert _note_count(notes_engine) == 0


def test_begin_immediate_on_leaves_the_callers_session_open_until_it_commits(notes_engine: Engine) -> None:
    """The session form starts the transaction and hands control back to the caller."""
    session = Session(notes_engine)
    try:
        begin_immediate_on(session)
        session.execute(text("INSERT INTO notes (body) VALUES ('pending')"))

        assert session.in_transaction()
        assert _note_count(notes_engine) == 0

        session.commit()

        assert _note_count(notes_engine) == 1
        assert session.execute(text("SELECT COUNT(*) FROM notes")).scalar_one() == 1
    finally:
        session.close()
