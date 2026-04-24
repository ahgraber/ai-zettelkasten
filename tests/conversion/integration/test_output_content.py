"""Integration tests for conversion output content endpoints."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.storage.manifest import (
    ManifestArtifactMarkdown,
    ManifestArtifacts,
    ManifestConfigSnapshot,
    ManifestConversionMetadata,
    ManifestSource,
    ManifestV1,
)
from aizk.conversion.storage.s3_client import S3Client, S3Error, S3NotFoundError
from tests.conversion._helpers import make_job, make_source


def _create_bookmark(session, karakeep_id: str):
    # KarakeepBookmarkRef.bookmark_id rejects dots and paths; sanitize for the ref only.
    safe_bookmark_id = karakeep_id.replace(".", "_")[:64]
    return make_source(session, karakeep_id, source_ref_bookmark_id=safe_bookmark_id)


def _create_job(session, *, aizk_uuid, idempotency_key: str):
    return make_job(
        session,
        aizk_uuid=aizk_uuid,
        idempotency_key=idempotency_key,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
    )


def _create_output(session, *, job_id: int, aizk_uuid: UUID, s3_prefix: str = "prefix/abc") -> ConversionOutput:
    output = ConversionOutput(
        job_id=job_id,
        aizk_uuid=aizk_uuid,
        title="Test Output",
        payload_version=1,
        s3_prefix=s3_prefix,
        markdown_key=f"{s3_prefix}/output.md",
        manifest_key=f"{s3_prefix}/manifest.json",
        markdown_hash_xx64="aabbccdd11223344",
        figure_count=2,
        docling_version="1.0.0",
        pipeline_name="default",
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    session.add(output)
    session.commit()
    session.refresh(output)
    return output


def _manifest_v1_bytes(aizk_uuid: UUID, karakeep_id: str) -> bytes:
    manifest = ManifestV1(
        aizk_uuid=aizk_uuid,
        karakeep_id=karakeep_id,
        source=ManifestSource(
            url="https://example.com/page",
            normalized_url="https://example.com/page",
            title="Example",
            source_type="other",
            fetched_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        ),
        conversion=ManifestConversionMetadata(
            job_id=1,
            payload_version=1,
            docling_version="2.0.0",
            pipeline_name="html",
            started_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            finished_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            duration_seconds=0,
        ),
        artifacts=ManifestArtifacts(
            markdown=ManifestArtifactMarkdown(
                key="s3://bucket/output.md",
                hash_xx64="abcd1234",
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ),
            figures=[],
        ),
        config_snapshot=ManifestConfigSnapshot(
            docling_pdf_max_pages=250,
            docling_enable_ocr=True,
            docling_enable_table_structure=True,
            docling_picture_description_model="openai/gpt-5-nano",
            docling_picture_timeout=180.0,
            docling_enable_picture_classification=True,
            picture_description_enabled=False,
        ),
    )
    return manifest.model_dump_json().encode("utf-8")


@pytest.fixture()
def mock_s3() -> MagicMock:
    """Return a mock S3Client."""
    return MagicMock(spec=S3Client)


@pytest.fixture()
def client(db_session, mock_s3) -> TestClient:
    from aizk.conversion.api.dependencies import get_db_session, get_s3_client

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    return TestClient(app)


# --- happy paths ---


def test_get_manifest_returns_json_bytes(db_session, client, mock_s3) -> None:
    bookmark = _create_bookmark(db_session, "bm_manifest")
    job = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="a" * 64)
    output = _create_output(db_session, job_id=job.id, aizk_uuid=bookmark.aizk_uuid)
    manifest_bytes = _manifest_v1_bytes(bookmark.aizk_uuid, bookmark.karakeep_id)
    mock_s3.get_object_bytes.return_value = manifest_bytes

    response = client.get(f"/v1/outputs/{output.id}/manifest")

    assert response.status_code == 200
    assert response.content == manifest_bytes
    assert "application/json" in response.headers["content-type"]
    mock_s3.get_object_bytes.assert_called_once_with(output.manifest_key)


def test_get_markdown_returns_text(db_session, client, mock_s3) -> None:
    bookmark = _create_bookmark(db_session, "bm_markdown")
    job = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="d" * 64)
    output = _create_output(db_session, job_id=job.id, aizk_uuid=bookmark.aizk_uuid)
    mock_s3.get_object_bytes.return_value = b"# Title\n\nBody text."

    response = client.get(f"/v1/outputs/{output.id}/markdown")

    assert response.status_code == 200
    assert response.content == b"# Title\n\nBody text."
    assert "text/markdown" in response.headers["content-type"]
    mock_s3.get_object_bytes.assert_called_once_with(output.markdown_key)


def test_get_figure_returns_image_with_correct_content_type(db_session, client, mock_s3) -> None:
    bookmark = _create_bookmark(db_session, "bm_figure")
    job = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="e" * 64)
    output = _create_output(db_session, job_id=job.id, aizk_uuid=bookmark.aizk_uuid, s3_prefix="prefix/fig")
    mock_s3.get_object_bytes.return_value = b"\x89PNG\r\n"

    response = client.get(f"/v1/outputs/{output.id}/figures/image-001.png")

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n"
    assert response.headers["content-type"] == "image/png"
    mock_s3.get_object_bytes.assert_called_once_with("prefix/fig/figures/image-001.png")


# --- error scenarios (parametrized across endpoints) ---

_ENDPOINT_SUFFIXES = ["manifest", "markdown", "figures/image-001.png"]


@pytest.mark.parametrize("suffix", _ENDPOINT_SUFFIXES)
def test_endpoint_404_unknown_output(client, suffix) -> None:
    response = client.get(f"/v1/outputs/99999/{suffix}")
    assert response.status_code == 404


@pytest.mark.parametrize("suffix", _ENDPOINT_SUFFIXES)
def test_endpoint_404_when_s3_not_found(db_session, client, mock_s3, suffix) -> None:
    bookmark = _create_bookmark(db_session, f"bm_missing_{suffix.replace('/', '_')}")
    key = suffix.replace("/", "_")
    job = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key=key.ljust(64, "0"))
    output = _create_output(db_session, job_id=job.id, aizk_uuid=bookmark.aizk_uuid)
    mock_s3.get_object_bytes.side_effect = S3NotFoundError("missing")

    response = client.get(f"/v1/outputs/{output.id}/{suffix}")

    assert response.status_code == 404


@pytest.mark.parametrize("suffix", _ENDPOINT_SUFFIXES)
def test_endpoint_502_on_s3_error(db_session, client, mock_s3, suffix) -> None:
    bookmark = _create_bookmark(db_session, f"bm_err_{suffix.replace('/', '_')}")
    key = ("err_" + suffix.replace("/", "_")).ljust(64, "0")
    job = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key=key)
    output = _create_output(db_session, job_id=job.id, aizk_uuid=bookmark.aizk_uuid)
    mock_s3.get_object_bytes.side_effect = S3Error("storage down", "s3_error")

    response = client.get(f"/v1/outputs/{output.id}/{suffix}")

    assert response.status_code == 502


# --- figures: endpoint-specific cases ---


@pytest.mark.parametrize("filename", ["../escape.png", "sub/dir.png", "/abs.png"])
def test_get_figure_rejects_path_traversal(client, filename) -> None:
    # FastAPI's router intercepts filenames containing "/" or ".." before the handler
    # runs (no route match → 404), while bare invalid names reach the handler (→ 400).
    # Either way the request is rejected — assert no 2xx/5xx response.
    response = client.get(f"/v1/outputs/1/figures/{filename}")
    assert response.status_code in {400, 404}


def test_get_figure_404_when_no_figures(db_session, client, mock_s3) -> None:
    bookmark = _create_bookmark(db_session, "bm_no_figures")
    job = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="f" * 64)
    output = _create_output(db_session, job_id=job.id, aizk_uuid=bookmark.aizk_uuid)
    # Patch figure_count to zero so the handler rejects before any S3 lookup
    output.figure_count = 0
    db_session.add(output)
    db_session.commit()

    response = client.get(f"/v1/outputs/{output.id}/figures/image-001.png")

    assert response.status_code == 404
    mock_s3.get_object_bytes.assert_not_called()
