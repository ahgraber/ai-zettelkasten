"""Alembic migration helpers for the shared database foundation.

One migration tree governs every stage's tables; ``env.py`` registers the full
schema across stages so autogenerate and the equivalence check see all tables.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config


class LegacyDatabaseError(RuntimeError):
    """Raised when a database predates the migration tree's baseline revision.

    The tree holds one baseline revision, not the historical chain that
    produced an older schema, so a database from before the rebuild cannot be
    migrated — whether it carries an ``alembic_version`` stamp this tree does
    not recognize, or has tables with no stamp at all. Rebuild the database
    instead; see README.md, "Rebuilding the database".
    """


def _refuse_legacy_database(database_url: str, cfg: Config) -> None:
    """Raise :exc:`LegacyDatabaseError` if the database predates the baseline revision.

    A fresh database (no tables) and a database already stamped at a revision
    this tree knows both proceed untouched; either legacy shape — tables with
    no stamp, or a stamp naming an unknown revision — refuses before
    ``command.upgrade()`` writes anything.
    """
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError
    import sqlalchemy as sa
    from sqlalchemy import pool

    engine = sa.create_engine(database_url, poolclass=pool.NullPool)
    try:
        inspector = sa.inspect(engine)
        tables = set(inspector.get_table_names())

        if not tables:
            return  # completely fresh DB — Alembic will create everything

        version_num = None
        if inspector.has_table("alembic_version"):
            with engine.connect() as conn:
                row = conn.execute(sa.text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
                if row:
                    version_num = row[0]

        schema_tables = tables - {"alembic_version"}
        if version_num is None:
            if schema_tables:
                raise LegacyDatabaseError(
                    f"database at {database_url} has tables but no migration stamp; this version "
                    'does not migrate it; rebuild it (README.md, "Rebuilding the database")'
                )
            return

        script = ScriptDirectory.from_config(cfg)
        try:
            known_revision = script.get_revision(version_num) is not None
        except CommandError:
            known_revision = False
        if not known_revision:
            raise LegacyDatabaseError(
                f"database at {database_url} is stamped with revision {version_num}, which this "
                'version does not migrate; rebuild it (README.md, "Rebuilding the database")'
            )
    finally:
        engine.dispose()


def run_migrations(database_url: str | None = None) -> None:
    """Run Alembic migrations to head.

    Constructs the Alembic config programmatically so callers don't need
    ``alembic.ini`` on disk or a specific working directory.

    Args:
        database_url: Override the database URL. When *None*, falls back to
            :class:`~aizk.db.config.DatabaseConfig`.

    Raises:
        LegacyDatabaseError: If the database predates the tree's baseline
            revision.  Rebuild the database; see the exception docstring.
    """
    from alembic import command
    from alembic.config import Config

    from aizk.db.config import DatabaseConfig

    effective_url = database_url or DatabaseConfig().database_url

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent))
    cfg.set_main_option("sqlalchemy.url", effective_url)

    _refuse_legacy_database(effective_url, cfg)

    command.upgrade(cfg, "head")
