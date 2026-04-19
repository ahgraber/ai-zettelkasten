"""Unit tests for manifest generation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

from pydantic import ValidationError
import pytest

from aizk.conversion.core.source_ref import (
    ArxivRef,
    KarakeepBookmarkRef,
    UrlRef,
)
from aizk.conversion.storage.manifest import (
    ConversionManifest,
    ManifestConfigSnapshot,
    ManifestConfigSnapshotV2,
    ManifestV1,
    ManifestV2,
    generate_manifest,
    generate_manifest_v2,
    load_manifest,
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
    aizk_uuid: UUID = _DEFAULT_AIZK_UUID,
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.payload_version = payload_version
    job.started_at = started_at
    job.finished_at = finished_at
    job.aizk_uuid = aizk_uuid
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
        docling_picture_description_model="openai/gpt-5-nano",
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
    assert manifest.config_snapshot.docling_picture_description_model == "openai/gpt-5-nano"
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
        "docling_picture_description_model",
        "docling_picture_timeout",
        "docling_enable_picture_classification",
        "picture_description_enabled",
    }
    assert snapshot["picture_description_enabled"] is True
    assert snapshot["docling_enable_picture_classification"] is True


# ---------------------------------------------------------------------------
# v2.0 manifest tests
# ---------------------------------------------------------------------------

_KK_REF = KarakeepBookmarkRef(bookmark_id="bk-123")
_ARXIV_REF = ArxivRef(arxiv_id="2301.00001")
_URL_REF = UrlRef(url="https://example.com/page")


def _base_manifest_v2(
    submitted_ref=_KK_REF,
    terminal_ref=_KK_REF,
    *,
    source_url: str | None = "https://example.com/page",
    source_normalized_url: str | None = "https://example.com/page",
    source_title: str | None = "Example",
    source_type: str | None = "other",
) -> ManifestV2:
    return generate_manifest_v2(
        submitted_ref=submitted_ref,
        terminal_ref=terminal_ref,
        job=_make_job(),
        fetched_at=_FETCHED_AT,
        markdown_s3_uri="s3://bucket/output.md",
        markdown_hash="abcd1234",
        figure_s3_uris=[],
        docling_version="2.0.0",
        pipeline_name="html",
        converter_name="docling-html",
        adapter_snapshot={"max_pages": 250, "enable_ocr": True},
        source_url=source_url,
        source_normalized_url=source_normalized_url,
        source_title=source_title,
        source_type=source_type,
    )


def test_manifest_v1_extra_forbid():
    """ManifestV1 must have extra='forbid'."""
    assert ManifestV1.model_config.get("extra") == "forbid"


def test_manifest_v2_extra_forbid():
    """ManifestV2 must have extra='forbid'."""
    assert ManifestV2.model_config.get("extra") == "forbid"


def test_manifest_config_snapshot_v2_extra_forbid():
    """ManifestConfigSnapshotV2 must have extra='forbid'."""
    assert ManifestConfigSnapshotV2.model_config.get("extra") == "forbid"


def test_load_manifest_returns_v1_for_version_1_0():
    """load_manifest on a v1.0 dict returns ManifestV1."""
    v1_manifest = ConversionManifest(
        aizk_uuid=_DEFAULT_AIZK_UUID,
        karakeep_id="kk-001",
        source={
            "url": "https://example.com/page",
            "normalized_url": "https://example.com/page",
            "title": "Example",
            "source_type": "other",
            "fetched_at": _FETCHED_AT,
        },
        conversion={
            "job_id": 1,
            "payload_version": 1,
            "docling_version": "2.0.0",
            "pipeline_name": "html",
            "started_at": _FETCHED_AT,
            "finished_at": _FETCHED_AT,
            "duration_seconds": 0,
        },
        artifacts={
            "markdown": {
                "key": "s3://bucket/output.md",
                "hash_xx64": "abcd1234",
                "created_at": _FETCHED_AT,
            },
            "figures": [],
        },
        config_snapshot=_config_snapshot(),
    )
    data = v1_manifest.model_dump()
    result = load_manifest(data)
    assert isinstance(result, ManifestV1)
    assert result.version == "1.0"
    assert result.aizk_uuid == _DEFAULT_AIZK_UUID


def test_load_manifest_returns_v2_for_version_2_0():
    """load_manifest on a v2.0 dict returns ManifestV2."""
    v2_manifest = _base_manifest_v2()
    data = v2_manifest.model_dump()
    result = load_manifest(data)
    assert isinstance(result, ManifestV2)
    assert result.version == "2.0"


def test_manifest_v2_writer_emits_converter_name():
    """generate_manifest_v2 sets config_snapshot.converter_name."""
    manifest = _base_manifest_v2()
    assert manifest.config_snapshot.converter_name == "docling-html"


def test_manifest_v2_direct_karakeep_job():
    """For direct KaraKeep submission, submitted_ref == terminal_ref with same bookmark_id."""
    ref = KarakeepBookmarkRef(bookmark_id="bk-123")
    manifest = _base_manifest_v2(submitted_ref=ref, terminal_ref=ref)
    assert manifest.submitted_ref.kind == "karakeep_bookmark"  # type: ignore[union-attr]
    assert manifest.terminal_ref.kind == "karakeep_bookmark"  # type: ignore[union-attr]
    assert manifest.submitted_ref.bookmark_id == "bk-123"  # type: ignore[union-attr]
    assert manifest.terminal_ref.bookmark_id == "bk-123"  # type: ignore[union-attr]


def test_manifest_v2_karakeep_to_arxiv_job():
    """For KaraKeep->arxiv resolution, submitted_ref.kind='karakeep_bookmark', terminal_ref.kind='arxiv'."""
    submitted = KarakeepBookmarkRef(bookmark_id="bk-456")
    terminal = ArxivRef(arxiv_id="2301.00001")
    manifest = _base_manifest_v2(submitted_ref=submitted, terminal_ref=terminal)
    assert manifest.submitted_ref.kind == "karakeep_bookmark"  # type: ignore[union-attr]
    assert manifest.terminal_ref.kind == "arxiv"  # type: ignore[union-attr]


def test_manifest_v2_direct_url_job():
    """For direct UrlRef submission, submitted_ref == terminal_ref (both kind='url')."""
    ref = UrlRef(url="https://example.com/page")
    manifest = _base_manifest_v2(submitted_ref=ref, terminal_ref=ref)
    assert manifest.submitted_ref.kind == "url"  # type: ignore[union-attr]
    assert manifest.terminal_ref.kind == "url"  # type: ignore[union-attr]


def test_manifest_v2_nullable_source_fields():
    """generate_manifest_v2(source_url=None, ...) produces ManifestV2 with source.url is None."""
    manifest = _base_manifest_v2(
        source_url=None,
        source_normalized_url=None,
        source_title=None,
        source_type=None,
    )
    assert manifest.source.url is None
    assert manifest.source.normalized_url is None
    assert manifest.source.title is None
    assert manifest.source.source_type is None
    assert manifest.source.fetched_at == _FETCHED_AT


def test_manifest_v2_unknown_fields_raise():
    """ManifestV2 rejects extra fields at read time."""
    v2_manifest = _base_manifest_v2()
    data = v2_manifest.model_dump()
    data["unexpected_field"] = "surprise"
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(data)
