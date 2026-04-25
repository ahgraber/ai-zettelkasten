"""Unit tests for the worker error taxonomy and traceback persistence.

Covers:
  - Error class taxonomy (error_code, retryable) for every exception type the
    orchestrator maps onto job status.
  - Traceback capture and persistence in `_report_status` / `handle_job_error`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.conversion.storage.s3_client import S3Error, S3UploadError
from aizk.conversion.utilities.bookmark_utils import BookmarkContentError
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import converter, errors as errors_mod, fetcher
from aizk.conversion.workers.errors import ReportedChildError
from aizk.conversion.workers.orchestrator import (
    _report_status,
    handle_job_error,
)

# ---------------------------------------------------------------------------
# Error taxonomy: error_code + retryable
# ---------------------------------------------------------------------------


class TestErrorTaxonomy:
    """Every exception class carries an explicit error_code + retryable."""

    @pytest.mark.parametrize(
        "exc_cls, expected_code, expected_retryable",
        [
            (errors_mod.JobDataIntegrityError, "job_data_integrity", False),
            (errors_mod.ConversionArtifactsMissingError, "conversion_artifacts_missing", False),
            (errors_mod.ConversionCancelledError, "conversion_cancelled", False),
            (errors_mod.ConversionSubprocessError, "conversion_subprocess_failed", True),
            (errors_mod.PreflightError, "conversion_preflight_failed", True),
            (BookmarkContentError, "karakeep_bookmark_missing_contents", False),
            (fetcher.FetchError, "fetch_error", True),
        ],
        ids=lambda v: v.__name__ if isinstance(v, type) else None,
    )
    def test_simple_constructor(self, exc_cls, expected_code, expected_retryable) -> None:
        err = exc_cls("test message")
        assert err.error_code == expected_code
        assert err.retryable is expected_retryable

    def test_conversion_timeout_carries_phase(self) -> None:
        err = errors_mod.ConversionTimeoutError("timeout", phase="converting")
        assert err.error_code == "conversion_timeout"
        assert err.retryable is True

    def test_reported_child_defaults_to_retryable(self) -> None:
        err = errors_mod.ReportedChildError("child failed", "transient")
        assert err.error_code == "transient"
        assert err.retryable is True

    def test_reported_child_can_be_marked_permanent(self) -> None:
        err = errors_mod.ReportedChildError("child failed", "docling_empty_output", retryable=False)
        assert err.error_code == "docling_empty_output"
        assert err.retryable is False

    def test_reported_child_retryable_kwarg_round_trips(self) -> None:
        err = errors_mod.ReportedChildError("child failed", "transient", retryable=True)
        assert err.retryable is True

    def test_docling_empty_output_is_permanent(self) -> None:
        err = converter.DoclingEmptyOutputError()
        assert err.error_code == "docling_empty_output"
        assert err.retryable is False

    def test_s3_error_is_retryable(self) -> None:
        err = S3Error("bucket not configured", "s3_upload_failed")
        assert err.retryable is True

    def test_s3_upload_error_is_retryable(self) -> None:
        err = S3UploadError("key/obj", "ETag mismatch")
        assert err.retryable is True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config(monkeypatch: pytest.MonkeyPatch) -> ConversionConfig:
    """Return a ConversionConfig with minimal valid settings."""
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "0")
    return ConversionConfig(_env_file=None)


@pytest.fixture()
def bookmark(db_session: Session) -> Source:
    """Create and return a test source."""
    _ref = KarakeepBookmarkRef(bookmark_id="bm_traceback_test")
    bm = Source(
        karakeep_id="bm_traceback_test",
        source_ref=_ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Traceback Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bm)
    db_session.commit()
    db_session.refresh(bm)
    return bm


@pytest.fixture()
def job(db_session: Session, bookmark: Source) -> ConversionJob:
    """Create and return a RUNNING test job."""
    j = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        status=ConversionJobStatus.RUNNING,
        idempotency_key="test-traceback-key",
        attempts=1,
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)
    return j


# ---------------------------------------------------------------------------
# _report_status — traceback field
# ---------------------------------------------------------------------------


def test_report_status_includes_traceback_in_payload() -> None:
    mock_queue = MagicMock()
    tb = "Traceback (most recent call last):\n  File ...\nKeyError: 'content'"

    _report_status(
        mock_queue,
        event="failed",
        message="boom",
        error_code="conversion_failed",
        traceback_text=tb,
    )

    payload = mock_queue.put_nowait.call_args[0][0]
    assert payload["traceback"] == tb
    assert payload["event"] == "failed"
    assert payload["message"] == "boom"


def test_report_status_omits_traceback_when_none() -> None:
    mock_queue = MagicMock()

    _report_status(
        mock_queue,
        event="completed",
        message="done",
    )

    payload = mock_queue.put_nowait.call_args[0][0]
    assert "traceback" not in payload


def test_report_status_omits_traceback_when_empty() -> None:
    mock_queue = MagicMock()

    _report_status(
        mock_queue,
        event="failed",
        message="boom",
        traceback_text="",
    )

    payload = mock_queue.put_nowait.call_args[0][0]
    assert "traceback" not in payload


# ---------------------------------------------------------------------------
# ReportedChildError — traceback attribute
# ---------------------------------------------------------------------------


def test_reported_child_error_carries_traceback() -> None:
    tb = "Traceback (most recent call last):\n  ...\nValueError: bad"
    err = ReportedChildError("bad", "conversion_failed", traceback=tb)

    assert err.traceback == tb
    assert str(err) == "bad"
    assert err.error_code == "conversion_failed"


def test_reported_child_error_traceback_defaults_to_none() -> None:
    err = ReportedChildError("bad", "conversion_failed")

    assert err.traceback is None


# ---------------------------------------------------------------------------
# handle_job_error — persists error_detail
# ---------------------------------------------------------------------------


def test_handle_job_error_stores_traceback_in_error_detail(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
) -> None:
    tb = "Traceback (most recent call last):\n  File 'converter.py'\nKeyError: 'content'"
    error = ReportedChildError("conversion failed", "docling_error", traceback=tb)

    with patch("aizk.conversion.workers.orchestrator.get_engine", return_value=db_session.get_bind()):
        handle_job_error(job.id, error, config)

    db_session.expire_all()
    updated_job = db_session.get(ConversionJob, job.id)
    assert updated_job is not None
    assert updated_job.error_detail == tb
    assert updated_job.error_message == "conversion failed"
    assert updated_job.error_code == "docling_error"


def test_handle_job_error_stores_none_detail_when_no_traceback(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
) -> None:
    error = ReportedChildError("timeout", "conversion_timeout")

    with patch("aizk.conversion.workers.orchestrator.get_engine", return_value=db_session.get_bind()):
        handle_job_error(job.id, error, config)

    db_session.expire_all()
    updated_job = db_session.get(ConversionJob, job.id)
    assert updated_job is not None
    assert updated_job.error_detail is None
    assert updated_job.error_message == "timeout"


def test_handle_job_error_logs_error_with_detail(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tb = "Traceback ...\nKeyError: 'x'"
    error = ReportedChildError("bad key", "docling_error", traceback=tb)

    with patch("aizk.conversion.workers.orchestrator.get_engine", return_value=db_session.get_bind()):
        import logging

        with caplog.at_level(logging.ERROR):
            handle_job_error(job.id, error, config)

    assert "bad key" in caplog.text
    assert "docling_error" in caplog.text


# ---------------------------------------------------------------------------
# Migration — error_detail column exists
# ---------------------------------------------------------------------------


def test_error_detail_column_exists_after_migration(db_session: Session, bookmark: Source) -> None:
    """Verify the error_detail column is writable after migrations run."""
    j = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title="Migration Test",
        status=ConversionJobStatus.FAILED_PERM,
        idempotency_key="migration-test-key",
        error_detail="Traceback ...",
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)

    assert j.error_detail == "Traceback ..."
