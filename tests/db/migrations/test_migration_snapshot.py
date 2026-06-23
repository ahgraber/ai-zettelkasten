"""Migration coverage for the generated sanitized legacy database snapshot."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "migration_snapshots"
_LEGACY_DB = _FIXTURE_DIR / "legacy_a8c9d0e1f2b3.db"
_MANIFEST = _FIXTURE_DIR / "legacy_a8c9d0e1f2b3.manifest.json"
_LEGACY_REVISION = "a8c9d0e1f2b3"


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _scalar_map(conn, sql: str) -> dict[str, int]:
    return {str(key): int(count) for key, count in conn.execute(text(sql)).fetchall()}


def test_sanitized_legacy_snapshot_upgrades_to_head(tmp_path: Path) -> None:
    """The generated legacy snapshot upgrades to head without losing core invariants."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    working_db = tmp_path / _LEGACY_DB.name
    shutil.copy2(_LEGACY_DB, working_db)

    db_url = f"sqlite:///{working_db}"
    engine = create_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _LEGACY_REVISION
        assert (
            _scalar_map(conn, "SELECT status, COUNT(*) FROM conversion_jobs GROUP BY status")
            == manifest["job_status_counts"]
        )
        assert (
            _scalar_map(conn, "SELECT kind, COUNT(*) FROM conversion_job_events GROUP BY kind")
            == manifest["event_kind_counts"]
        )
        assert conn.execute(text("SELECT COUNT(*) FROM sources")).scalar_one() == manifest["row_counts"]["sources"]
        assert (
            conn.execute(text("SELECT COUNT(*) FROM conversion_jobs")).scalar_one()
            == manifest["row_counts"]["conversion_jobs"]
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM conversion_outputs")).scalar_one()
            == manifest["row_counts"]["conversion_outputs"]
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM conversion_job_events")).scalar_one()
            == manifest["row_counts"]["conversion_job_events"]
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM sources WHERE url IS NOT NULL AND url NOT LIKE 'https://example.invalid/%'")
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM sources WHERE karakeep_id NOT LIKE 'fixture_%'")).scalar_one() == 0
        )

    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "head")
    expected_head = ScriptDirectory.from_config(cfg).get_current_head()

    upgraded = inspect(create_engine(db_url))
    tables = set(upgraded.get_table_names())
    assert "conversion_job_events" not in tables
    assert "pipeline_events" in tables

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == expected_head
        assert conn.execute(text("SELECT COUNT(*) FROM sources")).scalar_one() == manifest["row_counts"]["sources"]
        assert (
            conn.execute(text("SELECT COUNT(*) FROM conversion_jobs")).scalar_one()
            == manifest["row_counts"]["conversion_jobs"]
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM conversion_outputs")).scalar_one()
            == manifest["row_counts"]["conversion_outputs"]
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM pipeline_events WHERE stage = 'conversion'")).scalar_one()
            == (manifest["row_counts"]["conversion_job_events"])
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM pipeline_events WHERE stage = 'conversion' AND work_unit_ref = 'None'")
            ).scalar_one()
            == manifest["orphan_event_count"]
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM pipeline_events WHERE stage = 'conversion' AND source_id = :uuid"),
                {"uuid": manifest["audit_source_uuid"]},
            ).scalar_one()
            == manifest["audit_source_event_count"]
        )
        assert (
            conn.execute(
                text(
                    "SELECT COUNT(DISTINCT owner_id) FROM conversion_jobs "
                    "WHERE idempotency_key = 'shared-cross-owner-key'"
                )
            ).scalar_one()
            == 2
        )

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == expected_head
        assert (
            conn.execute(text("SELECT COUNT(*) FROM pipeline_events WHERE stage = 'conversion'")).scalar_one()
            == (manifest["row_counts"]["conversion_job_events"])
        )
