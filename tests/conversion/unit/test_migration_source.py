"""Tests for the c1d2e3f4a5b6 rename_bookmarks_to_sources migration.

Covers upgrade, backfill correctness, idempotency, collision detection,
downgrade round-trip, and idempotency_key recomputation.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

from aizk.conversion.core.errors import IrreversibleMigrationError
from aizk.conversion.utilities.hashing import compute_idempotency_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "src" / "aizk" / "conversion" / "migrations"

_PREV_REVISION = "b7f8e9a0c1d2"
_THIS_REVISION = "c1d2e3f4a5b6"


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _sha256_hex(text_: str) -> str:
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def _db_url(tmp_path: Path, name: str = "test.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _apply_up_to_prev(cfg: Config) -> None:
    """Upgrade to the revision just before the one under test."""
    command.upgrade(cfg, _PREV_REVISION)


def _insert_bookmark(conn, *, karakeep_id: str, aizk_uuid: str | None = None) -> str:
    """Insert a row into bookmarks and return the aizk_uuid used."""
    aizk_uuid = aizk_uuid or str(uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        text(
            "INSERT INTO bookmarks "
            "(karakeep_id, aizk_uuid, url, normalized_url, title, "
            "content_type, source_type, created_at, updated_at) "
            "VALUES (:kid, :uuid, :url, :nurl, :title, :ct, :st, :ca, :ua)"
        ),
        {
            "kid": karakeep_id,
            "uuid": aizk_uuid,
            "url": f"https://example.com/{karakeep_id}",
            "nurl": f"https://example.com/{karakeep_id}",
            "title": karakeep_id,
            "ct": "html",
            "st": "other",
            "ca": now,
            "ua": now,
        },
    )
    return aizk_uuid


def _insert_job(conn, *, aizk_uuid: str, idempotency_key: str) -> int:
    """Insert a conversion_jobs row and return its id."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = conn.execute(
        text(
            "INSERT INTO conversion_jobs "
            "(aizk_uuid, title, payload_version, status, attempts, "
            "idempotency_key, created_at, updated_at) "
            "VALUES (:uuid, 'test', 1, 'QUEUED', 0, :key, :ca, :ua)"
        ),
        {"uuid": aizk_uuid, "key": idempotency_key, "ca": now, "ua": now},
    )
    return result.lastrowid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sources_table_exists_after_migration(tmp_path):
    """After upgrade, 'sources' table must exist and 'bookmarks' must not."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, _THIS_REVISION)

    engine = create_engine(url)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert "sources" in table_names, "sources table should exist after migration"
    assert "bookmarks" not in table_names, "bookmarks table should be gone after migration"


def test_sources_has_new_columns(tmp_path):
    """After upgrade, sources table has source_ref and source_ref_hash columns."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, _THIS_REVISION)

    engine = create_engine(url)
    inspector = inspect(engine)
    col_names = {c["name"] for c in inspector.get_columns("sources")}

    assert "source_ref" in col_names
    assert "source_ref_hash" in col_names


