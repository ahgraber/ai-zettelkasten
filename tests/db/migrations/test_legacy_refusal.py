"""Tests for the pre-rebuild database refusal in ``run_migrations``.

Covers the ``schema-migrations`` spec scenario "A legacy-stamped database is
refused": a database from before the rebuild — whether stamped with a
revision the tree no longer knows, or carrying tables with no stamp at all —
must not be migrated. ``run_migrations`` refuses before writing anything, and
a fresh or already-current database proceeds untouched.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import shutil
import sqlite3

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

from aizk.db.migrations import LegacyDatabaseError, run_migrations

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "migration_snapshots"
_LEGACY_DB = _FIXTURE_DIR / "legacy_a8c9d0e1f2b3.db"
_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent


def _digest(db_path: Path) -> str:
    """Hash the database file's bytes so a refused run's "unchanged" claim is exact."""
    return hashlib.sha256(db_path.read_bytes()).hexdigest()


def test_legacy_stamped_database_refuses_and_is_unchanged(tmp_path: Path) -> None:
    """A database stamped with a revision the tree no longer knows is refused."""
    db_path = tmp_path / "legacy.db"
    shutil.copyfile(_LEGACY_DB, db_path)
    url = f"sqlite:///{db_path}"

    before = _digest(db_path)

    with pytest.raises(LegacyDatabaseError) as exc_info:
        run_migrations(url)

    message = str(exc_info.value)
    assert "a8c9d0e1f2b3" in message
    assert "Rebuilding the database" in message

    assert _digest(db_path) == before


def test_unstamped_database_with_tables_refuses_and_is_unchanged(tmp_path: Path) -> None:
    """A database with tables but no ``alembic_version`` row is refused."""
    db_path = tmp_path / "unstamped.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    url = f"sqlite:///{db_path}"

    before = _digest(db_path)

    with pytest.raises(LegacyDatabaseError) as exc_info:
        run_migrations(url)

    message = str(exc_info.value)
    assert "Rebuilding the database" in message

    assert _digest(db_path) == before


def test_fresh_database_upgrades_to_head(tmp_path: Path) -> None:
    """An empty database — the pave's first run — migrates without error."""
    db_path = tmp_path / "fresh.db"
    url = f"sqlite:///{db_path}"

    run_migrations(url)

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    expected_head = ScriptDirectory.from_config(cfg).get_current_head()

    conn = sqlite3.connect(db_path)
    try:
        version_num = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()
    assert version_num == expected_head


def test_database_already_at_head_runs_again_without_error(tmp_path: Path) -> None:
    """Running the migration runner again against a current database is a no-op."""
    url = f"sqlite:///{tmp_path / 'current.db'}"

    run_migrations(url)
    run_migrations(url)  # second call must not raise
