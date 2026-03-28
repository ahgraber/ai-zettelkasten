"""Alembic migration helpers for the conversion service."""

from __future__ import annotations

from pathlib import Path


def run_migrations(database_url: str | None = None) -> None:
    """Run Alembic migrations to head.

    Constructs the Alembic config programmatically so callers don't need
    ``alembic.ini`` on disk or a specific working directory.

    Args:
        database_url: Override the database URL. When *None*, ``env.py``
            falls back to :class:`~aizk.conversion.utilities.config.ConversionConfig`.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