def test_karakeep_id_is_nullable(tmp_path):
    """After upgrade, sources.karakeep_id must be nullable."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, _THIS_REVISION)

    engine = create_engine(url)
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("sources")}

    assert cols["karakeep_id"]["nullable"] is True, "karakeep_id must be nullable in sources"


def test_backfill_populates_source_ref_and_hash(tmp_path):
    """Upgrade backfills source_ref='karakeep:<id>' and correct sha256 hash."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    karakeep_id = "test_bm_001"
    with engine.begin() as conn:
        _insert_bookmark(conn, karakeep_id=karakeep_id)

    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT source_ref, source_ref_hash FROM sources WHERE karakeep_id = :kid"),
            {"kid": karakeep_id},
        ).fetchone()

    assert row is not None
    import json as _json

    expected_ref = _json.dumps(
        {"kind": "karakeep_bookmark", "bookmark_id": karakeep_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_hash = _sha256_hex(expected_ref)
    assert row[0] == expected_ref, f"source_ref mismatch: {row[0]!r} != {expected_ref!r}"
    assert row[1] == expected_hash, f"source_ref_hash mismatch: {row[1]!r} != {expected_hash!r}"


def test_backfill_is_idempotent(tmp_path):
    """Running upgrade a second time on an already-migrated DB gives the same result."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    karakeep_id = "test_bm_idempotent"
    with engine.begin() as conn:
        _insert_bookmark(conn, karakeep_id=karakeep_id)

    # First upgrade
    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        row1 = conn.execute(
            text("SELECT source_ref, source_ref_hash FROM sources WHERE karakeep_id = :kid"),
            {"kid": karakeep_id},
        ).fetchone()

    # Second upgrade (no-op — already at head)
    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        row2 = conn.execute(
            text("SELECT source_ref, source_ref_hash FROM sources WHERE karakeep_id = :kid"),
            {"kid": karakeep_id},
        ).fetchone()

    assert row1 == row2, "Backfill changed values on second upgrade run"


def test_backfill_collision_assertion_fires(tmp_path, monkeypatch):
    """Duplicate karakeep_id values cause the migration to raise RuntimeError."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    # Manually insert two rows with the same karakeep_id (bypassing unique constraint)
    # by inserting into SQLite directly with PRAGMA foreign_keys=OFF.
    raw_db = str(tmp_path / "test.db")
    conn_raw = sqlite3.connect(raw_db)
    conn_raw.execute("PRAGMA foreign_keys=OFF")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Insert first row normally via engine
    engine = create_engine(url)
    with engine.begin() as conn:
        _insert_bookmark(conn, karakeep_id="dup_bm", aizk_uuid=str(uuid4()))

    # Force a second row with the same karakeep_id by dropping the unique index first
    conn_raw.execute("DROP INDEX IF EXISTS ix_bookmarks_karakeep_id")
    conn_raw.execute(
        "INSERT INTO bookmarks (karakeep_id, aizk_uuid, url, normalized_url, title, "
        "content_type, source_type, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "dup_bm",
            str(uuid4()),
            "https://example.com/dup",
            "https://example.com/dup",
            "dup",
            "html",
            "other",
            now,
            now,
        ),
    )
    conn_raw.commit()
    conn_raw.close()

    # The migration should detect the hash collision and raise
    with pytest.raises(Exception, match="collision|Collision|duplicate|UNIQUE"):
        command.upgrade(cfg, _THIS_REVISION)


def test_downgrade_round_trips_cleanly(tmp_path):
    """Upgrade then downgrade to prev revision leaves bookmarks table and no sources."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    with engine.begin() as conn:
        _insert_bookmark(conn, karakeep_id="round_trip_bm")

    command.upgrade(cfg, _THIS_REVISION)
    command.downgrade(cfg, _PREV_REVISION)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert "bookmarks" in table_names, "bookmarks table should be restored after downgrade"
    assert "sources" not in table_names, "sources table should be gone after downgrade"

    with engine.connect() as conn:
        row = conn.execute(text("SELECT karakeep_id FROM bookmarks WHERE karakeep_id = 'round_trip_bm'")).fetchone()
    assert row is not None, "Row should survive round-trip"


def test_downgrade_aborts_when_karakeep_id_is_null(tmp_path):
    """Downgrade raises IrreversibleMigrationError when any source row has karakeep_id IS NULL."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    with engine.begin() as conn:
        _insert_bookmark(conn, karakeep_id="normal_bm")

    command.upgrade(cfg, _THIS_REVISION)

    # Insert a sources row with NULL karakeep_id (post-migration, possible for new source kinds)
    with engine.begin() as conn:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            text(
                "INSERT INTO sources (karakeep_id, aizk_uuid, source_ref, source_ref_hash, "
                "url, normalized_url, title, content_type, source_type, created_at, updated_at) "
                "VALUES (NULL, :uuid, :ref, :hash, NULL, NULL, 'null-source', NULL, NULL, :ca, :ua)"
            ),
            {
                "uuid": str(uuid4()),
                "ref": "url:https://example.com/null",
                "hash": _sha256_hex("url:https://example.com/null"),
                "ca": now,
                "ua": now,
            },
        )

    with pytest.raises(IrreversibleMigrationError):
        command.downgrade(cfg, _PREV_REVISION)


