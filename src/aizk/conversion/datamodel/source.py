"""SQLModel entity for conversion sources (generalized from KaraKeep bookmarks)."""

import datetime
from typing import TYPE_CHECKING, Any, List
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field, Relationship, SQLModel


class Source(SQLModel, table=True):
    """Durable identity row for anything the conversion service can convert.

    Generalizes the legacy ``Bookmark`` table. ``karakeep_id`` is nullable
    because non-KaraKeep sources have no such id. ``source_ref`` is the
    canonical fetch instruction (a discriminated ``SourceRef`` pydantic model
    serialized to JSON); ``source_ref_hash`` is a SHA-256 of its canonical
    dedup payload and uniquely identifies the source for deduplication.
    """

    __tablename__ = "sources"

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    karakeep_id: str | None = Field(
        default=None,
        max_length=255,
        nullable=True,
        unique=True,
        index=True,
    )
    aizk_uuid: UUID = Field(
        default_factory=uuid4,
        nullable=False,
        unique=True,
        index=True,
    )
    source_ref: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    source_ref_hash: str = Field(
        max_length=64,
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

    jobs: List["ConversionJob"] = Relationship(back_populates="source")
    outputs: List["ConversionOutput"] = Relationship(back_populates="source")

    @classmethod
    def from_karakeep_id(cls, karakeep_id: str, **overrides: Any) -> "Source":
        """Construct a Source from a KaraKeep bookmark id, synthesizing source_ref.

        Test and admin convenience only — production flow goes through the API
        job submission path which builds ``source_ref`` from ``JobSubmission``.
        Overrides may set ``url``, ``title``, etc.
        """
        from aizk.conversion.core.source_ref import (
            KarakeepBookmarkRef,
            compute_source_ref_hash,
        )

        ref = KarakeepBookmarkRef(bookmark_id=karakeep_id)
        defaults = {
            "karakeep_id": karakeep_id,
            "source_ref": ref.model_dump(),
            "source_ref_hash": compute_source_ref_hash(ref),
        }
        defaults.update(overrides)
        return cls(**defaults)


if TYPE_CHECKING:
    from aizk.conversion.datamodel.job import ConversionJob
    from aizk.conversion.datamodel.output import ConversionOutput
