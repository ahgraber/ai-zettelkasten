"""Unit tests for error-handling hygiene and diagnostic logging.

Covers:
  - EgressPolicyError persisted error_message is sanitized (no rejected destination).
  - Enforcement-site WARNING logs carry the rejected destination for SOC diagnostic use.
  - Oversized-image prefetch produces a WARNING naming FetchTooLargeError with URL and cap.
  - prefetch_images emits a per-conversion summary line exactly once per call.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import socket
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session

from aizk.conversion.core.errors import (
    DenyListDestination,
    FetchTooLargeError,
)
from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.processing.errors import classify_job_error
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.egress import assert_egress_allowed
from aizk.conversion.utilities.html_prefetch import prefetch_images
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _finalize_failure(db_session: Session, job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Persist a terminal FAILED write through the adapter's ``finalize``.

    Stashes the scrubbed :class:`JobErrorDetails` (via ``classify_job_error``,
    which applies the egress scrub) and calls ``finalize`` with the matching
    FAILED outcome inside a committing session. The scrub is what these security
    tests pin: the rejected destination must never reach the row.
    """
    handler = ConversionStageHandler(config)
    handler._error_details[job_id] = classify_job_error(error)
    retry_class = RetryClass.RETRYABLE if bool(getattr(error, "retryable", True)) else RetryClass.PERMANENT
    with Session(db_session.get_bind()) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, retry_class))
        session.commit()


@pytest.fixture()
def config(monkeypatch: pytest.MonkeyPatch) -> ConversionConfig:
    """Minimal ConversionConfig for orchestrator tests."""
    monkeypatch.setenv("AIZK_RETRY_BASE_DELAY_SECONDS", "0")
    return ConversionConfig(_env_file=None)


@pytest.fixture()
def source(db_session: Session) -> Source:
    """Create a test Source row."""
    ref = KarakeepBookmarkRef(bookmark_id="bm_hygiene_test")
    bm = Source(
        karakeep_id="bm_hygiene_test",
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        owner_id="self",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Hygiene Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bm)
    db_session.commit()
    db_session.refresh(bm)
    return bm


@pytest.fixture()
def running_job(db_session: Session, source: Source) -> ConversionJob:
    """Create a RUNNING ConversionJob for the test source."""
    j = ConversionJob(
        source_id=source.source_id,
        owner_id="self",
        title=source.title,
        status=ConversionJobStatus.RUNNING,
        idempotency_key="hygiene-test-key",
        attempts=1,
    )
    db_session.add(j)
    db_session.commit()
    db_session.refresh(j)
    return j


# ---------------------------------------------------------------------------
# EgressPolicyError error_message sanitization
# ---------------------------------------------------------------------------


def test_classify_job_error_sanitizes_egress_policy_error_message(
    db_session: Session,
    running_job: ConversionJob,
    config: ConversionConfig,
) -> None:
    """Persisted error_message for EgressPolicyError must not contain the rejected destination.

    The error message on the exception carries the rejected host/IP for diagnostic
    purposes, but only the policy-violation class name (error_code) should reach
    the database so it cannot be echoed back to clients.
    """
    # The full exception message contains the rejected destination.
    rejected_host = "internal.corp.example"
    exc = DenyListDestination(f"Resolved address for host {rejected_host!r} is in the egress deny set")

    _finalize_failure(db_session, running_job.id, exc, config)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, running_job.id)
    assert updated is not None

    # Status must be FAILED_PERM (EgressPolicyError.retryable = False).
    assert updated.status == ConversionJobStatus.FAILED_PERM

    # error_message must be the error_code only — never the rejected destination.
    assert updated.error_message == exc.error_code
    assert rejected_host not in (updated.error_message or "")
    # error_detail must be None: the traceback string would otherwise carry
    # the rejected destination via the original exception message.
    assert updated.error_detail is None


def test_job_response_schema_excludes_error_detail() -> None:
    """``JobResponse`` must NOT expose ``error_detail`` to API clients.

    Defence-in-depth pin: even if the persisted ``ConversionJob.error_detail``
    were ever populated with a traceback that contained a rejected destination
    (e.g., a future regression of the egress sanitization in
    ``classify_job_error`` / ``finalize``), the API surface must not surface it.

    Adding ``error_detail`` to ``JobResponse`` is a deliberate change that
    requires removing this test; failing this test on a routine change is the
    intended early-warning signal.
    """
    from aizk.conversion.api.schemas.jobs import JobResponse

    fields = set(JobResponse.model_fields.keys())
    assert "error_detail" not in fields, (
        "JobResponse must not expose error_detail; the field can carry rejected destinations "
        "from EgressPolicyError tracebacks. See network-egress-policy/design.md § 'Typed errors'."
    )


def test_classify_job_error_strips_traceback_for_subprocess_egress_error(
    db_session: Session,
    running_job: ConversionJob,
    config: ConversionConfig,
) -> None:
    """A ReportedChildError carrying an egress-class error_code must drop its traceback.

    The conversion subprocess raises EgressPolicyError, which is caught and
    repackaged by the supervisor as ``ReportedChildError(message, error_code,
    traceback=...)``. The traceback string contains the original exception
    message — including the rejected destination — so persisting it to
    ``error_detail`` would leak the host/IP that the design says shall not be
    echoed back to clients.
    """
    from aizk.conversion.processing.errors import ReportedChildError

    rejected_host = "internal.corp.example"
    leaked_traceback = (
        "Traceback (most recent call last):\n"
        '  File "egress.py", line 268, in assert_egress_allowed\n'
        f"    raise DenyListDestination(\"Resolved address for host '{rejected_host}' is in the egress deny set\")\n"
        f"DenyListDestination: Resolved address for host '{rejected_host}' is in the egress deny set"
    )
    exc = ReportedChildError(
        message=f"Resolved address for host '{rejected_host}' is in the egress deny set",
        error_code="deny_list",
        retryable=False,
        traceback=leaked_traceback,
    )

    _finalize_failure(db_session, running_job.id, exc, config)

    db_session.expire_all()
    updated = db_session.get(ConversionJob, running_job.id)
    assert updated is not None
    assert updated.status == ConversionJobStatus.FAILED_PERM
    assert updated.error_message == "deny_list"
    assert rejected_host not in (updated.error_message or "")
    # The crucial regression: error_detail must be None even though the
    # subprocess sent up a traceback that contains the destination.
    assert updated.error_detail is None
    assert rejected_host not in (updated.error_detail or "")