# Frozen snapshot used by the migration — must stay in sync with the migration.
_MIGRATION_FROZEN_SNAPSHOT = {
    "pdf_max_pages": 250,
    "ocr_enabled": True,
    "table_structure_enabled": True,
    "picture_description_model": "openai/gpt-5.4-nano",
    "picture_timeout": 180.0,
    "picture_classification_enabled": True,
    "picture_description_enabled": False,
}


def _source_ref_hash_for(karakeep_id: str) -> str:
    import json as _json

    source_ref = _json.dumps(
        {"kind": "karakeep_bookmark", "bookmark_id": karakeep_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_hex(source_ref)


def _expected_canonical_key(karakeep_id: str) -> str:
    """Clean formula matching compute_idempotency_key() — used for the canonical job."""
    import json as _json

    source_ref_hash = _source_ref_hash_for(karakeep_id)
    config_json = _json.dumps(_MIGRATION_FROZEN_SNAPSHOT, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(f"{source_ref_hash}:docling:{config_json}")


def _expected_suffixed_key(karakeep_id: str, job_id: int) -> str:
    """Suffixed formula — used for non-canonical (extra) historical jobs per source."""
    import json as _json

    source_ref_hash = _source_ref_hash_for(karakeep_id)
    config_json = _json.dumps(_MIGRATION_FROZEN_SNAPSHOT, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(f"{source_ref_hash}:docling:{config_json}:job_{job_id}")


def test_idempotency_key_recomputed_with_new_formula(tmp_path):
    """After upgrade, a single job per source gets the clean formula key."""
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    karakeep_id = "idem_bm_001"
    old_key = "a" * 64
    with engine.begin() as conn:
        aizk_uuid = _insert_bookmark(conn, karakeep_id=karakeep_id)
        _insert_job(conn, aizk_uuid=aizk_uuid, idempotency_key=old_key)

    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT cj.idempotency_key FROM conversion_jobs cj "
                "JOIN sources s ON s.aizk_uuid = cj.aizk_uuid "
                "WHERE s.karakeep_id = :kid"
            ),
            {"kid": karakeep_id},
        ).fetchone()

    assert row is not None
    expected_new_key = _expected_canonical_key(karakeep_id)
    assert row[0] == expected_new_key, (
        f"idempotency_key mismatch:\n  got:      {row[0]}\n  expected: {expected_new_key}"
    )


def test_idempotency_key_no_collision_for_multiple_jobs_same_source(tmp_path):
    """Two historical jobs for the same source: canonical gets the clean key, extra gets suffixed.

    The canonical job (highest id, no SUCCEEDED bias here since both are QUEUED) must
    get the clean formula matching compute_idempotency_key(), while the non-canonical
    job gets the suffixed formula.  The unique index must not be violated.
    """
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    karakeep_id = "idem_bm_multi"
    with engine.begin() as conn:
        aizk_uuid = _insert_bookmark(conn, karakeep_id=karakeep_id)
        job_id_1 = _insert_job(conn, aizk_uuid=aizk_uuid, idempotency_key="b" * 64)
        job_id_2 = _insert_job(conn, aizk_uuid=aizk_uuid, idempotency_key="c" * 64)

    # Must not raise a unique constraint error.
    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT cj.id, cj.idempotency_key FROM conversion_jobs cj "
                "JOIN sources s ON s.aizk_uuid = cj.aizk_uuid "
                "WHERE s.karakeep_id = :kid "
                "ORDER BY cj.id"
            ),
            {"kid": karakeep_id},
        ).fetchall()

    assert len(rows) == 2
    rows_by_id = {row[0]: row[1] for row in rows}
    key_1 = rows_by_id[job_id_1]
    key_2 = rows_by_id[job_id_2]
    assert key_1 != key_2, "Two jobs for the same source must receive distinct migrated keys"
    # job_id_2 has the higher id → canonical (sorted first within the source group).
    assert key_2 == _expected_canonical_key(karakeep_id), "Higher-id job should be canonical"
    assert key_1 == _expected_suffixed_key(karakeep_id, job_id_1), "Lower-id job should be suffixed"
    # Canonical key matches what compute_idempotency_key() produces.
    source_ref_hash = _source_ref_hash_for(karakeep_id)
    assert key_2 == compute_idempotency_key(source_ref_hash, "docling", _MIGRATION_FROZEN_SNAPSHOT)


