"""Unit tests for manifest generation (v2 writer + v1/v2 readers)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from aizk.conversion.storage.manifest import (
    ConversionManifest,
    ConversionManifestV1,
    ConversionManifestV2,
    ManifestConfigSnapshotV1,
    ManifestConfigSnapshotV2,
    generate_manifest,
    load_manifest,
)

_DEFAULT_AIZK_UUID = UUID("12345678-1234-5678-1234-567812345678")
_FETCHED_AT = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_source(
    aizk_uuid: UUID = _DEFAULT_AIZK_UUID,
    karakeep_id: str | None = "kk-001",
    url: str | None = "https://example.com/page",
    normalized_url: str | None = "https://example.com/page",
    title: str | None = "Example",
    source_type: str | None = "other",
    source_ref: dict | None = None,
) -> MagicMock:
    src = MagicMock()
    src.aizk_uuid = aizk_uuid
    src.karakeep_id = karakeep_id
    src.url = url
    src.normalized_url = normalized_url
    src.title = title
    src.source_type = source_type
    src.source_ref = source_ref or {"kind": "karakeep_bookmark", "bookmark_id": karakeep_id or "kk-001"}
    return src


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


def _snapshot_v2(converter_name: str = "docling", **adapter) -> ManifestConfigSnapshotV2:
    defaults = {
        "pdf_max_pages": 250,
        "ocr_enabled": True,
        "table_structure_enabled": True,
        "picture_description_model": "openai/gpt-5-nano",
        "picture_timeout": 180.0,
        "picture_classification_enabled": True,
        "picture_description_enabled": False,
    }
    defaults.update(adapter)
    return ManifestConfigSnapshotV2(converter_name=converter_name, adapter=defaults)


def _base_manifest(*, picture_description_enabled: bool = False, **source_kwargs) -> ConversionManifestV2:
    return generate_manifest(
        source=_make_source(**source_kwargs),
        job=_make_job(),
        fetched_at=_FETCHED_AT,
        markdown_s3_uri="s3://bucket/output.md",
        markdown_hash="abcd1234",
        figure_s3_uris=[],
        docling_version="2.0.0",
        pipeline_name="html",
        config_snapshot=_snapshot_v2(picture_description_enabled=picture_description_enabled),
    )


# ---------------------------------------------------------------------------
# Writer emits v2 with expected shape
# ---------------------------------------------------------------------------


def test_writer_emits_version_2():
    manifest = _base_manifest()
    assert manifest.version == "2.0"


def test_writer_emits_converter_name_in_config_snapshot():
    manifest = _base_manifest()
    assert manifest.config_snapshot.converter_name == "docling"


def test_writer_emits_karakeep_terminal_provenance_with_bookmark_id():
    """For a KaraKeep-terminal job: provenance.kind == karakeep_bookmark, no ingress."""
    manifest = _base_manifest(source_type="other", url=None)
    assert manifest.provenance.kind == "karakeep_bookmark"
    assert manifest.provenance.bookmark_id == "kk-001"
    assert manifest.ingress is None


def test_writer_emits_arxiv_provenance_and_karakeep_ingress_for_arxiv_source_type():
    """For a KaraKeep-to-arxiv job: provenance.kind == arxiv, ingress.kind == karakeep_bookmark."""
    manifest = _base_manifest(
        source_type="arxiv",
        url="https://arxiv.org/abs/2301.12345",
        karakeep_id="bm_arxiv",
        source_ref={"kind": "karakeep_bookmark", "bookmark_id": "bm_arxiv"},
    )
    assert manifest.provenance.kind == "arxiv"
    assert manifest.provenance.arxiv_id == "2301.12345"
    assert manifest.ingress is not None
    assert manifest.ingress.kind == "karakeep_bookmark"
    assert manifest.ingress.bookmark_id == "bm_arxiv"


def test_writer_source_fields_nullable_for_sparse_sources():
    manifest = _base_manifest(
        url=None,
        normalized_url=None,
        title=None,
        source_type=None,
    )
    assert manifest.source.url is None
    assert manifest.source.normalized_url is None
    assert manifest.source.title is None
    assert manifest.source.source_type is None


# ---------------------------------------------------------------------------
# extra="forbid" contracts
# ---------------------------------------------------------------------------


def test_manifest_v2_forbids_extra_fields():
    assert ConversionManifestV2.model_config["extra"] == "forbid"


def test_config_snapshot_v2_forbids_extra_fields():
    assert ManifestConfigSnapshotV2.model_config["extra"] == "forbid"


# ---------------------------------------------------------------------------
# Inline HTML provenance — end-to-end SourceRef → storage → manifest writer
# ---------------------------------------------------------------------------


def test_writer_emits_inline_html_provenance_with_content_hash_from_storage_payload():
    """MF3 regression: manifest writer reads content_hash from the stored payload.

    Before the fix, ``InlineHtmlRef.model_dump()`` wrote ``body`` (not
    ``content_hash``) to the ``source_ref`` column. The manifest writer's
    inline_html branch raised ValueError because content_hash was absent. This
    test exercises the full round-trip: ref.to_storage_payload() → source_ref
    column → generate_manifest() → ManifestProvenanceInline with the expected
    hash.
    """
    import hashlib

    from aizk.conversion.core.source_ref import InlineHtmlRef

    body = b"<html><body>hello</body></html>"
    ref = InlineHtmlRef(body=body)
    expected_hash = hashlib.sha256(body).hexdigest()

    manifest = _base_manifest(
        karakeep_id=None,
        url=None,
        source_type=None,
        source_ref=ref.to_storage_payload(),
    )

    assert manifest.provenance.kind == "inline_html"
    assert manifest.provenance.content_hash == expected_hash
    assert manifest.ingress is None


def test_manifest_v1_forbids_extra_fields():
    assert ConversionManifestV1.model_config["extra"] == "forbid"


# ---------------------------------------------------------------------------
# Version-dispatching loader
# ---------------------------------------------------------------------------


def test_load_manifest_dispatches_v1():
    legacy = {
        "version": "1.0",
        "aizk_uuid": str(_DEFAULT_AIZK_UUID),
        "karakeep_id": "kk-legacy",
        "source": {
            "url": "https://example.com",
            "normalized_url": "https://example.com",
            "title": "Legacy",
            "source_type": "other",
            "fetched_at": _FETCHED_AT.isoformat(),
        },
        "conversion": {
            "job_id": 1,
            "payload_version": 1,
            "docling_version": "1.0.0",
            "pipeline_name": "html",
            "started_at": _FETCHED_AT.isoformat(),
            "finished_at": _FETCHED_AT.isoformat(),
            "duration_seconds": 0,
        },
        "artifacts": {
            "markdown": {
                "key": "s3://bucket/legacy.md",
                "hash_xx64": "abcd",
                "created_at": _FETCHED_AT.isoformat(),
            },
            "figures": [],
        },
        "config_snapshot": {
            "docling_pdf_max_pages": 250,
            "docling_enable_ocr": True,
            "docling_enable_table_structure": True,
            "docling_picture_description_model": "m",
            "docling_picture_timeout": 1.0,
            "docling_enable_picture_classification": True,
            "picture_description_enabled": False,
        },
    }
    loaded = load_manifest(legacy)
    assert isinstance(loaded, ConversionManifestV1)
    assert loaded.karakeep_id == "kk-legacy"


def test_load_manifest_dispatches_v2():
    manifest = _base_manifest()
    data = manifest.model_dump(mode="json")
    loaded = load_manifest(data)
    assert isinstance(loaded, ConversionManifestV2)
    assert loaded.version == "2.0"


def test_load_manifest_rejects_unknown_version():
    with pytest.raises(ValueError, match="Unknown manifest version"):
        load_manifest({"version": "99.9"})


# ---------------------------------------------------------------------------
# ConversionManifest alias
# ---------------------------------------------------------------------------


def test_conversion_manifest_alias_is_v2():
    assert ConversionManifest is ConversionManifestV2
