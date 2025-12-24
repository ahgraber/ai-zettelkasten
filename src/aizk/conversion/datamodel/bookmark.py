"""SQLModel entity for conversion bookmarks."""

import datetime
from typing import TYPE_CHECKING, List
from uuid import UUID, uuid4

from sqlmodel import Field, Relationship, SQLModel


class Bookmark(SQLModel, table=True):
    """Bookmark metadata needed for conversion routing and deduplication."""

    __tablename__ = "bookmarks"

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    karakeep_id: str = Field(max_length=255, nullable=False, unique=True, index=True)
    aizk_uuid: UUID = Field(
        default_factory=uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
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

    jobs: List["ConversionJob"] = Relationship(back_populates="bookmark")
    outputs: List["ConversionOutput"] = Relationship(back_populates="bookmark")


if TYPE_CHECKING:
    from aizk.conversion.datamodel.job import ConversionJob
    from aizk.conversion.datamodel.output import ConversionOutput
