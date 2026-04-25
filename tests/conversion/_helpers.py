"""Shared test factories for conversion-service tests.

These helpers replace per-file `_create_bookmark` / `_create_job` copies. They persist
rows through the passed SQLModel session and return the refreshed ORM object.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from uuid import UUID

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source

if TYPE_CHECKING:
    from sqlmodel import Session


def make_source(
    session: "Session",
    karakeep_id: str,
    *,
    aizk_uuid: UUID | None = None,
    source_ref_bookmark_id: str | None = None,
    url: str | None = None,
    title: str | None = None,
    content_type: str | None = None,
    source_type: str | None = None,
) -> Source:
    """Create, persist, and return a Source row keyed by `karakeep_id`.

    `source_ref_bookmark_id` lets callers keep the `karakeep_id` column distinct from the
    ref's `bookmark_id` field (used by dot-containing ID edge cases in test_output_content).
    """
    ref = KarakeepBookmarkRef(bookmark_id=source_ref_bookmark_id or karakeep_id)
    kwargs = dict(
        karakeep_id=karakeep_id,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        url=url,
        normalized_url=url,
        title=title,
        content_type=content_type,
        source_type=source_type,
    )
    if aizk_uuid is not None:
        kwargs["aizk_uuid"] = aizk_uuid
    source = Source(**kwargs)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def make_job(
    session: "Session",
    *,
    aizk_uuid: UUID,
    idempotency_key: str,
    status: ConversionJobStatus = ConversionJobStatus.QUEUED,
    title: str = "Test",
    attempts: int = 0,
    created_at: dt.datetime | None = None,
) -> ConversionJob:
    """Create, persist, and return a ConversionJob row."""
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        title=title,
        payload_version=1,
        status=status,
        attempts=attempts,
        idempotency_key=idempotency_key,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    session.commit()
    session.refresh(job)
    return job
