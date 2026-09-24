"""Apply operator retry and cancel transitions to contextualization work-units."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aizk.contextualization.handler import (
    DEFAULT_RETRY_BASE_DELAY_SECONDS as CONTEXTUALIZATION_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_RETRY_MAX_ATTEMPTS as CONTEXTUALIZATION_RETRY_MAX_ATTEMPTS,
    DEFAULT_STALE_AFTER_MINUTES as CONTEXTUALIZATION_STALE_AFTER_MINUTES,
    DEFAULT_TIMEOUT_SECONDS as CONTEXTUALIZATION_TIMEOUT_SECONDS,
)
from aizk.contextualization.stage import CONTEXTUALIZATION_DEFINITION
from aizk.pipeline.repository import WorkUnitRepository

if TYPE_CHECKING:
    from sqlmodel import Session

#: The stage's repository under its default policy. The policy values bear on
#: claim and finalize only; requeue and cancel never read them.
_CONTEXTUALIZATION_REPOSITORY: WorkUnitRepository[Any] = WorkUnitRepository(
    CONTEXTUALIZATION_DEFINITION,
    retry_max_attempts=CONTEXTUALIZATION_RETRY_MAX_ATTEMPTS,
    retry_base_delay_seconds=CONTEXTUALIZATION_RETRY_BASE_DELAY_SECONDS,
    stale_after_minutes=CONTEXTUALIZATION_STALE_AFTER_MINUTES,
    timeout_seconds=CONTEXTUALIZATION_TIMEOUT_SECONDS,
)


def apply_contextualization_retry(session: "Session", job: Any, *, principal: str) -> None:
    """Re-queue a terminal contextualization work-unit and record the ``requeued`` event.

    Clears the error and retry-wait fields, keeps ``attempts``, and names
    ``principal`` on the event. Raises ``ValueError`` for a non-requeueable
    status.
    """
    _CONTEXTUALIZATION_REPOSITORY.requeue(session, job, reason="operator_retry", principal=principal)


def apply_contextualization_cancel(session: "Session", job: Any) -> None:
    """Cancel a contextualization work-unit and record the terminal transition.

    Clears any retry wait and keeps the error fields. Raises ``ValueError`` for
    a non-cancellable status.
    """
    _CONTEXTUALIZATION_REPOSITORY.cancel(session, job, reason="operator_cancel")
