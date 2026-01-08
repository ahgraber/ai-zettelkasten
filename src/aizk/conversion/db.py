"""Database utilities for the conversion service."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

import aizk.conversion.datamodel  # noqa: F401
from aizk.conversion.utilities.config import ConversionConfig


def _configure_sqlite_pragmas(engine: Engine) -> None:
    """Apply SQLite PRAGMA settings for WAL and concurrency."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


_ENGINE_CACHE: dict[str, Engine] = {}


def get_engine(database_url: str | None = None) -> Engine:
    """Create a database engine with SQLite tuning when applicable."""
    if database_url is None:
        config = ConversionConfig()
        database_url = config.database_url

    if engine := _ENGINE_CACHE.get(database_url):
        return engine

    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(
        database_url,
        connect_args=connect_args,
    )
    if database_url.startswith("sqlite"):
        _configure_sqlite_pragmas(engine)
    _ENGINE_CACHE[database_url] = engine
    return engine


def get_session(engine: Engine | None = None) -> Iterator[Session]:
    """Yield a SQLModel session for dependency injection."""
    engine = engine or get_engine()
    with Session(engine) as session:
        yield session


def create_db_and_tables(engine: Engine | None = None) -> None:
    """Create database tables for conversion service models."""
    engine = engine or get_engine()
    SQLModel.metadata.create_all(engine)