def test_canonical_migrated_key_matches_fresh_submission_formula(tmp_path):
    """The canonical historical job's key equals what compute_idempotency_key() produces.

    A post-migration re-submission of the same bookmark with default config must
    hit this job rather than creating a new one.
    """
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    karakeep_id = "idem_bm_replay_continuity"
    with engine.begin() as conn:
        aizk_uuid = _insert_bookmark(conn, karakeep_id=karakeep_id)
        _insert_job(conn, aizk_uuid=aizk_uuid, idempotency_key="d" * 64)

    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        migrated_key = conn.execute(
            text(
                "SELECT cj.idempotency_key FROM conversion_jobs cj "
                "JOIN sources s ON s.aizk_uuid = cj.aizk_uuid "
                "WHERE s.karakeep_id = :kid"
            ),
            {"kid": karakeep_id},
        ).scalar_one()

    source_ref_hash = _source_ref_hash_for(karakeep_id)
    fresh_submission_key = compute_idempotency_key(source_ref_hash, "docling", _MIGRATION_FROZEN_SNAPSHOT)

    assert migrated_key == _expected_canonical_key(karakeep_id)
    assert migrated_key == fresh_submission_key


def test_succeeded_job_is_canonical_over_higher_id_failed_job(tmp_path):
    """When one job SUCCEEDED and a later job FAILED, the SUCCEEDED job is canonical.

    The SUCCEEDED job should receive the clean formula key even though the FAILED
    job has a higher id.
    """
    url = _db_url(tmp_path)
    cfg = _alembic_cfg(url)
    _apply_up_to_prev(cfg)

    engine = create_engine(url)
    karakeep_id = "idem_bm_succeeded_canonical"
    with engine.begin() as conn:
        aizk_uuid = _insert_bookmark(conn, karakeep_id=karakeep_id)
        # Insert SUCCEEDED job first (lower id)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result = conn.execute(
            text(
                "INSERT INTO conversion_jobs "
                "(aizk_uuid, title, payload_version, status, attempts, "
                "idempotency_key, created_at, updated_at) "
                "VALUES (:uuid, 'test', 1, 'SUCCEEDED', 1, :key, :ca, :ua)"
            ),
            {"uuid": aizk_uuid, "key": "e" * 64, "ca": now, "ua": now},
        )
        succeeded_job_id = result.lastrowid
        # Insert FAILED job second (higher id)
        result = conn.execute(
            text(
                "INSERT INTO conversion_jobs "
                "(aizk_uuid, title, payload_version, status, attempts, "
                "idempotency_key, created_at, updated_at) "
                "VALUES (:uuid, 'test', 1, 'PERMANENTLY_FAILED', 1, :key, :ca, :ua)"
            ),
            {"uuid": aizk_uuid, "key": "f" * 64, "ca": now, "ua": now},
        )
        failed_job_id = result.lastrowid

    command.upgrade(cfg, _THIS_REVISION)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT cj.id, cj.idempotency_key FROM conversion_jobs cj "
                "JOIN sources s ON s.aizk_uuid = cj.aizk_uuid "
                "WHERE s.karakeep_id = :kid ORDER BY cj.id"
            ),
            {"kid": karakeep_id},
        ).fetchall()

    rows_by_id = {row[0]: row[1] for row in rows}
    succeeded_key = rows_by_id[succeeded_job_id]
    failed_key = rows_by_id[failed_job_id]

    assert succeeded_key != failed_key
    # SUCCEEDED job is canonical regardless of id ordering.
    assert succeeded_key == _expected_canonical_key(karakeep_id)
    assert failed_key == _expected_suffixed_key(karakeep_id, failed_job_id)
    # Canonical key matches fresh submission formula.
    source_ref_hash = _source_ref_hash_for(karakeep_id)
    assert succeeded_key == compute_idempotency_key(source_ref_hash, "docling", _MIGRATION_FROZEN_SNAPSHOT)
