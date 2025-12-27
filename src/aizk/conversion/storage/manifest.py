"""Manifest generation for conversion artifacts."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from aizk.datamodel.bookmark import Bookmark
    from aizk.datamodel.job import ConversionJob

logger = logging.getLogger(__name__)


class ManifestSource(BaseModel):
    """Source information in manifest."""

    url: str
    normalized_url: str
    title: str
    source_type: Literal["arxiv", "github", "other"]
    fetched_at: datetime


class ManifestConversionMetadata(BaseModel):
    """Conversion metadata in manifest."""

    job_id: int
    payload_version: int
    docling_version: str
    pipeline_name: Literal["html", "pdf"]
    started_at: datetime
    finished_at: datetime
    duration_seconds: int = Field(description="Duration from started_at to finished_at")


class ManifestArtifactMarkdown(BaseModel):
    """Markdown artifact metadata."""

    key: str = Field(description="Absolute S3 URI (s3://bucket/key)")
    hash_xx64: str = Field(description="xxHash64 hex digest")
    created_at: datetime


class ManifestArtifactFigure(BaseModel):
    """Figure artifact metadata."""

    key: str = Field(description="Absolute S3 URI (s3://bucket/key)")
    created_at: datetime


class ManifestArtifacts(BaseModel):
    """Artifacts section of manifest."""

    markdown: ManifestArtifactMarkdown
    figures: list[ManifestArtifactFigure]


class ConversionManifest(BaseModel):
    """Complete conversion manifest with all metadata."""

    version: str = "1.0"
    aizk_uuid: str
    karakeep_id: str
    source: ManifestSource
    conversion: ManifestConversionMetadata
    artifacts: ManifestArtifacts

    class Config:
        """Pydantic config."""

        json_encoders = {datetime: lambda v: v.isoformat()}


def _coerce_datetime(value: datetime | None, fallback: datetime) -> datetime:
    """Return datetime value or fallback if None."""
    return value if value is not None else fallback


def generate_manifest(
    bookmark: Bookmark,
    job: ConversionJob,
    fetched_at: datetime,
    markdown_s3_uri: str,
    markdown_hash: str,
    figure_s3_uris: list[str],
    docling_version: str,
    pipeline_name: Literal["html", "pdf"],
) -> ConversionManifest:
    """Generate manifest for conversion artifacts.

    Args:
        bookmark: Bookmark record with source metadata.
        job: ConversionJob record with timing and job info.
        fetched_at: Timestamp when content was fetched.
        markdown_s3_uri: Absolute S3 URI for markdown (s3://bucket/key).
        markdown_hash: Markdown xxHash64 hex digest.
        figure_s3_uris: List of absolute S3 URIs for figures.
        docling_version: Docling version used.
        pipeline_name: Pipeline name (html/pdf).

    Returns:
        ConversionManifest Pydantic model.
    """
    started_at = _coerce_datetime(job.started_at, fetched_at)
    finished_at = _coerce_datetime(job.finished_at, fetched_at)
    duration_seconds = max(0, int((finished_at - started_at).total_seconds()))

    source_type = bookmark.source_type
    if source_type not in {"arxiv", "github", "other"}:
        source_type = "other"

    figures = [ManifestArtifactFigure(key=uri, created_at=finished_at) for uri in figure_s3_uris]

    return ConversionManifest(
        aizk_uuid=bookmark.aizk_uuid,
        karakeep_id=bookmark.karakeep_id,
        source=ManifestSource(
            url=bookmark.url,
            normalized_url=bookmark.normalized_url,
            title=bookmark.title,
            source_type=source_type,  # type: ignore[arg-type]
            fetched_at=fetched_at,
        ),
        conversion=ManifestConversionMetadata(
            job_id=job.id or 0,
            payload_version=job.payload_version,
            docling_version=docling_version,
            pipeline_name=pipeline_name,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
        ),
        artifacts=ManifestArtifacts(
            markdown=ManifestArtifactMarkdown(
                key=markdown_s3_uri,
                hash_xx64=markdown_hash,
                created_at=finished_at,
            ),
            figures=figures,
        ),
    )


def save_manifest(manifest: ConversionManifest, output_path: Path) -> None:
    """Save manifest to JSON file.

    Args:
        manifest: ConversionManifest Pydantic model.
        output_path: Path to save manifest.json.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(manifest.model_dump_json(indent=2, exclude_none=True))
    logger.info("Saved manifest to %s", output_path)
