"""SQLModel entity for conversion jobs."""

from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, Relationship

from aizk.pipeline.lifecycle import WorkUnitColumns


class ConversionJob(WorkUnitColumns, table=True):
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
        Index("ix_conversion_jobs_status", "status"),
        Index("ix_conversion_jobs_earliest_next_attempt_at", "earliest_next_attempt_at"),
        Index("ix_conversion_jobs_created_at", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    source_id: UUID = Field(foreign_key="sources.source_id", nullable=False, index=True)
    owner_id: str = Field(nullable=False, index=True)
    title: str = Field(max_length=500, nullable=False)
    payload_version: int = Field(default=1, nullable=False)
    error_detail: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    idempotency_key: str = Field(max_length=64, nullable=False, index=True)
    source_ref: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    phase: str | None = Field(default=None, max_length=7, nullable=True)

    source: "Source" = Relationship(back_populates="jobs")
    output: Optional["ConversionOutput"] = Relationship(back_populates="job")


if TYPE_CHECKING:
    from aizk.conversion.datamodel.output import ConversionOutput
    from aizk.conversion.datamodel.source import Source
