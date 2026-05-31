"""Exception classes and failure classification for the conversion worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from aizk.conversion.core.errors import EgressPolicyError


class ConversionArtifactsMissingError(RuntimeError):
    """Raised when expected conversion artifacts are missing."""

    error_code = "conversion_artifacts_missing"
    retryable: ClassVar[bool] = False


class ConversionCancelledError(RuntimeError):
    """Raised when a conversion job is cancelled during processing."""

    error_code = "conversion_cancelled"
    retryable: ClassVar[bool] = False


class ConversionTimeoutError(RuntimeError):
    """Raised when a conversion job exceeds the configured timeout."""

    error_code = "conversion_timeout"
    retryable: ClassVar[bool] = True

    def __init__(self, message: str, phase: str) -> None:
        super().__init__(message)
        self.phase = phase


class ConversionSubprocessError(RuntimeError):
    """Raised when the conversion subprocess exits unexpectedly."""

    error_code = "conversion_subprocess_failed"
    retryable: ClassVar[bool] = True


class JobDataIntegrityError(RuntimeError):
    """Raised when job data invariants are violated."""

    error_code = "job_data_integrity"
    retryable: ClassVar[bool] = False


class ReportedChildError(RuntimeError):
    """Raised when a child process reports a failure."""

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        error_code: str,
        *,
        retryable: bool | None = None,
        traceback: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.traceback = traceback
        if retryable is not None:
            self.retryable = retryable


class PreflightError(RuntimeError):
    """Raised when preflight validation fails unexpectedly."""

    error_code = "conversion_preflight_failed"
    retryable: ClassVar[bool] = True


class SubprocessMetadataInvalid(RuntimeError):  # noqa: N818
    """Raised when metadata.json cannot be validated as SubprocessMetadata.

    Triggered on unknown extra fields, missing required fields, or type mismatches.
    Permanent failure — the subprocess produced a schema-incompatible artifact.
    """

    error_code = "subprocess_metadata_invalid"
    retryable: ClassVar[bool] = False


_EGRESS_POLICY_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "deny_list",
        "disallowed_scheme",
        "redirect_egress_violation",
        "dns_timeout",
        "workspace_escape",
        "egress_policy_violation",
    }
)


@dataclass(frozen=True)
class JobErrorDetails:
    """Scrubbed, persistence-ready failure details extracted from an exception.

    Carries exactly the values the ``ConversionStageHandler.finalize`` adapter
    writes to ``ConversionJob.error_*`` and the ``FailedPayload`` event: the
    ``error_code``, the egress-scrubbed ``error_message`` and ``error_detail``,
    the retry disposition, and the optional ``last_phase``. Egress-policy errors
    are already scrubbed here
    (``error_message`` is the bare code, ``error_detail`` is ``None``) so a
    rejected URL/IP never reaches the caller, let alone durable storage.
    """

    error_code: str
    error_message: str
    error_detail: str | None
    retryable: bool
    last_phase: str | None


def classify_job_error(error: Exception) -> JobErrorDetails:
    """Extract the scrubbed, persistence-ready failure details from ``error``.

    The single source of truth for how a conversion exception maps to the
    durable failure fields, called by the ``ConversionStageHandler.finalize``
    adapter so the error_code, retry decision, and the egress scrub stay
    consistent.

    Egress-policy errors carry rejected destinations (URLs, IPs) in their
    message/traceback. They are scrubbed here on two paths:

    1. Direct ``isinstance(error, EgressPolicyError)`` — error raised in this
       process (e.g., from ``_get_source_ref``).
    2. ``ReportedChildError`` whose ``error_code`` is one of the egress-policy
       codes — error raised in the conversion subprocess and reported up.

    Both paths set ``error_message`` to the bare ``error_code`` and drop
    ``error_detail`` (which would otherwise carry the destination via the
    traceback). The full detail is already captured by the enforcement-site
    WARNING logs in egress.py / egress_fetch.py / paths.py.
    """
    error_code = getattr(error, "error_code", "conversion_failed")
    error_detail = getattr(error, "traceback", None)

    # Default to retryable=True for exceptions that lack the conversion-error
    # contract (plain OSError, KeyError, etc. that may leak from the upload-retry
    # arm). The unknown-exception default matches FetchError's policy: when in
    # doubt, retry rather than mark permanent.
    retryable: bool = bool(getattr(error, "retryable", True))

    is_egress_policy = isinstance(error, EgressPolicyError) or error_code in _EGRESS_POLICY_ERROR_CODES
    if is_egress_policy:
        message = error_code
        error_detail = None
    else:
        message = str(error)

    last_phase = getattr(error, "last_phase", None)
    return JobErrorDetails(
        error_code=error_code,
        error_message=message,
        error_detail=error_detail,
        retryable=retryable,
        last_phase=last_phase,
    )
