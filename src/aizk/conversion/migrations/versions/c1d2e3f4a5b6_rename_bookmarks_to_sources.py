"""Rename bookmarks table to sources; add source_ref columns; update FKs.

Revision ID: c1d2e3f4a5b6
Revises: b7f8e9a0c1d2
Create Date: 2026-04-19 00:00:00.000000

SQLite FK notes
---------------
SQLite does not support ALTER TABLE ... DROP/ADD FOREIGN KEY.  The standard
Alembic workaround is ``batch_alter_table(recreate="always")``, but that
approach reads the *existing* DDL text to build the new table definition —
meaning any old FK clauses are silently carried forward.

Instead we use explicit CREATE TABLE … + INSERT SELECT … + DROP TABLE sequences
for the two child tables so that *only* the new FK (→ sources) appears in the
final DDL.  This keeps the migrated schema identical to SQLModel's
``metadata.create_all()`` output and satisfies the schema-parity test.
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes  # noqa: F401 — AutoString referenced in DDL

from aizk.conversion.core.errors import IrreversibleMigrationError

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b7f8e9a0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Canonical table definitions (used in both upgrade and downgrade)
# ---------------------------------------------------------------------------

_CONVERSION_JOBS_COLS = [
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("aizk_uuid", sa.Uuid(), nullable=False),
    sa.Column("title", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=False),
    sa.Column("payload_version", sa.Integer(), nullable=False),
    sa.Column(
        "status",
        sa.Enum(
            "NEW",
            "QUEUED",
            "RUNNING",
            "UPLOAD_PENDING",
            "SUCCEEDED",
            "FAILED_RETRYABLE",
            "FAILED_PERM",
            "CANCELLED",
            name="conversionjobstatus",
        ),
        nullable=False,
    ),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("error_code", sqlmodel.sql.sqltypes.AutoString(length=50), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("idempotency_key", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column("earliest_next_attempt_at", sa.DateTime(), nullable=True),
    sa.Column("last_error_at", sa.DateTime(), nullable=True),
    sa.Column("queued_at", sa.DateTime(), nullable=True),
    sa.Column("started_at", sa.DateTime(), nullable=True),
    sa.Column("finished_at", sa.DateTime(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.Column("error_detail", sa.Text(), nullable=True),
]

_CONVERSION_OUTPUTS_COLS = [
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("job_id", sa.Integer(), nullable=False),
    sa.Column("aizk_uuid", sa.Uuid(), nullable=False),
    sa.Column("title", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=False),
    sa.Column("payload_version", sa.Integer(), nullable=False),
    sa.Column("s3_prefix", sa.Text(), nullable=False),
    sa.Column("markdown_key", sa.Text(), nullable=False),
    sa.Column("manifest_key", sa.Text(), nullable=False),
    sa.Column("markdown_hash_xx64", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column("figure_count", sa.Integer(), nullable=False),
    sa.Column("docling_version", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column("pipeline_name", sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
]

# Column names shared between old conversion_jobs and the new version
_JOBS_COPY_COLS = (
    "id, aizk_uuid, title, payload_version, status, attempts, error_code, "
    "error_message, idempotency_key, earliest_next_attempt_at, last_error_at, "
    "queued_at, started_at, finished_at, created_at, updated_at, error_detail"
)

_OUTPUTS_COPY_COLS = (
    "id, job_id, aizk_uuid, title, payload_version, s3_prefix, markdown_key, "
    "manifest_key, markdown_hash_xx64, figure_count, docling_version, "
    "pipeline_name, created_at"
)


def upgrade() -> None:
    """Rename bookmarks → sources, make karakeep_id nullable, add source_ref columns,
    rebuild conversion_jobs/conversion_outputs with clean FK → sources,
    backfill source_ref + source_ref_hash, assert no hash collisions,
    add unique index on source_ref_hash, and recompute idempotency_key.
    """
    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Create the new `sources` table.
    # ------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("karakeep_id", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("aizk_uuid", sa.Uuid(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("source_ref_hash", sa.Text(), nullable=True),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("normalized_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column("content_type", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=True),
        sa.Column("source_type", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sources_aizk_uuid"), ["aizk_uuid"], unique=True)
        batch_op.create_index(batch_op.f("ix_sources_karakeep_id"), ["karakeep_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_sources_normalized_url"), ["normalized_url"], unique=False)

    # ------------------------------------------------------------------
    # 2. Copy all rows from bookmarks → sources.
    # ------------------------------------------------------------------
    conn.execute(
        sa.text(
            "INSERT INTO sources "
            "(id, karakeep_id, aizk_uuid, source_ref, source_ref_hash, "
            "url, normalized_url, title, content_type, source_type, "
            "created_at, updated_at) "
            "SELECT id, karakeep_id, aizk_uuid, NULL, NULL, "
            "url, normalized_url, title, content_type, source_type, "
            "created_at, updated_at "
            "FROM bookmarks"
        )
    )

    # ------------------------------------------------------------------
    # 3. Backfill source_ref + source_ref_hash; assert no collisions.
    # ------------------------------------------------------------------
    rows = conn.execute(sa.text("SELECT id, karakeep_id FROM sources WHERE karakeep_id IS NOT NULL")).fetchall()

    hash_to_id: dict[str, int] = {}
    for row_id, karakeep_id in rows:
        payload = {"kind": "karakeep_bookmark", "bookmark_id": karakeep_id}
        source_ref = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        source_ref_hash = _sha256_hex(source_ref)
        if source_ref_hash in hash_to_id:
            raise RuntimeError(
                f"Hash collision: source_ref_hash {source_ref_hash!r} "
                f"produced by both row {hash_to_id[source_ref_hash]} and row {row_id}"
            )
        hash_to_id[source_ref_hash] = row_id
        conn.execute(
            sa.text("UPDATE sources SET source_ref = :ref, source_ref_hash = :hash WHERE id = :id"),
            {"ref": source_ref, "hash": source_ref_hash, "id": row_id},
        )

    # ------------------------------------------------------------------
    # 4. Add unique index on source_ref_hash.
    # ------------------------------------------------------------------
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sources_source_ref_hash"), ["source_ref_hash"], unique=True)

    # ------------------------------------------------------------------
    # 5. Drop the old bookmarks table (child-table FKs reference bookmarks;
    #    we disable FK enforcement during the swap to avoid constraint errors).
    # ------------------------------------------------------------------
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))

    with op.batch_alter_table("bookmarks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bookmarks_normalized_url"))
        batch_op.drop_index(batch_op.f("ix_bookmarks_karakeep_id"))
        batch_op.drop_index(batch_op.f("ix_bookmarks_aizk_uuid"))
    op.drop_table("bookmarks")

    # ------------------------------------------------------------------
    # 6. Rebuild conversion_jobs with source_ref column + clean FK → sources.
    #    Strategy: rename → create new → copy → drop old.
    # ------------------------------------------------------------------
    conn.execute(sa.text("ALTER TABLE conversion_jobs RENAME TO _conversion_jobs_old"))

    op.create_table(
        "conversion_jobs",
        *_CONVERSION_JOBS_COLS,
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["aizk_uuid"], ["sources.aizk_uuid"]),
        sa.PrimaryKeyConstraint("id"),
    )
    conn.execute(
        sa.text(
            f"INSERT INTO conversion_jobs ({_JOBS_COPY_COLS}, source_ref) "
            f"SELECT {_JOBS_COPY_COLS}, NULL "
            "FROM _conversion_jobs_old"
        )
    )
    conn.execute(sa.text("DROP TABLE _conversion_jobs_old"))

    # Recreate indexes on conversion_jobs
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_conversion_jobs_aizk_uuid"), ["aizk_uuid"], unique=False)
        batch_op.create_index(batch_op.f("ix_conversion_jobs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_conversion_jobs_earliest_next_attempt_at"),
            ["earliest_next_attempt_at"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_conversion_jobs_idempotency_key"), ["idempotency_key"], unique=True)
        batch_op.create_index(batch_op.f("ix_conversion_jobs_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_conversion_jobs_status_next_attempt_queued",
            ["status", "earliest_next_attempt_at", "queued_at"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # 7. Rebuild conversion_outputs with clean FK → sources.
    # ------------------------------------------------------------------
    conn.execute(sa.text("ALTER TABLE conversion_outputs RENAME TO _conversion_outputs_old"))

    op.create_table(
        "conversion_outputs",
        *_CONVERSION_OUTPUTS_COLS,
        sa.ForeignKeyConstraint(["aizk_uuid"], ["sources.aizk_uuid"]),
        sa.ForeignKeyConstraint(["job_id"], ["conversion_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    conn.execute(
        sa.text(
            f"INSERT INTO conversion_outputs ({_OUTPUTS_COPY_COLS}) "
            f"SELECT {_OUTPUTS_COPY_COLS} "
            "FROM _conversion_outputs_old"
        )
    )
    conn.execute(sa.text("DROP TABLE _conversion_outputs_old"))

    # Recreate indexes on conversion_outputs
    with op.batch_alter_table("conversion_outputs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_conversion_outputs_aizk_uuid"), ["aizk_uuid"], unique=False)
        batch_op.create_index(batch_op.f("ix_conversion_outputs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_conversion_outputs_job_id"), ["job_id"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_conversion_outputs_markdown_hash_xx64"),
            ["markdown_hash_xx64"],
            unique=False,
        )

    conn.execute(sa.text("PRAGMA foreign_keys=ON"))

    # ------------------------------------------------------------------
    # 8. Populate source_ref on conversion_jobs from sources.
    # ------------------------------------------------------------------
    conn.execute(
        sa.text(
            "UPDATE conversion_jobs "
            "SET source_ref = ("
            "  SELECT source_ref FROM sources "
            "  WHERE sources.aizk_uuid = conversion_jobs.aizk_uuid"
            ")"
        )
    )

    # ------------------------------------------------------------------
    # 9. Recompute idempotency_key: sha256(source_ref_hash:docling:config_json:job_<id>).
    #
    # Frozen at the defaults shipped with this migration. Using live config
    # would make the hash differ between migration-time and a fresh job
    # submitted post-migration with non-default settings.
    #
    # The job ID is appended as a disambiguator so that two historical jobs
    # referencing the same source always produce distinct keys, avoiding a
    # unique-constraint violation when the UPDATE loop reaches the second row.
    # Replay-idempotency is guaranteed for post-migration submissions only;
    # historical rows receive stable, unique keys but are not guaranteed to
    # deduplicate on re-submission.
    # ------------------------------------------------------------------
    _snapshot = {
        "pdf_max_pages": 250,
        "ocr_enabled": True,
        "table_structure_enabled": True,
        "picture_description_model": "openai/gpt-5.4-nano",
        "picture_timeout": 180.0,
        "picture_classification_enabled": True,
        "picture_description_enabled": False,
    }
    _config_json = json.dumps(_snapshot, sort_keys=True, separators=(",", ":"))

    job_rows = conn.execute(
        sa.text(
            "SELECT cj.id, s.source_ref_hash "
            "FROM conversion_jobs cj "
            "JOIN sources s ON s.aizk_uuid = cj.aizk_uuid "
            "WHERE s.source_ref_hash IS NOT NULL"
        )
    ).fetchall()

    for job_id, source_ref_hash in job_rows:
        new_key = _sha256_hex(f"{source_ref_hash}:docling:{_config_json}:job_{job_id}")
        conn.execute(
            sa.text("UPDATE conversion_jobs SET idempotency_key = :key WHERE id = :id"),
            {"key": new_key, "id": job_id},
        )


def downgrade() -> None:
    """Reverse: recreate bookmarks from sources, restore FKs, drop sources.

    Aborts if any row in sources has karakeep_id IS NULL (non-reversible).
    """
    conn = op.get_bind()

    # Guard: cannot downgrade if any source row lacks a karakeep_id
    null_count = conn.execute(sa.text("SELECT COUNT(*) FROM sources WHERE karakeep_id IS NULL")).scalar()
    if null_count:
        raise IrreversibleMigrationError(
            f"Cannot downgrade: {null_count} row(s) in sources have karakeep_id IS NULL. Downgrade would lose data."
        )

    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))

    # ------------------------------------------------------------------
    # 1. Recreate bookmarks table (original schema, karakeep_id NOT NULL).
    # ------------------------------------------------------------------
    op.create_table(
        "bookmarks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("karakeep_id", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("aizk_uuid", sa.Uuid(), nullable=False),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("normalized_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column("content_type", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=True),
        sa.Column("source_type", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("bookmarks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_bookmarks_aizk_uuid"), ["aizk_uuid"], unique=True)
        batch_op.create_index(batch_op.f("ix_bookmarks_karakeep_id"), ["karakeep_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_bookmarks_normalized_url"), ["normalized_url"], unique=False)

    # ------------------------------------------------------------------
    # 2. Copy rows back from sources → bookmarks.
    # ------------------------------------------------------------------
    conn.execute(
        sa.text(
            "INSERT INTO bookmarks "
            "(id, karakeep_id, aizk_uuid, url, normalized_url, "
            "title, content_type, source_type, created_at, updated_at) "
            "SELECT id, karakeep_id, aizk_uuid, url, normalized_url, "
            "title, content_type, source_type, created_at, updated_at "
            "FROM sources"
        )
    )

    # ------------------------------------------------------------------
    # 3. Rebuild conversion_jobs: remove source_ref, retarget FK → bookmarks.
    # ------------------------------------------------------------------
    conn.execute(sa.text("ALTER TABLE conversion_jobs RENAME TO _conversion_jobs_old"))

    op.create_table(
        "conversion_jobs",
        *_CONVERSION_JOBS_COLS,
        sa.ForeignKeyConstraint(["aizk_uuid"], ["bookmarks.aizk_uuid"]),
        sa.PrimaryKeyConstraint("id"),
    )
    conn.execute(
        sa.text(f"INSERT INTO conversion_jobs ({_JOBS_COPY_COLS}) SELECT {_JOBS_COPY_COLS} FROM _conversion_jobs_old")
    )
    conn.execute(sa.text("DROP TABLE _conversion_jobs_old"))

    # Recreate indexes
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_conversion_jobs_aizk_uuid"), ["aizk_uuid"], unique=False)
        batch_op.create_index(batch_op.f("ix_conversion_jobs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_conversion_jobs_earliest_next_attempt_at"),
            ["earliest_next_attempt_at"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_conversion_jobs_idempotency_key"), ["idempotency_key"], unique=True)
        batch_op.create_index(batch_op.f("ix_conversion_jobs_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_conversion_jobs_status_next_attempt_queued",
            ["status", "earliest_next_attempt_at", "queued_at"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # 4. Rebuild conversion_outputs with FK → bookmarks.
    # ------------------------------------------------------------------
    conn.execute(sa.text("ALTER TABLE conversion_outputs RENAME TO _conversion_outputs_old"))

    op.create_table(
        "conversion_outputs",
        *_CONVERSION_OUTPUTS_COLS,
        sa.ForeignKeyConstraint(["aizk_uuid"], ["bookmarks.aizk_uuid"]),
        sa.ForeignKeyConstraint(["job_id"], ["conversion_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    conn.execute(
        sa.text(
            f"INSERT INTO conversion_outputs ({_OUTPUTS_COPY_COLS}) "
            f"SELECT {_OUTPUTS_COPY_COLS} "
            "FROM _conversion_outputs_old"
        )
    )
    conn.execute(sa.text("DROP TABLE _conversion_outputs_old"))

    with op.batch_alter_table("conversion_outputs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_conversion_outputs_aizk_uuid"), ["aizk_uuid"], unique=False)
        batch_op.create_index(batch_op.f("ix_conversion_outputs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_conversion_outputs_job_id"), ["job_id"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_conversion_outputs_markdown_hash_xx64"),
            ["markdown_hash_xx64"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # 5. Drop sources table.
    # ------------------------------------------------------------------
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sources_source_ref_hash"))
        batch_op.drop_index(batch_op.f("ix_sources_normalized_url"))
        batch_op.drop_index(batch_op.f("ix_sources_karakeep_id"))
        batch_op.drop_index(batch_op.f("ix_sources_aizk_uuid"))
    op.drop_table("sources")

    conn.execute(sa.text("PRAGMA foreign_keys=ON"))
