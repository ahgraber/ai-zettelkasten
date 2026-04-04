"""Unit tests for manifest generation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

from aizk.conversion.storage.manifest import (
    ConversionManifest,
    ManifestConfigSnapshot,
    generate_manifest,
)

_DEFAULT_AIZK_UUID = UUID("12345678-1234-5678-1234-567812345678")


def _make_bookmark(
    aizk_uuid: UUID = _DEFAULT_AIZK_UUID,
    karakeep_id: str = "kk-001",
    url: str = "https://example.com/page",
    normalized_url: str = "https://example.com/page",
    title: str = "Example",
    source_type: str = "other",
) -> MagicMock:
    bm = MagicMock()
    bm.aizk_uuid = aizk_uuid
    bm.karakeep_id = karakeep_id
    bm.url = url
    bm.normalized_url = normalized_url
    bm.title = title
    bm.source_type = source_type
    return bm


def _make_job(
    job_id: int = 1,
    payload_version: int = 1,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.payload_version = payload_version
    job.started_at = started_at
    job.finished_at = finished_at
    return job


_FETCHED_AT = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _config_snapshot(
    *,
    picture_description_enabled: bool = False,
    docling_enable_picture_classification: bool = True,
) -> ManifestConfigSnapshot:
    return ManifestConfigSnapshot(
        docling_pdf_max_pages=250,
        docling_enable_ocr=True,
        docling_enable_table_structure=True,
        docling_vlm_model="openai/gpt-5-nano",
        docling_picture_timeout=180.0,
        docling_enable_picture_classification=docling_enable_picture_classification,
        picture_description_enabled=picture_description_enabled,
    )


def _base_manifest(*, picture_description_enabled: bool = False) -> ConversionManifest:
    return generate_manifest(
        bookmark=_make_bookmark(),
        job=_make_job(),
        fetched_at=_FETCHED_AT,
        markdown_s3_uri="s3://bucket/output.md",
        markdown_hash="abcd1234",
        figure_s3_uris=[],
        docling_version="2.0.0",
        pipeline_name="html",
        config_snapshot=_config_snapshot(picture_description_enabled=picture_description_enabled),
    )


def test_manifest_config_snapshot_present():
    manifest = _base_manifest()
    assert hasattr(manifest, "config_snapshot")
    assert manifest.config_snapshot is not None


def test_manifest_config_snapshot_contains_picture_description_enabled_false():
    manifest = _base_manifest(picture_description_enabled=False)
    assert manifest.config_snapshot.picture_description_enabled is False


def test_manifest_config_snapshot_contains_picture_description_enabled_true():
    manifest = _base_manifest(picture_description_enabled=True)
    assert manifest.config_snapshot.picture_description_enabled is True


def test_manifest_config_snapshot_contains_docling_fields():
    manifest = _base_manifest(picture_description_enabled=True)
    assert manifest.config_snapshot.docling_pdf_max_pages == 250
    assert manifest.config_snapshot.docling_enable_ocr is True
    assert manifest.config_snapshot.docling_enable_table_structure is True
    assert manifest.config_snapshot.docling_vlm_model == "openai/gpt-5-nano"
    assert manifest.config_snapshot.docling_picture_timeout == 180.0


def test_manifest_config_snapshot_contains_docling_enable_picture_classification():
    manifest = _base_manifest()
    assert manifest.config_snapshot.docling_enable_picture_classification is True
    manifest_off = generate_manifest(
        bookmark=_make_bookmark(),
        job=_make_job(),
        fetched_at=_FETCHED_AT,
        markdown_s3_uri="s3://bucket/output.md",
        markdown_hash="abcd1234",
        figure_s3_uris=[],
        docling_version="2.0.0",
        pipeline_name="html",
        config_snapshot=_config_snapshot(docling_enable_picture_classification=False),
    )
    assert manifest_off.config_snapshot.docling_enable_picture_classification is False


def test_manifest_config_snapshot_serialises_to_json():
    manifest = _base_manifest(picture_description_enabled=True)
    data = manifest.model_dump()
    assert "config_snapshot" in data
    snapshot = data["config_snapshot"]
    assert set(snapshot) == {
        "docling_pdf_max_pages",
        "docling_enable_ocr",
        "docling_enable_table_structure",
        "docling_vlm_model",
        "docling_picture_timeout",
        "docling_enable_picture_classification",
        "picture_description_enabled",
    }
    assert snapshot["picture_description_enabled"] is True
    assert snapshot["docling_enable_picture_classification"] is True
