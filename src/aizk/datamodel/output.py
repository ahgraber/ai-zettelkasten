"""SQLModel entity for conversion outputs."""

import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, Relationship, SQLModel


class ConversionOutput(SQLModel, table=True):
    """Represents successful conversion artifacts and metadata."""

    __tablename__ = "conversion_outputs"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="conversion_jobs.id", nullable=False, unique=True, index=True)
    aizk_uuid: str = Field(foreign_key="bookmarks.aizk_uuid", nullable=False, index=True)
    title: str = Field(max_length=500, nullable=False)
    payload_version: int = Field(nullable=False)
    s3_prefix: str = Field(sa_column=Column(Text, nullable=False))
    markdown_key: str = Field(sa_column=Column(Text, nullable=False))
    manifest_key: str = Field(sa_column=Column(Text, nullable=False))
    markdown_hash_xx64: str = Field(max_length=16, nullable=False, index=True)
    figure_count: int = Field(default=0, nullable=False)
    docling_version: str = Field(max_length=20, nullable=False)
    pipeline_name: str = Field(max_length=50, nullable=False)
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
        index=True,
    )

    bookmark: "Bookmark" = Relationship(back_populates="outputs")
    job: Optional["ConversionJob"] = Relationship(back_populates="output")


if TYPE_CHECKING:
    from aizk.datamodel.bookmark import Bookmark
    from aizk.datamodel.job import ConversionJob
