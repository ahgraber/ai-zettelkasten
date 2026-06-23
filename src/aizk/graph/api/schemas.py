"""Response schemas for the contextualization operator API."""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aizk.pipeline.lifecycle import WorkUnitStatus


class ContextualizationJobResponse(BaseModel):
    """Serialized view of one contextualization work-unit."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    conversion_output_id: int
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


class ContextualizationJobList(BaseModel):
    """A page of contextualization work-units with its pagination envelope."""

    jobs: list[ContextualizationJobResponse]
    total: int
    limit: int
    offset: int
