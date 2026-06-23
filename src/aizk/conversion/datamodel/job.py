"""SQLModel entity for conversion jobs."""

import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, Relationship, SQLModel


class ConversionJobStatus(str, Enum):
    """Allowed statuses for conversion jobs."""

    NEW = "NEW"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    UPLOAD_PENDING = "UPLOAD_PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERM = "FAILED_PERM"
    CANCELLED = "CANCELLED"


class ConversionJob(SQLModel, table=True):
    """Represents a single conversion attempt with retry tracking."""

    __tablename__ = "conversion_jobs"
    __table_args__ = (
        Index(
            "ix_conversion_jobs_status_next_attempt_queued",
            "status",
            "earliest_next_attempt_at",
            "queued_at",
        ),
        # Owner-scoped uniqueness: two principals may legitimately hold the
        # same idempotency_key (same source/config) while a single principal
        # still cannot duplicate within their own scope.
        Index(
            "uq_conversion_jobs_owner_idempotency_key",
            "owner_id",
            "idempotency_key",
            unique=True,
        ),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    source_id: UUID = Field(foreign_key="sources.source_id", nullable=False, index=True)
    owner_id: str = Field(nullable=False, index=True)
    title: str = Field(max_length=500, nullable=False)
    payload_version: int = Field(default=1, nullable=False)
    status: ConversionJobStatus = Field(default=ConversionJobStatus.NEW, nullable=False, index=True)
    attempts: int = Field(default=0, nullable=False)
    error_code: Optional[str] = Field(default=None, max_length=50)
    error_message: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    error_detail: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    idempotency_key: str = Field(max_length=64, nullable=False, index=True)
    source_ref: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    earliest_next_attempt_at: Optional[datetime.datetime] = Field(default=None, index=True)
    last_error_at: Optional[datetime.datetime] = Field(default=None)
    queued_at: Optional[datetime.datetime] = Field(default=None)
    started_at: Optional[datetime.datetime] = Field(default=None)
    finished_at: Optional[datetime.datetime] = Field(default=None)
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
        index=True,
    )
    updated_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )

    source: "Source" = Relationship(back_populates="jobs")
    output: Optional["ConversionOutput"] = Relationship(back_populates="job")


if TYPE_CHECKING:
    from aizk.conversion.datamodel.output import ConversionOutput
    from aizk.conversion.datamodel.source import Source
