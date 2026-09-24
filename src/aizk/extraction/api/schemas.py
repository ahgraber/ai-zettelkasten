"""Request and response schemas for the extraction operator routes."""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aizk.pipeline.lifecycle import WorkUnitStatus


class ExtractionSubmission(BaseModel):
    """A request to extract one source's entity mentions."""

    source_id: UUID


class ExtractionJobResponse(BaseModel):
    """Serialized view of one extraction work-unit."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    source_id: UUID
    status: WorkUnitStatus
    attempts: int
    error_code: str | None = None
    error_message: str | None = None
    earliest_next_attempt_at: datetime.datetime | None = None
    queued_at: datetime.datetime | None = None
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ExtractionJobList(BaseModel):
    """A page of extraction work-units with its pagination envelope."""

    jobs: list[ExtractionJobResponse]
    total: int
    limit: int
    offset: int
