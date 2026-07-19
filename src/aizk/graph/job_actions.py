"""Operator retry/cancel transitions for graph work-units, as domain helpers.

These lift the retry and cancel transitions out of the JSON API route modules so
every operator surface — the graph JSON API and the operator console — calls the
same domain code rather than importing another module's route internals (mirroring
:mod:`aizk.conversion.job_actions`). Contextualization and extraction share one
transition body, parameterized only by their ``pipeline_events`` stage and their
per-stage event kind/payload types; :func:`_build_transitions` closes over those
differences and returns the stage's ``(apply_retry, apply_cancel)`` pair.

Each helper performs the status-eligibility check (raising :class:`ValueError` with
an operator-facing reason when ineligible), applies the field mutations, and
co-commits the matching lifecycle event via
:func:`aizk.pipeline.events.record_transition`. The caller owns the surrounding
``BEGIN IMMEDIATE`` transaction; these helpers do **not** commit.
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
from aizk.pipeline.events import record_transition
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlmodel import Session

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
    """Return a graph stage's ``(apply_retry, apply_cancel)`` transition helpers.

    The two closures share the retry/cancel body across stages; only ``stage`` (the
    shared ``pipeline_events`` stage), the event ``kind`` enums, and the typed event
    payload classes vary per stage.
    """

    def apply_retry(session: "Session", job: Any) -> None:
        """Re-queue a terminal work-unit, clearing error/retry-wait fields and recording the event.

        Raises :class:`ValueError` when the unit is not in a re-queueable terminal
        status (the message is the operator-facing reason).
        """
        if job.status not in RETRYABLE_FROM:
            raise ValueError(f"cannot retry a work-unit in status {job.status.value!r}")
        now = _utcnow()
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
        """Cancel a work-unit, writing a terminal ``CANCELLED`` status and recording the event.

        Raises :class:`ValueError` when the unit is not in a cancellable status (the
        message is the operator-facing reason).
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
