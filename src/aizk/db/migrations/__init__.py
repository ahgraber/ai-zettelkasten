"""Alembic migration helpers for the shared database foundation.

One migration tree governs every stage's tables; ``env.py`` registers the full
schema across stages so autogenerate and the equivalence check see all tables.
"""

from __future__ import annotations

from pathlib import Path


class UnversionedSchemaError(RuntimeError):
    """Raised when the DB has existing tables but no ``alembic_version`` entry.

    This means Alembic would try to run the baseline migration against a
    pre-existing schema, crash mid-migration, and leave the DB in a broken
    state.  Stamp the database at the correct revision to fix it::

        uv run python - <<'EOF'
        from alembic import command
        from alembic.config import Config
        from pathlib import Path

        cfg = Config()
        cfg.set_main_option(
            "script_location",
            str(Path("src/aizk/db/migrations").resolve()),
        )
        command.stamp(cfg, "<REVISION>")
        EOF

    To find ``<REVISION>``, inspect the schema::

        DB=data/conversion_service.db
        sqlite3 $DB 'PRAGMA table_info(conversion_jobs);' | grep error_detail
        sqlite3 $DB "SELECT name FROM sqlite_master
                     WHERE type='index'
                     AND name='ix_conversion_jobs_status_next_attempt_queued';"

    Revision map (pick the *highest* that matches):

    * ``57317cf19d3b`` — baseline (no ``error_detail``)
    * ``a1b2c3d4e5f6`` — ``error_detail`` exists, no composite status index
    * ``b7f8e9a0c1d2`` — composite status index exists, ``bookmarks`` still present
    * ``c1d2e3f4a5b6`` — ``bookmarks`` renamed to ``sources``
    * ``d5e6f7a8b9c0`` — ``source_ref`` NOT NULL enforced
    * ``e6f7a8b9c0d1`` — ``owner_id`` columns added
    * ``f7a8b9c0d1e2`` — owner-scoped idempotency key (HEAD)
    """


def _assert_versioned(database_url: str) -> None:
    """Raise :exc:`UnversionedSchemaError` if schema exists but has no version record.

    A missing or empty ``alembic_version`` table against a non-empty schema
    means Alembic will attempt to re-run the baseline migration and crash.
    Detecting this before ``command.upgrade()`` produces a clear, actionable
    error instead of a mid-migration ``OperationalError``.
    """
    import sqlalchemy as sa
    from sqlalchemy import pool

    engine = sa.create_engine(database_url, poolclass=pool.NullPool)
    try:
        inspector = sa.inspect(engine)
        tables = set(inspector.get_table_names())

        if not tables:
            return  # completely fresh DB — Alembic will create everything

        if inspector.has_table("alembic_version"):
            with engine.connect() as conn:
                row = conn.execute(sa.text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
                if row:
                    return  # versioned and tracked — normal path

        schema_tables = tables - {"alembic_version"}
        if schema_tables:
            raise UnversionedSchemaError(
                f"Database has existing tables {sorted(schema_tables)!r} but no alembic_version entry. "
                "Stamp the database at the correct revision before starting the service. "
                "See UnversionedSchemaError docstring for the stamping procedure."
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
        UnversionedSchemaError: If the database has an existing schema with no
            ``alembic_version`` entry.  Stamp the database first; see the
            exception docstring for instructions.
    """
    from alembic import command
    from alembic.config import Config

    from aizk.db.config import DatabaseConfig

    effective_url = database_url or DatabaseConfig().database_url

    _assert_versioned(effective_url)

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent))
    cfg.set_main_option("sqlalchemy.url", effective_url)
    command.upgrade(cfg, "head")
