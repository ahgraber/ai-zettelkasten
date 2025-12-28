"""SQLModel entity for conversion bookmarks."""

import datetime
from typing import TYPE_CHECKING, List

from sqlmodel import Field, Relationship, SQLModel


class Bookmark(SQLModel, table=True):
    """Bookmark metadata needed for conversion routing and deduplication."""

    __tablename__ = "bookmarks"

    id: int | None = Field(default=None, primary_key=True)
    karakeep_id: str = Field(max_length=255, nullable=False, unique=True, index=True)
    aizk_uuid: str = Field(max_length=36, nullable=False, unique=True, index=True)
    url: str = Field(nullable=False)
    normalized_url: str = Field(nullable=False, index=True)
    title: str = Field(max_length=500, nullable=False)
    content_type: str = Field(max_length=10, nullable=False)
    source_type: str = Field(max_length=20, nullable=False)
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
    from aizk.datamodel.job import ConversionJob
    from aizk.datamodel.output import ConversionOutput
