"""Unit tests for the rename-bookmarks-to-sources migration backfill."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import Engine, create_engine, text


@pytest.fixture()
def pre_migration_engine(tmp_path) -> Engine:
    """Build a database at the pre-rename migration state (b7f8e9a0c1d2)."""
    from aizk.conversion.db import get_engine
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "pre_migration.db"
    db_url = f"sqlite:///{db_path}"

    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", "src/aizk/conversion/migrations")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "b7f8e9a0c1d2")

    return get_engine(db_url)


def test_migration_backfills_source_ref_and_hash(pre_migration_engine: Engine, tmp_path):
    """Existing bookmark rows get a KarakeepBookmarkRef source_ref + matching hash."""
    from alembic import command
    from alembic.config import Config

    # Insert a legacy bookmarks row before running the rename migration.
    karakeep_id = "bm_backfill_test"
    row_uuid = uuid.uuid4()
    with pre_migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bookmarks"
                " (karakeep_id, aizk_uuid, url, normalized_url, title, content_type,"
                " source_type, created_at, updated_at)"
                " VALUES (:kid, :uuid, :url, :nurl, :title, :ct, :st, :now, :now)"
            ),
            {
                "kid": karakeep_id,
                "uuid": row_uuid.hex,  # SQLite stores as hex without hyphens
                "url": "https://example.com",
                "nurl": "https://example.com",
                "title": "Test",
                "ct": "html",
                "st": "other",
                "now": "2025-01-01 00:00:00",
            },
        )

    # Run the rename migration.
    db_url = pre_migration_engine.url.render_as_string(hide_password=False)
    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", "src/aizk/conversion/migrations")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "c3d4e5f6a7b8")

    # Verify: bookmarks table is gone, sources table has the row with backfilled columns.
    with pre_migration_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert "sources" in tables
        assert "bookmarks" not in tables

        row = conn.execute(
            text(
                "SELECT karakeep_id, source_ref, source_ref_hash FROM sources"
                " WHERE karakeep_id = :kid"
            ),
            {"kid": karakeep_id},
        ).fetchone()
        assert row is not None

        source_ref = json.loads(row.source_ref)
        assert source_ref == {"kind": "karakeep_bookmark", "bookmark_id": karakeep_id}

        expected_payload = {"kind": "karakeep_bookmark", "bookmark_id": karakeep_id}
        expected_hash = hashlib.sha256(
            json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert row.source_ref_hash == expected_hash


# ---------------------------------------------------------------------------
# Downgrade guard — prevents silent data loss when non-karakeep rows exist
# ---------------------------------------------------------------------------


def _alembic_cfg_for(db_url: str):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "src/aizk/conversion/migrations")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _apply_rename_migration(pre_migration_engine: Engine) -> str:
    """Upgrade to c3d4e5f6a7b8 and return the DB URL."""
    from alembic import command

    db_url = pre_migration_engine.url.render_as_string(hide_password=False)
    command.upgrade(_alembic_cfg_for(db_url), "c3d4e5f6a7b8")
    return db_url


def test_downgrade_succeeds_when_only_karakeep_rows_present(pre_migration_engine: Engine):
    """Downgrade is safe when every source row is a karakeep-bookmark."""
    from alembic import command

    with pre_migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bookmarks"
                " (karakeep_id, aizk_uuid, url, normalized_url, title, content_type,"
                " source_type, created_at, updated_at)"
                " VALUES (:kid, :uuid, :url, :nurl, :title, :ct, :st, :now, :now)"
            ),
            {
                "kid": "bm_downgrade_ok",
                "uuid": uuid.uuid4().hex,
                "url": "https://example.com",
                "nurl": "https://example.com",
                "title": "Test",
                "ct": "html",
                "st": "other",
                "now": "2025-01-01 00:00:00",
            },
        )

    db_url = _apply_rename_migration(pre_migration_engine)

    # Should not raise.
    command.downgrade(_alembic_cfg_for(db_url), "b7f8e9a0c1d2")

    with pre_migration_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert "bookmarks" in tables
        assert "sources" not in tables


def test_downgrade_aborts_when_non_karakeep_source_rows_exist(pre_migration_engine: Engine):
    """A source_ref with ``kind != 'karakeep_bookmark'`` must block the downgrade.

    Regression for the silent-data-loss foot-gun: the pre-refactor schema
    cannot represent a url/arxiv/inline_html source, so dropping source_ref
    would lose those rows forever.
    """
    from alembic import command

    db_url = _apply_rename_migration(pre_migration_engine)

    # After the rename, sources has the backfilled karakeep row (from the fixture's
    # own INSERT) — but the fixture didn't seed one. Insert a non-karakeep row
    # directly against the post-rename schema.
    non_karakeep_ref = json.dumps({"kind": "url", "url": "https://direct.example.com"})
    non_karakeep_hash = hashlib.sha256(
        json.dumps({"kind": "url", "url": "https://direct.example.com"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pre_migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources"
                " (karakeep_id, aizk_uuid, url, normalized_url, title, content_type,"
                " source_type, source_ref, source_ref_hash, created_at, updated_at)"
                " VALUES (NULL, :uuid, :url, :nurl, :title, :ct, :st, :ref, :hash, :now, :now)"
            ),
            {
                "uuid": uuid.uuid4().hex,
                "url": "https://direct.example.com",
                "nurl": "https://direct.example.com",
                "title": "Direct",
                "ct": "html",
                "st": "other",
                "ref": non_karakeep_ref,
                "hash": non_karakeep_hash,
                "now": "2025-01-01 00:00:00",
            },
        )

    with pytest.raises(RuntimeError, match="non-karakeep source_ref kinds"):
        command.downgrade(_alembic_cfg_for(db_url), "b7f8e9a0c1d2")

    # Downgrade aborted before any DDL ran — sources table is still present.
    with pre_migration_engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert "sources" in tables
        assert "bookmarks" not in tables


def test_downgrade_aborts_when_source_has_null_karakeep_id(pre_migration_engine: Engine):
    """A NULL karakeep_id cannot survive the NOT NULL tightening on downgrade."""
    from alembic import command

    db_url = _apply_rename_migration(pre_migration_engine)

    # Insert a karakeep-kind ref but with NULL karakeep_id (legal post-refactor).
    ref_payload = {"kind": "karakeep_bookmark", "bookmark_id": "orphan"}
    ref_json = json.dumps(ref_payload)
    ref_hash = hashlib.sha256(
        json.dumps(ref_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pre_migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources"
                " (karakeep_id, aizk_uuid, url, normalized_url, title, content_type,"
                " source_type, source_ref, source_ref_hash, created_at, updated_at)"
                " VALUES (NULL, :uuid, :url, :nurl, :title, :ct, :st, :ref, :hash, :now, :now)"
            ),
            {
                "uuid": uuid.uuid4().hex,
                "url": "https://example.com",
                "nurl": "https://example.com",
                "title": "Orphan",
                "ct": "html",
                "st": "other",
                "ref": ref_json,
                "hash": ref_hash,
                "now": "2025-01-01 00:00:00",
            },
        )

    with pytest.raises(RuntimeError, match="NULL karakeep_id"):
        command.downgrade(_alembic_cfg_for(db_url), "b7f8e9a0c1d2")
