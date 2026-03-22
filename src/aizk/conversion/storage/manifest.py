"""Manifest generation for conversion artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from aizk.conversion.datamodel.bookmark import Bookmark
    from aizk.conversion.datamodel.job import ConversionJob

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


class ManifestConfigSnapshot(BaseModel):
    """Conversion config fields that affect output, captured for exact replay."""

    docling_pdf_max_pages: int = Field(description="Maximum PDF pages processed by Docling")
    docling_enable_ocr: bool = Field(description="Whether OCR is enabled during conversion")
    docling_enable_table_structure: bool = Field(description="Whether table structure extraction is enabled")
    docling_vlm_model: str = Field(description="Configured VLM model for picture descriptions")
    docling_picture_timeout: float = Field(description="Timeout for picture description generation")
    picture_description_enabled: bool = Field(description="Whether figure alt-text was generated via chat completions")


class ConversionManifest(BaseModel):
    """Complete conversion manifest with all metadata."""

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})

    version: str = "1.0"
    aizk_uuid: UUID
    karakeep_id: str
    source: ManifestSource
    conversion: ManifestConversionMetadata
    artifacts: ManifestArtifacts
    config_snapshot: ManifestConfigSnapshot


def _coerce_datetime(value: datetime | None, fallback: datetime) -> datetime:
    """Return datetime value or fallback if None."""
    chosen = value if value is not None else fallback
    if chosen.tzinfo is None:
        return chosen.replace(tzinfo=timezone.utc)
    return chosen


def generate_manifest(
    bookmark: Bookmark,
    job: ConversionJob,
    fetched_at: datetime,
    markdown_s3_uri: str,
    markdown_hash: str,
    figure_s3_uris: list[str],
    docling_version: str,
    pipeline_name: Literal["html", "pdf"],
    *,
    config_snapshot: ManifestConfigSnapshot,
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
        config_snapshot: Replayable conversion config snapshot used for this output.

    Returns:
        ConversionManifest Pydantic model.
    """
    if job.id is None:
        raise ValueError("ConversionJob.id must be set before manifest generation")

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
            job_id=job.id,
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
        config_snapshot=config_snapshot,
    )


def build_manifest_config_snapshot(config_values: dict[str, Any]) -> ManifestConfigSnapshot:
    """Build a typed manifest config snapshot from Docling config values."""
    return ManifestConfigSnapshot(**config_values)


def save_manifest(manifest: ConversionManifest, output_path: Path) -> None:
    """Save manifest to JSON file.

    Args:
        manifest: ConversionManifest Pydantic model.
        output_path: Path to save manifest.json.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(manifest.model_dump_json(indent=2, exclude_none=True))
    logger.info("Saved manifest to %s", output_path)
