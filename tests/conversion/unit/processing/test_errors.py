"""Unit tests for the worker error taxonomy and traceback persistence.

Covers:
  - Error class taxonomy (error_code, retryable) for every exception type the
    adapter maps onto job status.
  - Traceback capture in `_report_status` and persistence of the terminal
    failure write through the runner adapter's ``finalize``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.processing import converter, errors as errors_mod, fetcher
from aizk.conversion.processing.errors import ReportedChildError, SubprocessMetadataInvalid
from aizk.conversion.processing.subproc import _report_status
from aizk.conversion.storage.s3_client import S3Error, S3UploadError
from aizk.conversion.utilities.bookmark_utils import BookmarkContentError
from aizk.conversion.utilities.config import ConversionConfig


def _finalize_failure(db_session: Session, job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Persist a terminal FAILED write through the adapter's ``finalize``.

    The adapter's ``execute`` stashes the scrubbed :class:`JobErrorDetails` on
    failure and ``map_result`` resolves the retry class; this helper reproduces
    that bridge (stash via ``classify_job_error`` and resolve via
    ``map_result``) so ``finalize`` writes the terminal status, ``error_*``
    fields, and ``failed`` event.
    """
    handler = ConversionStageHandler(config)
    outcome = handler.map_result(error)
    from aizk.conversion.processing.errors import classify_job_error

    handler._error_details[job_id] = classify_job_error(error)
    with Session(db_session.get_bind()) as session:
        handler.finalize(session, job_id, outcome)
        session.commit()


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
            (SubprocessMetadataInvalid, "subprocess_metadata_invalid", False),
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
    monkeypatch.setenv("AIZK_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AIZK_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AIZK_S3_REGION", "us-east-1")
    monkeypatch.setenv("AIZK_S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("AIZK_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AIZK_RETRY_BASE_DELAY_SECONDS", "0")
    return ConversionConfig(_env_file=None)


@pytest.fixture()
def bookmark(db_session: Session) -> Source:
    """Create and return a test source."""
    _ref = KarakeepBookmarkRef(bookmark_id="bm_traceback_test")
    bm = Source(
        karakeep_id="bm_traceback_test",
        source_ref=_ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref),
        owner_id="self",
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
        owner_id="self",
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
# finalize — persists error_detail (the terminal failure write)
# ---------------------------------------------------------------------------


def test_finalize_stores_traceback_in_error_detail(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
) -> None:
    tb = "Traceback (most recent call last):\n  File 'converter.py'\nKeyError: 'content'"
    error = ReportedChildError("conversion failed", "docling_error", traceback=tb)

    _finalize_failure(db_session, job.id, error, config)

    db_session.expire_all()
    updated_job = db_session.get(ConversionJob, job.id)
    assert updated_job is not None
    assert updated_job.error_detail == tb
    assert updated_job.error_message == "conversion failed"
    assert updated_job.error_code == "docling_error"


def test_finalize_stores_none_detail_when_no_traceback(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
) -> None:
    error = ReportedChildError("timeout", "conversion_timeout")

    _finalize_failure(db_session, job.id, error, config)

    db_session.expire_all()
    updated_job = db_session.get(ConversionJob, job.id)
    assert updated_job is not None
    assert updated_job.error_detail is None
    assert updated_job.error_message == "timeout"


# ---------------------------------------------------------------------------
# finalize — error.retryable → status mapping (via map_result + finalize)
#
# Pins the contract that drives retry behavior: a regression that flips
# FAILED_RETRYABLE ↔ FAILED_PERM for any error class would silently break
# the worker's retry loop or eat permanent failures into infinite retries.
# ``ConversionCancelledError`` is intentionally absent here — under the runner
# adapter cancellation maps to the CANCELLED lifecycle (not FAILED_PERM); that
# path is covered by ``test_handler.py``'s map_result + finalize tests.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_factory", "expected_status", "expects_finished_at"),
    [
        # Retryable errors → FAILED_RETRYABLE, finished_at stays None.
        pytest.param(
            lambda: errors_mod.ConversionSubprocessError("subprocess died"),
            ConversionJobStatus.FAILED_RETRYABLE,
            False,
            id="ConversionSubprocessError-retryable",
        ),
        pytest.param(
            lambda: errors_mod.PreflightError("preflight failed"),
            ConversionJobStatus.FAILED_RETRYABLE,
            False,
            id="PreflightError-retryable",
        ),
        pytest.param(
            lambda: errors_mod.ConversionTimeoutError("timed out", phase="converting"),
            ConversionJobStatus.FAILED_RETRYABLE,
            False,
            id="ConversionTimeoutError-retryable",
        ),
        pytest.param(
            lambda: fetcher.FetchError("fetch failed"),
            ConversionJobStatus.FAILED_RETRYABLE,
            False,
            id="FetchError-retryable",
        ),
        pytest.param(
            lambda: S3Error("bucket unreachable", "s3_error"),
            ConversionJobStatus.FAILED_RETRYABLE,
            False,
            id="S3Error-retryable",
        ),
        pytest.param(
            lambda: ReportedChildError("transient", "transient_code"),
            ConversionJobStatus.FAILED_RETRYABLE,
            False,
            id="ReportedChildError-default-retryable",
        ),
        # Permanent errors → FAILED_PERM, finished_at set.
        pytest.param(
            lambda: errors_mod.JobDataIntegrityError("bad job"),
            ConversionJobStatus.FAILED_PERM,
            True,
            id="JobDataIntegrityError-permanent",
        ),
        pytest.param(
            lambda: errors_mod.ConversionArtifactsMissingError("no artifacts"),
            ConversionJobStatus.FAILED_PERM,
            True,
            id="ConversionArtifactsMissingError-permanent",
        ),
        pytest.param(
            lambda: BookmarkContentError("missing content"),
            ConversionJobStatus.FAILED_PERM,
            True,
            id="BookmarkContentError-permanent",
        ),
        pytest.param(
            lambda: converter.DoclingEmptyOutputError(),
            ConversionJobStatus.FAILED_PERM,
            True,
            id="DoclingEmptyOutputError-permanent",
        ),
        pytest.param(
            lambda: ReportedChildError("permanent child", "docling_empty_output", retryable=False),
            ConversionJobStatus.FAILED_PERM,
            True,
            id="ReportedChildError-marked-permanent",
        ),
        pytest.param(
            lambda: SubprocessMetadataInvalid("malformed metadata.json"),
            ConversionJobStatus.FAILED_PERM,
            True,
            id="SubprocessMetadataInvalid-permanent",
        ),
    ],
)
def test_finalize_maps_retryable_to_status(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
    error_factory,
    expected_status: ConversionJobStatus,
    expects_finished_at: bool,
) -> None:
    error = error_factory()
    _finalize_failure(db_session, job.id, error, config)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, job.id)
    assert updated is not None
    assert updated.status == expected_status
    if expects_finished_at:
        assert updated.finished_at is not None
        assert updated.earliest_next_attempt_at is None
    else:
        assert updated.finished_at is None
        assert updated.earliest_next_attempt_at is not None


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: FileNotFoundError("missing artifact"), id="FileNotFoundError"),
        pytest.param(lambda: KeyError("markdown_filename"), id="KeyError"),
        pytest.param(lambda: OSError("disk full"), id="OSError"),
        pytest.param(lambda: RuntimeError("unexpected"), id="RuntimeError"),
    ],
)
def test_finalize_handles_plain_exception_without_retryable_attr(
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
    error_factory,
) -> None:
    # Regression for H6: plain Python exceptions lack the conversion-error
    # contract (no `retryable` attribute). The map_result + finalize path must
    # default to retryable=True rather than raise AttributeError, otherwise an
    # unexpected exception leaking from the upload-retry arm would silently leave
    # the job stuck in RUNNING state.
    error = error_factory()
    _finalize_failure(db_session, job.id, error, config)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, job.id)
    assert updated is not None
    # Default policy: when in doubt, retry.
    assert updated.status == ConversionJobStatus.FAILED_RETRYABLE
    # error_code falls back to the existing default in classify_job_error.
    assert updated.error_code == "conversion_failed"