# ---------------------------------------------------------------------------
# Enforcement-site WARNING log carries rejected destination
# ---------------------------------------------------------------------------


def test_assert_egress_allowed_logs_warning_with_rejected_destination(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assert_egress_allowed must emit a WARNING containing the rejected host and IP.

    The WARNING is the diagnostic record SOC/operators use to investigate policy
    violations without re-running the request. The rejected destination must appear
    in the log even though it is excluded from persisted error_message.
    """
    private_ip = "10.0.0.5"

    def _deny_set_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (private_ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _deny_set_getaddrinfo)

    with (
        caplog.at_level(logging.WARNING, logger="aizk.conversion.utilities.egress"),
        pytest.raises(DenyListDestination),
    ):
        assert_egress_allowed("http://private.corp.example/page")

    # The WARNING must carry the rejected host and IP as structured fields.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING from egress.py"

    deny_record = next(
        (r for r in warning_records if "deny" in r.getMessage().lower()),
        None,
    )
    assert deny_record is not None, f"No deny-set WARNING found; records: {[r.getMessage() for r in warning_records]}"
    assert deny_record.__dict__.get("host") == "private.corp.example"
    assert deny_record.__dict__.get("ip") == private_ip


# ---------------------------------------------------------------------------
# Oversized img prefetch WARNING names error class, URL, and cap
# ---------------------------------------------------------------------------


def test_prefetch_images_logs_warning_for_oversized_image(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An oversized <img> prefetch must produce a WARNING naming FetchTooLargeError, the URL, and the cap.

    The WARNING is the operator-visible signal that an image was silently dropped
    due to the size cap, and it must include enough context to diagnose the drop
    without re-running the request.
    """
    html = '<html><body><img src="http://example.com/huge.png"></body></html>'
    cap = 1024  # very small cap for the test

    with (
        patch(
            "aizk.conversion.utilities.html_prefetch.egress_fetch_bytes",
            new=AsyncMock(
                side_effect=FetchTooLargeError(
                    "Response from 'http://example.com/huge.png' exceeds configured limit of 1024 bytes"
                )
            ),
        ),
        caplog.at_level(logging.WARNING, logger="aizk.conversion.utilities.html_prefetch"),
    ):
        from aizk.conversion.utilities.html_prefetch import PrefetchPolicy

        asyncio.run(prefetch_images(html, tmp_path, policy=PrefetchPolicy(per_image_max_bytes=cap)))

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING for the oversized image"

    size_cap_record = next(
        (r for r in warning_records if "FetchTooLargeError" in str(r.__dict__)),
        None,
    )
    assert size_cap_record is not None, "Expected WARNING with FetchTooLargeError in extra fields"
    assert size_cap_record.__dict__.get("error_class") == "FetchTooLargeError"
    assert "http://example.com/huge.png" in str(size_cap_record.__dict__.get("img_src", ""))
    assert size_cap_record.__dict__.get("per_image_cap_bytes") == cap


# ---------------------------------------------------------------------------
# prefetch_images summary line emitted exactly once per call
# ---------------------------------------------------------------------------


def test_prefetch_images_emits_summary_once_with_correct_counts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """prefetch_images must emit exactly one INFO summary per call with per-failure-mode counts.

    The summary is the primary operator-visible artifact for the prefetch phase:
    it gives a per-call breakdown of success/skip counts without requiring log
    aggregation across multiple warning entries.
    """
    from aizk.conversion.core.errors import EgressPolicyError as _EgressPolicyError, FetchError as _FetchError

    html = """
    <html><body>
      <img src="http://example.com/ok.png">
      <img src="http://deny.example.com/img.png">
      <img src="http://example.com/huge.png">
      <img src="http://example.com/error.png">
    </body></html>
    """

    # Pre-create the images dir so file writes don't fail.
    (tmp_path / "prefetched-images").mkdir()

    async def _side_effect(url, **kwargs):
        if "deny" in url:
            raise _EgressPolicyError("denied")
        if "huge" in url:
            raise FetchTooLargeError("too large")
        if "error" in url:
            raise _FetchError("network error")
        # Success case: return 1-byte PNG stub
        return b"\x89PNG\r\n\x1a\n", {"content-type": "image/png"}

    with (
        patch(
            "aizk.conversion.utilities.html_prefetch.egress_fetch_bytes",
            new=AsyncMock(side_effect=_side_effect),
        ),
        caplog.at_level(logging.INFO, logger="aizk.conversion.utilities.html_prefetch"),
    ):
        asyncio.run(prefetch_images(html, tmp_path))

    # Exactly one INFO summary line per call.
    summary_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "prefetch_images" in r.getMessage() and "prefetched" in r.getMessage()
    ]
    assert len(summary_records) == 1, f"Expected exactly 1 summary line, got {len(summary_records)}"

    msg = summary_records[0].getMessage()
    # 1 success, 1 egress_blocked, 1 too_large, 1 errors
    assert "prefetched 1" in msg
    assert "egress_blocked=1" in msg
    assert "too_large=1" in msg
    assert "errors=1" in msg
