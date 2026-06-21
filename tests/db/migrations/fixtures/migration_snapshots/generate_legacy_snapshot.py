#!/usr/bin/env python3
"""Generate the sanitized legacy migration snapshot fixture.

Run from the handler root:

    uv run python tests/db/migrations/fixtures/migration_snapshots/generate_legacy_snapshot.py

The generator reads only aggregate counts from ``data/conversion_service.db``.
It does not copy real titles, URLs, KaraKeep IDs, S3 keys, UUIDs, payloads, or
error text. The output fixture is synthetic, deterministic, and safe to check in.
"""

from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from alembic import command
from alembic.config import Config
from setproctitle import setproctitle

from aizk.utilities.path_utils import get_repo_path

REPO_ROOT = get_repo_path(__file__)
MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent
FIXTURE_DIR = Path(__file__).resolve().parent
SOURCE_DB = REPO_ROOT / "data" / "conversion_service.db"
LEGACY_REVISION = "a8c9d0e1f2b3"
FIXTURE_DB = FIXTURE_DIR / f"legacy_{LEGACY_REVISION}.db"
MANIFEST_PATH = FIXTURE_DIR / f"legacy_{LEGACY_REVISION}.manifest.json"

OWNERS = ("self", "fixture-owner-a", "fixture-owner-b")
STATUS_PLAN = (
    ("NEW", 1),
    ("QUEUED", 2),
    ("RUNNING", 2),
    ("UPLOAD_PENDING", 2),
    ("SUCCEEDED", 8),
    ("FAILED_RETRYABLE", 4),
    ("FAILED_PERM", 3),
    ("CANCELLED", 3),
)
AUDIT_SOURCE_INDEX = 1


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _read_source_profile() -> dict[str, Any]:
    """Read aggregate shape from the local historical DB without copying row data."""
    if not SOURCE_DB.exists():
        raise FileNotFoundError(f"source DB not found: {SOURCE_DB}")

    profile: dict[str, Any] = {"source_path": str(SOURCE_DB.relative_to(REPO_ROOT))}
    with sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True) as conn:
        row_counts: dict[str, int] = {}
        for table in ("bookmarks", "conversion_jobs", "conversion_outputs"):
            row_counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
        profile["row_counts"] = row_counts
        profile["job_status_counts"] = {
            status: int(count)
            for status, count in conn.execute("SELECT status, COUNT(*) FROM conversion_jobs GROUP BY status")
        }
    return profile


def _utc(index: int) -> str:
    """Return a deterministic timestamp string."""
    return (
        (dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=index))
        .replace(tzinfo=None)
        .isoformat()
    )


def _source_uuid(index: int) -> str:
    """Return a deterministic UUID hex string for a fixture source."""
    return uuid5(NAMESPACE_URL, f"https://example.invalid/aizk-fixture/source/{index}").hex


