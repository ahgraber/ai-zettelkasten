"""Alembic environment configuration for conversion service migrations."""

from __future__ import annotations

from alembic import context
from sqlalchemy import pool
from sqlmodel import SQLModel, create_engine

import aizk.conversion.datamodel  # noqa: F401 — registers models on SQLModel.metadata
from aizk.conversion.utilities.config import ConversionConfig

target_metadata = SQLModel.metadata


def _database_url() -> str:
    """Return the database URL, preferring the alembic config override."""
    alembic_cfg = context.config
    return alembic_cfg.get_main_option("sqlalchemy.url") or ConversionConfig().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode for SQL script generation."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
