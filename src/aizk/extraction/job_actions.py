"""Apply operator retry, cancel, and readmission transitions to extraction work-units."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aizk.extraction.core.currentness import Currentness, classify_extraction_source
from aizk.extraction.handler import (
    DEFAULT_RETRY_BASE_DELAY_SECONDS as EXTRACTION_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_RETRY_MAX_ATTEMPTS as EXTRACTION_RETRY_MAX_ATTEMPTS,
    DEFAULT_STALE_AFTER_MINUTES as EXTRACTION_STALE_AFTER_MINUTES,
    DEFAULT_TIMEOUT_SECONDS as EXTRACTION_TIMEOUT_SECONDS,
)
from aizk.extraction.stage import EXTRACTION_DEFINITION
from aizk.pipeline.lifecycle import TERMINAL_STATUSES, is_terminal
from aizk.pipeline.repository import WorkUnitRepository

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.graph.mentions import ResolvedExtractionConfig


#: The extraction stage's repository under its default policy. The policy values bear on
#: claim and finalize only; requeue and cancel never read them.
_EXTRACTION_REPOSITORY: WorkUnitRepository[Any] = WorkUnitRepository(
    EXTRACTION_DEFINITION,
    retry_max_attempts=EXTRACTION_RETRY_MAX_ATTEMPTS,
    retry_base_delay_seconds=EXTRACTION_RETRY_BASE_DELAY_SECONDS,
    stale_after_minutes=EXTRACTION_STALE_AFTER_MINUTES,
    timeout_seconds=EXTRACTION_TIMEOUT_SECONDS,
)


def apply_extraction_retry(session: "Session", job: Any, *, principal: str) -> None:
    """Re-queue a terminal extraction work-unit and record the ``requeued`` event.

    Clears the error and retry-wait fields, keeps ``attempts``, and names
    ``principal`` on the event. Raises ``ValueError`` for a non-requeueable
    status.
    """
    _EXTRACTION_REPOSITORY.requeue(session, job, reason="operator_retry", principal=principal)


def apply_extraction_cancel(session: "Session", job: Any) -> None:
    """Cancel an extraction work-unit and record the terminal transition.

    Clears any retry wait and keeps the error fields. Raises ``ValueError`` for
    a non-cancellable status.
    """
    _EXTRACTION_REPOSITORY.cancel(session, job, reason="operator_cancel")


def apply_extraction_readmission(
    session: "Session", job: Any, *, resolved: "ResolvedExtractionConfig", principal: str
) -> None:
    """Re-queue a finished extraction when its source is ``STALE`` or ``NEEDS_RECONCILIATION`` under ``resolved``.

    Any terminal status is eligible once the source's currentness admits it;
    ``attempts`` is kept and ``principal`` is named on the ``requeued`` event.
    The caller owns the transaction. Unclassified sources and other statuses
    raise ``ValueError``.
    """
    if not is_terminal(job.status):
        raise ValueError(f"cannot re-extract a work-unit in status {job.status.value!r}")
    classification = classify_extraction_source(session, job.source_id, resolved=resolved)
    if classification not in (Currentness.STALE, Currentness.NEEDS_RECONCILIATION):
        refused = classification.value if classification is not None else "not classified"
        raise ValueError(f"cannot re-extract a source that is {refused}")
    _EXTRACTION_REPOSITORY.requeue(
        session, job, reason="operator_readmission", principal=principal, from_statuses=TERMINAL_STATUSES
    )