def _source_ref(bookmark_id: str) -> str:
    payload = {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _source_ref_hash(source_ref: str) -> str:
    return hashlib.sha256(source_ref.encode("utf-8")).hexdigest()


def _job_statuses() -> list[str]:
    statuses: list[str] = []
    for status, count in STATUS_PLAN:
        statuses.extend([status] * count)
    return statuses


def _insert_sources(conn: sqlite3.Connection, total: int) -> None:
    for index in range(1, total + 1):
        bookmark_id = f"fixture_{index:03d}"
        source_ref = _source_ref(bookmark_id)
        content_type = "pdf" if index % 3 == 0 else "html"
        source_type = "asset" if content_type == "pdf" else "url"
        conn.execute(
            """
            INSERT INTO sources (
                id, karakeep_id, aizk_uuid, source_ref, source_ref_hash, url,
                normalized_url, title, content_type, source_type, created_at,
                updated_at, owner_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index,
                bookmark_id,
                _source_uuid(index),
                source_ref,
                _source_ref_hash(source_ref),
                f"https://example.invalid/fixture/{index:03d}",
                f"https://example.invalid/fixture/{index:03d}",
                f"Synthetic fixture source {index:03d}",
                content_type,
                source_type,
                _utc(index),
                _utc(index + 1),
                OWNERS[index % len(OWNERS)],
            ),
        )


def _job_timestamps(index: int, status: str) -> tuple[str | None, str | None, str | None]:
    queued_at = _utc(index + 10)
    started_at = _utc(index + 20) if status not in {"NEW", "QUEUED"} else None
    finished_at = _utc(index + 30) if status in {"SUCCEEDED", "FAILED_RETRYABLE", "FAILED_PERM", "CANCELLED"} else None
    return queued_at, started_at, finished_at


def _insert_jobs(conn: sqlite3.Connection, statuses: list[str]) -> None:
    for index, status in enumerate(statuses, start=1):
        bookmark_id = f"fixture_{index:03d}"
        source_ref = _source_ref(bookmark_id)
        owner_id = OWNERS[index % len(OWNERS)]
        if index in (1, 2):
            idempotency_key = "shared-cross-owner-key"
            owner_id = ("fixture-owner-a", "fixture-owner-b")[index - 1]
        else:
            idempotency_key = f"fixture-idempotency-{index:03d}"
        queued_at, started_at, finished_at = _job_timestamps(index, status)
        attempts = 0 if status in {"NEW", "QUEUED"} else 1 + (index % 3)
        error_code = None
        error_message = None
        error_detail = None
        earliest_next_attempt_at = None
        last_error_at = None
        if status in {"FAILED_RETRYABLE", "FAILED_PERM"}:
            error_code = "fixture_retryable" if status == "FAILED_RETRYABLE" else "fixture_permanent"
            error_message = f"Synthetic {status.lower()} failure"
            error_detail = json.dumps({"kind": "fixture_error", "status": status}, sort_keys=True)
            last_error_at = _utc(index + 40)
            if status == "FAILED_RETRYABLE":
                earliest_next_attempt_at = _utc(index + 50)

        conn.execute(
            """
            INSERT INTO conversion_jobs (
                id, aizk_uuid, title, payload_version, status, attempts,
                error_code, error_message, idempotency_key,
                earliest_next_attempt_at, last_error_at, queued_at, started_at,
                finished_at, created_at, updated_at, error_detail, source_ref,
                owner_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index,
                _source_uuid(index),
                f"Synthetic fixture job {index:03d}",
                1,
                status,
                attempts,
                error_code,
                error_message,
                idempotency_key,
                earliest_next_attempt_at,
                last_error_at,
                queued_at,
                started_at,
                finished_at,
                _utc(index),
                _utc(index + 2),
                error_detail,
                source_ref,
                owner_id,
            ),
        )


def _insert_outputs(conn: sqlite3.Connection, statuses: list[str]) -> int:
    output_id = 0
    for index, status in enumerate(statuses, start=1):
        if status != "SUCCEEDED":
            continue
        output_id += 1
        conn.execute(
            """
            INSERT INTO conversion_outputs (
                id, job_id, aizk_uuid, title, payload_version, s3_prefix,
                markdown_key, manifest_key, markdown_hash_xx64, figure_count,
                docling_version, pipeline_name, created_at, owner_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                output_id,
                index,
                _source_uuid(index),
                f"Synthetic fixture output {index:03d}",
                1,
                f"fixture/source-{index:03d}/",
                f"fixture/source-{index:03d}/output.md",
                f"fixture/source-{index:03d}/manifest.json",
                f"{index:016x}"[-16:],
                index % 4,
                "fixture-docling",
                "fixture-pipeline",
                _utc(index + 60),
                OWNERS[index % len(OWNERS)],
            ),
        )
    return output_id


def _event_payload(kind: str, index: int, status: str) -> str:
    return json.dumps({"kind": kind, "fixture_index": index, "status": status}, sort_keys=True, separators=(",", ":"))


def _insert_event(
    conn: sqlite3.Connection,
    *,
    job_id: int | None,
    aizk_uuid: str,
    attempt: int,
    occurred_index: int,
    kind: str,
    from_status: str | None,
    to_status: str | None,
    payload_status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO conversion_job_events (
            job_id, aizk_uuid, attempt, occurred_at, kind, from_status,
            to_status, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            aizk_uuid,
            attempt,
            _utc(occurred_index),
            kind,
            from_status,
            to_status,
            _event_payload(kind, occurred_index, payload_status),
        ),
    )


def _insert_events(conn: sqlite3.Connection, statuses: list[str]) -> Counter[str]:
    kind_counts: Counter[str] = Counter()
    event_index = 100
    for index, status in enumerate(statuses, start=1):
        aizk_uuid = _source_uuid(index)
        if status == "NEW":
            continue
        _insert_event(
            conn,
            job_id=index,
            aizk_uuid=aizk_uuid,
            attempt=0,
            occurred_index=event_index,
            kind="queued",
            from_status=None,
            to_status="QUEUED",
            payload_status=status,
        )
        kind_counts["queued"] += 1
        event_index += 1

        if status != "QUEUED":
            _insert_event(
                conn,
                job_id=index,
                aizk_uuid=aizk_uuid,
                attempt=1,
                occurred_index=event_index,
                kind="claimed",
                from_status="QUEUED",
                to_status="RUNNING",
                payload_status=status,
            )
            kind_counts["claimed"] += 1
            event_index += 1

        terminal_kind: str | None = None
        from_status = "RUNNING"
        if status == "SUCCEEDED":
            terminal_kind = "succeeded"
        elif status in {"FAILED_RETRYABLE", "FAILED_PERM"}:
            terminal_kind = "failed"
        elif status == "CANCELLED":
            terminal_kind = "cancelled"
        elif status == "UPLOAD_PENDING":
            terminal_kind = "upload_pending"
        elif status == "RUNNING" and index % 2 == 0:
            terminal_kind = "phase"

        if terminal_kind is not None:
            _insert_event(
                conn,
                job_id=index,
                aizk_uuid=aizk_uuid,
                attempt=max(1, index % 3),
                occurred_index=event_index,
                kind=terminal_kind,
                from_status=from_status,
                to_status=status if terminal_kind != "phase" else "RUNNING",
                payload_status=status,
            )
            kind_counts[terminal_kind] += 1
            event_index += 1

        if index in (5, 10):
            _insert_event(
                conn,
                job_id=index,
                aizk_uuid=aizk_uuid,
                attempt=1,
                occurred_index=event_index,
                kind="source_enriched",
                from_status="RUNNING",
                to_status="RUNNING",
                payload_status=status,
            )
            kind_counts["source_enriched"] += 1
            event_index += 1

    _insert_event(
        conn,
        job_id=15,
        aizk_uuid=_source_uuid(15),
        attempt=2,
        occurred_index=event_index,
        kind="recovered_stale",
        from_status="RUNNING",
        to_status="FAILED_RETRYABLE",
        payload_status="FAILED_RETRYABLE",
    )
    kind_counts["recovered_stale"] += 1
    event_index += 1

    for orphan_index in (1, 2):
        _insert_event(
            conn,
            job_id=None,
            aizk_uuid=_source_uuid(AUDIT_SOURCE_INDEX),
            attempt=orphan_index,
            occurred_index=event_index,
            kind="failed" if orphan_index == 1 else "cancelled",
            from_status="RUNNING",
            to_status="FAILED_RETRYABLE" if orphan_index == 1 else "CANCELLED",
            payload_status="orphan",
        )
        kind_counts["failed" if orphan_index == 1 else "cancelled"] += 1
        event_index += 1

    return kind_counts


def _create_legacy_schema() -> None:
    if FIXTURE_DB.exists():
        FIXTURE_DB.unlink()
    command.upgrade(_alembic_cfg(f"sqlite:///{FIXTURE_DB}"), LEGACY_REVISION)


def _seed_fixture() -> dict[str, Any]:
    statuses = _job_statuses()
    with sqlite3.connect(FIXTURE_DB) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_sources(conn, len(statuses))
        _insert_jobs(conn, statuses)
        output_count = _insert_outputs(conn, statuses)
        event_kind_counts = _insert_events(conn, statuses)
        conn.commit()

        audit_uuid = _source_uuid(AUDIT_SOURCE_INDEX)
        audit_source_event_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM conversion_job_events WHERE aizk_uuid = ?",
                (audit_uuid,),
            ).fetchone()[0]
        )
        orphan_event_count = int(
            conn.execute("SELECT COUNT(*) FROM conversion_job_events WHERE job_id IS NULL").fetchone()[0]
        )

    return {
        "row_counts": {
            "sources": len(statuses),
            "conversion_jobs": len(statuses),
            "conversion_outputs": output_count,
            "conversion_job_events": sum(event_kind_counts.values()),
        },
        "job_status_counts": dict(Counter(statuses)),
        "event_kind_counts": dict(sorted(event_kind_counts.items())),
        "orphan_event_count": orphan_event_count,
        "audit_source_uuid": audit_uuid,
        "audit_source_event_count": audit_source_event_count,
    }


def main() -> None:
    setproctitle("aizk-generate-migration-snapshot")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    source_profile = _read_source_profile()
    _create_legacy_schema()
    fixture_profile = _seed_fixture()

    manifest = {
        "fixture": FIXTURE_DB.name,
        "legacy_revision": LEGACY_REVISION,
        "generated_from": source_profile,
        "sanitization": {
            "copied_raw_row_values": False,
            "url_domain": "example.invalid",
            "karakeep_id_prefix": "fixture_",
            "uuid_strategy": "uuid5(NAMESPACE_URL, synthetic fixture URL)",
        },
        **fixture_profile,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {FIXTURE_DB.relative_to(REPO_ROOT)}")
    print(f"Wrote {MANIFEST_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
