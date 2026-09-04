"""Apply operator retry, cancel, and extraction-readmission transitions.

The helpers validate status eligibility, mutate the work-unit, and stage the
matching lifecycle event. The caller owns the surrounding transaction; these
helpers do not commit.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from aizk.graph.events import (
    CONTEXTUALIZATION_STAGE,
    CancelledPayload as ContextCancelledPayload,
    GraphEventKind,
    RequeuedPayload as ContextRequeuedPayload,
)
from aizk.graph.extraction_events import (
    EXTRACTION_STAGE,
    CancelledPayload as ExtractionCancelledPayload,
    ExtractionEventKind,
    RequeuedPayload as ExtractionRequeuedPayload,
)
from aizk.graph.extraction_run import Currentness, classify_extraction_source
from aizk.pipeline.events import record_transition
from aizk.pipeline.lifecycle import WorkUnitStatus, is_terminal

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlmodel import Session

    from aizk.graph.mention_store import ResolvedExtractionConfig

#: Terminal statuses an operator may re-queue a work-unit from.
RETRYABLE_FROM = frozenset({WorkUnitStatus.FAILED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT})
#: Statuses an operator may cancel a work-unit from.
CANCELLABLE_FROM = frozenset({WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING, WorkUnitStatus.FAILED})


def _utcnow() -> dt.datetime:
    """Return a timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


def _build_transitions(
    *,
    stage: str,
    requeued_kind: Any,
    cancelled_kind: Any,
    requeued_payload_cls: Any,
    cancelled_payload_cls: Any,
) -> "tuple[Callable[[Session, Any], None], Callable[[Session, Any], None]]":
    """Return retry and cancel helpers for a graph stage.

    The helpers record stage-specific transition events.
    """

    def apply_retry(session: "Session", job: Any) -> None:
        """Re-queue a terminal work-unit and record the transition.

        Resets ``attempts`` and clears error and retry-wait fields. Raises ``ValueError``
        for a non-requeueable status.
        """
        if job.status not in RETRYABLE_FROM:
            raise ValueError(f"cannot retry a work-unit in status {job.status.value!r}")
        now = _utcnow()
        job.attempts = 0
        job.error_code = None
        job.error_message = None
        job.earliest_next_attempt_at = None
        job.finished_at = None
        job.queued_at = now
        job.updated_at = now
        record_transition(
            session,
            job,
            stage=stage,
            work_unit_ref=str(job.id),
            source_id=job.source_id,
            to_status=WorkUnitStatus.QUEUED,
            kind=requeued_kind,
            attempt=job.attempts,
            payload=requeued_payload_cls(requeue_reason="operator_retry"),
        )

    def apply_cancel(session: "Session", job: Any) -> None:
        """Cancel a work-unit and record the terminal transition.

        Raises ``ValueError`` for a non-cancellable status.
        """
        if job.status not in CANCELLABLE_FROM:
            raise ValueError(f"cannot cancel a work-unit in status {job.status.value!r}")
        now = _utcnow()
        job.finished_at = now
        job.updated_at = now
        record_transition(
            session,
            job,
            stage=stage,
            work_unit_ref=str(job.id),
            source_id=job.source_id,
            to_status=WorkUnitStatus.CANCELLED,
            kind=cancelled_kind,
            attempt=job.attempts,
            payload=cancelled_payload_cls(cancellation_reason="operator_cancel"),
        )

    return apply_retry, apply_cancel


apply_contextualization_retry, apply_contextualization_cancel = _build_transitions(
    stage=CONTEXTUALIZATION_STAGE,
    requeued_kind=GraphEventKind.REQUEUED,
    cancelled_kind=GraphEventKind.CANCELLED,
    requeued_payload_cls=ContextRequeuedPayload,
    cancelled_payload_cls=ContextCancelledPayload,
)

apply_extraction_retry, apply_extraction_cancel = _build_transitions(
    stage=EXTRACTION_STAGE,
    requeued_kind=ExtractionEventKind.REQUEUED,
    cancelled_kind=ExtractionEventKind.CANCELLED,
    requeued_payload_cls=ExtractionRequeuedPayload,
    cancelled_payload_cls=ExtractionCancelledPayload,
)


def apply_extraction_readmission(session: "Session", job: Any, *, resolved: "ResolvedExtractionConfig") -> None:
    """Re-queue a finished extraction when its source is ``STALE`` or ``NEEDS_RECONCILIATION`` under ``resolved``.

    The caller owns the transaction. Unclassified sources and other statuses raise
    ``ValueError``.
    """
    if not is_terminal(job.status):
        raise ValueError(f"cannot re-extract a work-unit in status {job.status.value!r}")
    classification = classify_extraction_source(session, job.source_id, resolved=resolved)
    if classification not in (Currentness.STALE, Currentness.NEEDS_RECONCILIATION):
        refused = classification.value if classification is not None else "not classified"
        raise ValueError(f"cannot re-extract a source that is {refused}")
    now = _utcnow()
    job.attempts = 0
    job.error_code = None
    job.error_message = None
    job.earliest_next_attempt_at = None
    job.started_at = None
    job.finished_at = None
    job.queued_at = now
    job.updated_at = now
    record_transition(
        session,
        job,
        stage=EXTRACTION_STAGE,
        work_unit_ref=str(job.id),
        source_id=job.source_id,
        to_status=WorkUnitStatus.QUEUED,
        kind=ExtractionEventKind.REQUEUED,
        attempt=job.attempts,
        payload=ExtractionRequeuedPayload(requeue_reason="operator_readmission"),
    )
