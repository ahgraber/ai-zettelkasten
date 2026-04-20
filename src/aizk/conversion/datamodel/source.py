"""SQLModel entity for conversion sources."""

import datetime
from typing import TYPE_CHECKING, List, Self
from uuid import UUID, uuid4

from pydantic import model_validator
from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class Source(SQLModel, table=True):
    """Source identity row — canonical durable identity for anything the system can convert."""

    __tablename__ = "sources"

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    karakeep_id: str | None = Field(default=None, max_length=255, nullable=True, unique=True, index=True)
    aizk_uuid: UUID = Field(
        default_factory=uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    source_ref: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    source_ref_hash: str | None = Field(default=None, sa_column=Column(Text, nullable=True, unique=True, index=True))
    url: str | None = Field(default=None, nullable=True)
    normalized_url: str | None = Field(default=None, nullable=True, index=True)
    title: str | None = Field(default=None, max_length=500, nullable=True)
    content_type: str | None = Field(default=None, max_length=10, nullable=True)
    source_type: str | None = Field(default=None, max_length=20, nullable=True)
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )
    updated_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        nullable=False,
    )

    jobs: List["ConversionJob"] = Relationship(back_populates="source")
    outputs: List["ConversionOutput"] = Relationship(back_populates="source")

    @model_validator(mode="after")
    def _require_source_ref_when_persisted(self) -> Self:
        if self.id is not None and (self.source_ref is None or self.source_ref_hash is None):
            raise ValueError("Persisted Source row must have source_ref and source_ref_hash set")
        return self


if TYPE_CHECKING:
    from aizk.conversion.datamodel.job import ConversionJob
    from aizk.conversion.datamodel.output import ConversionOutput