def test_finalize_skips_cancelled_job(
    db_session: Session,
    bookmark: Source,
    config: ConversionConfig,
) -> None:
    """A CANCELLED job must not be re-mapped to FAILED_* by finalize."""
    j = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        owner_id="self",
        title=bookmark.title,
        status=ConversionJobStatus.CANCELLED,
        idempotency_key="cancelled-key",
        attempts=1,
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)

    _finalize_failure(db_session, j.id, errors_mod.ConversionSubprocessError("late failure"), config)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, j.id)
    assert updated is not None
    assert updated.status == ConversionJobStatus.CANCELLED


# ---------------------------------------------------------------------------
# Migration — error_detail column exists
# ---------------------------------------------------------------------------


def test_error_detail_column_exists_after_migration(db_session: Session, bookmark: Source) -> None:
    """Verify the error_detail column is writable after migrations run."""
    j = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        owner_id="self",
        title="Migration Test",
        status=ConversionJobStatus.FAILED_PERM,
        idempotency_key="migration-test-key",
        error_detail="Traceback ...",
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)

    assert j.error_detail == "Traceback ..."


# ---------------------------------------------------------------------------
# _load_subprocess_metadata — raises SubprocessMetadataInvalid for bad JSON
# ---------------------------------------------------------------------------


_VALID_SUBPROCESS_METADATA = {
    "pipeline_name": "html",
    "terminal_ref": {"kind": "inline_html", "body": "dGVzdA=="},
    "content_type": "html",
    "markdown_filename": "output.md",
    "figure_files": [],
    "markdown_hash_xx64": "abc123def456789a",
    "docling_version": "test",
    "config_snapshot": {"converter_name": "docling"},
    "fetched_at": "2026-01-01T00:00:00+00:00",
    "source_meta": {
        "source_url": None,
        "normalized_url": None,
        "document_base_url": None,
        "resolver_title": None,
    },
    "document_title": None,
    "source_title": None,
}


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({**_VALID_SUBPROCESS_METADATA, "unexpected_field": "oops"}, id="unknown_extra_field"),
        pytest.param(
            {k: v for k, v in _VALID_SUBPROCESS_METADATA.items() if k != "pipeline_name"},
            id="missing_required_field",
        ),
    ],
)
def test_load_subprocess_metadata_raises_on_invalid(tmp_path, metadata):
    """metadata.json with an unknown extra field or a missing required field raises SubprocessMetadataInvalid."""
    from aizk.conversion.processing.uploader import _load_subprocess_metadata

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "metadata.json").write_text(json.dumps(metadata))

    with pytest.raises(SubprocessMetadataInvalid) as exc_info:
        _load_subprocess_metadata(ws, job_id=1)

    assert exc_info.value.error_code == "subprocess_metadata_invalid"
    assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# Integrated — _prepare_upload failure → finalize → FAILED_PERM
# ---------------------------------------------------------------------------


def test_prepare_upload_invalid_metadata_flows_to_failed_perm(
    tmp_path,
    db_session: Session,
    job: ConversionJob,
    config: ConversionConfig,
) -> None:
    """SubprocessMetadataInvalid from _prepare_upload flows through the adapter's
    finalize to FAILED_PERM — ``execute`` raises it, ``map_result`` classifies it
    PERMANENT, and ``finalize`` writes the terminal FAILED_PERM status."""

    from aizk.conversion.processing.uploader import _prepare_upload

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "metadata.json").write_text(json.dumps({"unexpected_only": "oops"}))

    try:
        _prepare_upload(job.id, ws, config)
    except Exception as exc:
        _finalize_failure(db_session, job.id, exc, config)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, job.id)
    assert updated is not None
    assert updated.status == ConversionJobStatus.FAILED_PERM
    assert updated.error_code == "subprocess_metadata_invalid"
    assert updated.finished_at is not None
