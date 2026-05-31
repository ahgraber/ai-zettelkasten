"""Source-record helpers for the conversion worker: source_ref loading and enrichment."""

from __future__ import annotations

import json
import logging

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.datamodel.events import record_source_event
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.source import Source as SourceRecord
from aizk.conversion.processing.errors import JobDataIntegrityError
from aizk.conversion.processing.types import SubprocessMetadata, _utcnow

logger = logging.getLogger(__name__)


def _get_source_ref(job_id: int, engine: Engine):
    """Read and deserialize source_ref from the job record."""
    from pydantic import TypeAdapter

    from aizk.conversion.core.source_ref import SourceRef

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during preflight")
        if not job.source_ref:
            raise JobDataIntegrityError(f"Job {job_id} has no source_ref")
        return TypeAdapter(SourceRef).validate_python(json.loads(job.source_ref))


def _write_source_enrichment(
    subprocess_meta: SubprocessMetadata,
    aizk_uuid: str,
    engine,
    *,
    job_id: int,
    attempt: int,
) -> None:
    """Best-effort update of Source row from SubprocessMetadata. Logs on failure, never raises.

    Writes url, normalized_url, title, source_type, and content_type from the
    subprocess IPC payload. Failure here does not affect manifest, ConversionOutput,
    or job completion — Source is an advisory cache only.

    A ``source_enriched`` event is appended to the event log regardless of
    whether the Source UPDATE succeeded or failed, scoped to the originating
    ``(job_id, attempt)``. The audit captures what the worker attempted and
    the outcome; the Source mutation itself remains advisory.
    """
    from uuid import UUID as _UUID

    from pydantic import TypeAdapter

    from aizk.conversion.core.source_ref import SourceRef as _SourceRef
    from aizk.conversion.core.types import SOURCE_TYPE_BY_KIND

    uuid_obj = aizk_uuid if isinstance(aizk_uuid, _UUID) else _UUID(aizk_uuid)

    columns_attempted: list[str] = ["url", "normalized_url", "title", "source_type"]
    if subprocess_meta.content_type:
        columns_attempted.append("content_type")

    update_succeeded = False
    failure_reason: str | None = None

    try:
        terminal_ref: _SourceRef = TypeAdapter(_SourceRef).validate_python(subprocess_meta.terminal_ref)
        source_type = SOURCE_TYPE_BY_KIND.get(terminal_ref.kind, "other")
        source_meta = subprocess_meta.source_meta.to_source_metadata()

        with Session(engine) as session:
            source = session.exec(select(SourceRecord).where(SourceRecord.aizk_uuid == uuid_obj)).one_or_none()
            if source is None:
                logger.warning(
                    "Source row not found for aizk_uuid=%s during enrichment",
                    aizk_uuid,
                )
                failure_reason = "source_row_not_found"
            else:
                # Only write mutable metadata columns — never write identity columns.
                source.url = source_meta.source_url
                source.normalized_url = source_meta.normalized_url
                source.title = subprocess_meta.source_title
                source.source_type = source_type
                if subprocess_meta.content_type:
                    source.content_type = subprocess_meta.content_type
                source.updated_at = _utcnow()
                session.add(source)
                session.commit()
                update_succeeded = True
    except Exception as exc:
        logger.exception(
            "Source enrichment failed for aizk_uuid=%s (best-effort; job proceeds)",
            aizk_uuid,
        )
        failure_reason = str(exc)

    # Audit the attempt regardless of outcome. The event is best-effort: a
    # failure here is logged but does not propagate (Source enrichment is
    # advisory).
    try:
        with Session(engine) as event_session:
            record_source_event(
                event_session,
                job_id=job_id,
                aizk_uuid=uuid_obj,
                attempt=attempt,
                columns_written=columns_attempted,
                update_succeeded=update_succeeded,
                failure_reason=failure_reason,
            )
            event_session.commit()
    except Exception:
        logger.exception(
            "Failed to record source_enriched event for aizk_uuid=%s (best-effort)",
            aizk_uuid,
        )
