"""Exception classes for the conversion worker."""

from __future__ import annotations

from typing import ClassVar


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
