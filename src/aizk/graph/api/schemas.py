"""Define request and response schemas for graph operator routes.

Schemas carry stage-specific intake references and reuse the shared capacity
refusal response.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aizk.pipeline.lifecycle import WorkUnitStatus

#: Upper bound for a persisted integer key. A value beyond it cannot name a row,
#: and reaches SQLite as an ``OverflowError`` — a 500 — rather than a rejection.
_MAX_ROWID = 2**63 - 1


class ContextualizationSubmission(BaseModel):
    """A request to contextualize one converted document."""

    conversion_output_id: int = Field(ge=1, le=_MAX_ROWID)


class ExtractionSubmission(BaseModel):
    """A request to extract one source's entity mentions."""

    source_id: UUID


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
