"""Shared schemas for conversion job data transfer."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AnyUrl, BaseModel, Field

from aizk.datamodel.job import ConversionJobStatus


class JobSubmission(BaseModel):
    """Request schema for job submission."""

    karakeep_id: str = Field(..., max_length=255)
    payload_version: int = Field(default=1, ge=1)
    idempotency_key: str | None = Field(default=None, max_length=64)


class ArtifactSummary(BaseModel):
    """Summary of conversion artifacts for completed jobs."""

    s3_prefix: str
    markdown_key: str
    manifest_key: str
    figure_count: int


class JobResponse(BaseModel):
    """Response schema for conversion jobs."""

    id: int
    aizk_uuid: UUID
    karakeep_id: str
    url: AnyUrl
    title: str
    source_type: str
    status: ConversionJobStatus
    attempts: int
    payload_version: int
    idempotency_key: str
    error_code: str | None = None
    error_message: str | None = None
    earliest_next_attempt_at: datetime | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    artifact_summary: ArtifactSummary | None = None


class JobList(BaseModel):
    """Response schema for job listing."""

    jobs: list[JobResponse]
    total: int
    limit: int
    offset: int
