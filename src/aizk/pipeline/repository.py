"""The work-unit repository: one lifecycle over any stage's work-unit table.

:class:`WorkUnitRepository` owns claim, stale recovery, the terminal transition
with retry scheduling and the attempt cap, operator retry, and operator cancel
for a stage whose table carries :class:`~aizk.pipeline.lifecycle.WorkUnitColumns`.
The stage contributes its :class:`~aizk.pipeline.stage.StageDefinition` (table,
event kinds, payload classes) and its retry policy; the repository contributes
the rules, so two stages configured alike behave alike.

Transaction discipline: every method takes the caller's session, stages its
writes through :func:`~aizk.pipeline.events.record_transition`, and never
commits. The caller holds the exclusive write transaction
(:func:`aizk.db.engine.begin_immediate`); the repository issues no
dialect-specific SQL, so a backend that replaces that primitive needs no change
here.

Two guards close races the per-stage copies of this code carried: a finalize
writes only while the row is still ``running`` (a late outcome against a row
an operator cancelled or retried, a stale sweep reclaimed, or an earlier
finalize already settled logs and no-ops), and a finalize whose attempt token
differs from the row's ``attempts`` no-ops, so a unit another claim took cannot
be finalized by the execution that lost it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
import datetime
import logging
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import select

from aizk.pipeline.events import record_transition
from aizk.pipeline.lifecycle import WORKER_DRAIN_TERMINATED, RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.utilities.clock import utcnow

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.pipeline.stage import StageDefinition

logger = logging.getLogger(__name__)

#: Terminal statuses an operator may re-queue a work-unit from.
RETRYABLE_FROM = frozenset({WorkUnitStatus.FAILED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT})
#: Statuses an operator may cancel a work-unit from.
CANCELLABLE_FROM = frozenset({WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING, WorkUnitStatus.FAILED})

#: The error code stale recovery writes on a unit it reclaims from ``running``.
STALE_RUNNING_ERROR_CODE = "worker_stale_running"

#: Error messages for the failures the runtime causes, keyed by their code.
_RUNTIME_ERROR_MESSAGES = {WORKER_DRAIN_TERMINATED: "terminated by the worker drain timeout"}


class WorkUnitModel(Protocol):
    """The columns the repository reads and writes on a stage's work-unit table.

    A stage's table satisfies this by inheriting
    :class:`~aizk.pipeline.lifecycle.WorkUnitColumns` and declaring its own
    ``id``, ``idempotency_key``, and ``source_id``.
    """

    id: int | None
    idempotency_key: str
    source_id: UUID
    status: WorkUnitStatus
    attempts: int
    error_code: str | None
    error_message: str | None
    earliest_next_attempt_at: datetime.datetime | None
    last_error_at: datetime.datetime | None
    queued_at: datetime.datetime | None
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None
    updated_at: datetime.datetime


Model = TypeVar("Model", bound=WorkUnitModel)

#: A payload class, or a factory taking the work-unit row before the fields.
PayloadFactory = Callable[..., "BaseModel"]

#: Runs inside the claim transaction after the claim is staged.
ClaimHook = Callable[["Session", Any], None]
#: Runs inside the transaction of every transition out of ``running``.
TransitionHook = Callable[["Session", Any, WorkUnitStatus], None]


class IneligibleTransitionError(ValueError):
    """An operator retry or cancel refused because of the unit's status.

    ``code`` is the stable refusal code (``job_not_retryable`` or
    ``job_not_cancellable``), ``from_status`` the status the unit was in, and
    the message the text an operator surface returns for the refusal.
    """

    def __init__(self, code: str, *, status: WorkUnitStatus) -> None:
        """Build the refusal for ``code`` against the unit's current ``status``."""
        verb = "retry" if code == "job_not_retryable" else "cancel"
        super().__init__(f"cannot {verb} a work-unit in status {WorkUnitStatus(status).value!r}")
        self.code = code
        self.from_status = WorkUnitStatus(status)


def find_by_key(session: "Session", model: Any, key: str) -> Any:
    """Return the work-unit of ``model`` carrying ``idempotency_key == key``, or ``None``."""
    return session.exec(select(model).where(model.idempotency_key == key)).one_or_none()


def build_payload(factory: PayloadFactory, row: Any, **fields: Any) -> "BaseModel":
    """Build an event payload: a payload class takes ``fields``; a factory takes ``row`` and ``fields``."""
    if isinstance(factory, type) and issubclass(factory, BaseModel):
        return factory(**fields)
    return factory(row, **fields)


class WorkUnitRepository(Generic[Model]):
    """Claim, recover, finalize, requeue, and cancel one stage's work-units.

    The retry policy is the stage's: ``retry_max_attempts`` caps the attempts a
    retryable failure may consume, ``retry_base_delay_seconds`` is the backoff
    base (the wait is ``base * 2**attempts``), ``stale_after_minutes`` is the
    age past which a ``running`` unit is reclaimed, and ``timeout_seconds`` is
    recorded on a timed-out unit's event. Every timestamp written comes from
    ``clock``.
    """

    def __init__(
        self,
        definition: "StageDefinition[Model, Any]",
        *,
        retry_max_attempts: int,
        retry_base_delay_seconds: float,
        stale_after_minutes: float,
        timeout_seconds: float,
        clock: Callable[[], datetime.datetime] = utcnow,
        on_claim: ClaimHook | None = None,
        on_transition: TransitionHook | None = None,
    ) -> None:
        """Bind the stage's definition, retry policy, clock, and in-transaction hooks."""
        self._definition = definition
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._stale_after_minutes = stale_after_minutes
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._on_claim = on_claim
        self._on_transition = on_transition

    @property
    def definition(self) -> "StageDefinition[Model, Any]":
        """The stage definition this repository operates over."""
        return self._definition

    # --- helpers ---------------------------------------------------------------

    def _extra(self, row: Any) -> dict[str, str]:
        """Return the correlation fields every log line from the repository carries."""
        return {"stage": self._definition.name, "work_unit_ref": str(row.id)}

    def _record(
        self,
        session: "Session",
        row: Any,
        *,
        to_status: WorkUnitStatus,
        kind: Any,
        payload: "BaseModel",
        run_id: int | None = None,
    ) -> None:
        """Stage the status change and its event for ``row`` in the caller's session."""
        record_transition(
            session,
            row,
            stage=self._definition.name,
            work_unit_ref=str(row.id),
            source_id=row.source_id,
            to_status=to_status,
            kind=kind,
            attempt=row.attempts,
            run_id=run_id,
            payload=payload,
        )

    def _kind(self, name: str) -> Any:
        """Return the stage's event kind member named ``name``."""
        return self._definition.event_kinds[name]

    def _transition(self, session: "Session", row: Any, to_status: WorkUnitStatus) -> None:
        """Run the stage's transition hook, if any, after a transition out of ``running``."""
        if self._on_transition is not None:
            self._on_transition(session, row, to_status)

    # --- lookups ---------------------------------------------------------------

    def find_by_key(self, session: "Session", key: str) -> Model | None:
        """Return the work-unit carrying ``idempotency_key == key``, or ``None``."""
        return find_by_key(session, self._definition.work_unit_model, key)

    # --- runner-driven transitions --------------------------------------------

    def claim_next(self, session: "Session", *, worker_pid: int | None, holding: Collection[int] = ()) -> Model | None:
        """Claim the oldest eligible work-unit and return it, or ``None``.

        Eligible is ``queued``, or ``failed`` with a retry wait that is set and
        has elapsed; the oldest by ``queued_at`` wins, and the lowest id among
        units queued at the same instant. Units whose ids are in
        ``holding`` (the ones the calling process still executes) are never
        claimed, so a process cannot take a unit it already holds. The claim
        sets ``started_at``, increments ``attempts`` (the only transition that
        does), records the ``claimed`` event, and runs the claim hook, all in
        the caller's transaction. Does not commit.
        """
        now = self._clock()
        model: Any = self._definition.work_unit_model
        query = select(model).where(
            or_(
                model.status == WorkUnitStatus.QUEUED,
                (model.status == WorkUnitStatus.FAILED)
                & model.earliest_next_attempt_at.is_not(None)
                & (model.earliest_next_attempt_at <= now),
            )
        )
        if holding:
            query = query.where(model.id.not_in(list(holding)))
        row = session.exec(query.order_by(model.queued_at, model.id)).first()
        if row is None:
            return None

        row.started_at = now
        row.attempts += 1
        row.updated_at = now
        self._record(
            session,
            row,
            to_status=WorkUnitStatus.RUNNING,
            kind=self._kind("CLAIMED"),
            payload=build_payload(self._definition.claimed_payload, row, claimed_at=now, worker_pid=worker_pid),
        )
        if self._on_claim is not None:
            self._on_claim(session, row)
        return row

    def recover_stale(self, session: "Session", *, holding: Collection[int] = ()) -> list[Model]:
        """Reclaim ``running`` units older than the stale window; return them.

        Each becomes ``failed`` with the stale error fields and a
        ``recovered_stale`` event: below the attempt cap with an immediate
        retry wait, so the next claim re-runs it; at the cap with no wait and a
        finished time, so a unit that keeps crashing its worker terminates.
        ``attempts`` is untouched (a recovery is not an attempt). Units whose
        ids are in ``holding`` (the ones the calling process still executes)
        are left alone: they belong to the wall-clock timeout, the drain, or
        another process's sweep. Runs the transition hook per unit; does not
        commit.
        """
        now = self._clock()
        stale_before = now - datetime.timedelta(minutes=self._stale_after_minutes)
        model: Any = self._definition.work_unit_model
        query = (
            select(model)
            .where(model.status == WorkUnitStatus.RUNNING)
            .where(model.started_at.is_not(None))
            .where(model.started_at < stale_before)
        )
        if holding:
            query = query.where(model.id.not_in(list(holding)))
        rows = session.exec(query).all()

        recovered: list[Model] = []
        for row in rows:
            last_started_at = row.started_at
            row.error_code = STALE_RUNNING_ERROR_CODE
            row.error_message = f"reset after {self._stale_after_minutes} minutes in RUNNING"
            row.last_error_at = now
            row.updated_at = now
            if row.attempts >= self._retry_max_attempts:
                row.earliest_next_attempt_at = None
                row.finished_at = now
            else:
                row.earliest_next_attempt_at = now
            self._record(
                session,
                row,
                to_status=WorkUnitStatus.FAILED,
                kind=self._kind("RECOVERED_STALE"),
                payload=build_payload(
                    self._definition.recovered_stale_payload,
                    row,
                    stale_after_minutes=self._stale_after_minutes,
                    last_started_at=last_started_at,
                ),
            )
            self._transition(session, row, WorkUnitStatus.FAILED)
            recovered.append(row)
        return recovered

    def finalize(
        self,
        session: "Session",
        unit_id: int,
        attempt: int | None,
        outcome: TerminalOutcome,
        *,
        error: tuple[str, str] | None = None,
        succeeded: "tuple[Any, BaseModel, int | None] | None" = None,
    ) -> bool:
        """Write the terminal transition for ``unit_id``; return whether it was written.

        The transition is written only while the row is still ``running``: a
        row a stale sweep reclaimed, an operator cancelled or retried, or an
        earlier finalize already settled no longer belongs to this execution.
        ``attempt`` is the attempt number the claim wrote for the finalizing
        handle; when it is not ``None`` and differs from the row's ``attempts``,
        another claim has taken the unit. Both cases log a warning and return
        ``False`` without writing.

        ``succeeded`` is the stage's terminal event ``(kind, payload, run_id)``
        for a ``SUCCEEDED`` outcome. ``error`` is the stage's ``(error_code,
        error_message)`` pair for a stage-classified ``FAILED`` outcome; a
        runtime-caused failure (``outcome.error_code`` set) ignores it. Either
        failure is retryable while ``attempts`` is below the cap and permanent
        otherwise; a stage-classified failure waits ``base * 2**attempts``, a
        runtime-caused one is eligible at once. Runs the transition hook after
        every write; does not commit.

        Raises:
            ValueError: If a ``SUCCEEDED`` outcome supplies no ``succeeded``
                event, or a stage-classified ``FAILED`` outcome no ``error``.
        """
        model: Any = self._definition.work_unit_model
        row = session.get(model, unit_id)
        if row is None:
            logger.warning(
                "Finalize skipped: work-unit is missing",
                extra={"stage": self._definition.name, "work_unit_ref": str(unit_id)},
            )
            return False
        current = WorkUnitStatus(row.status)
        if current is not WorkUnitStatus.RUNNING:
            logger.warning("Finalize skipped: work-unit is %s, not running", current.value, extra=self._extra(row))
            return False
        if attempt is not None and row.attempts != attempt:
            logger.warning(
                "Finalize skipped: work-unit is on attempt %d, not the finalizing attempt %d",
                row.attempts,
                attempt,
                extra=self._extra(row),
            )
            return False

        now = self._clock()
        status = outcome.status
        if status is WorkUnitStatus.SUCCEEDED:
            if succeeded is None:
                raise ValueError("a succeeded outcome needs the stage's terminal event")
            kind, payload, run_id = succeeded
            row.finished_at = now
            row.updated_at = now
            row.error_code = None
            row.error_message = None
            self._record(session, row, to_status=status, kind=kind, payload=payload, run_id=run_id)
        elif status is WorkUnitStatus.CANCELLED:
            row.finished_at = now
            row.updated_at = now
            self._record(
                session,
                row,
                to_status=status,
                kind=self._kind("CANCELLED"),
                payload=build_payload(self._definition.cancelled_payload, row, cancellation_reason="runner_cancel"),
            )
        elif status is WorkUnitStatus.TIMED_OUT:
            row.finished_at = now
            row.updated_at = now
            self._record(
                session,
                row,
                to_status=status,
                kind=self._kind("TIMED_OUT"),
                payload=build_payload(self._definition.timed_out_payload, row, timeout_seconds=self._timeout_seconds),
            )
        else:
            self._finalize_failed(session, row, now, outcome, error)
        self._transition(session, row, status)
        return True

    def _finalize_failed(
        self,
        session: "Session",
        row: Any,
        now: datetime.datetime,
        outcome: TerminalOutcome,
        error: tuple[str, str] | None,
    ) -> None:
        """Stage the ``failed`` transition: schedule a retry or make the failure permanent."""
        if outcome.error_code is not None:
            error_code = outcome.error_code
            error_message = _RUNTIME_ERROR_MESSAGES.get(error_code, f"terminated by the runtime: {error_code}")
            retryable = row.attempts < self._retry_max_attempts
            if retryable:
                row.earliest_next_attempt_at = now
            else:
                row.earliest_next_attempt_at = None
                row.finished_at = now
        else:
            if error is None:
                raise ValueError("a stage-classified failed outcome needs the stage's error pair")
            error_code, error_message = error
            retryable = outcome.retry_class is RetryClass.RETRYABLE and row.attempts < self._retry_max_attempts
            if retryable:
                delay = self._retry_base_delay_seconds * (2**row.attempts)
                row.earliest_next_attempt_at = now + datetime.timedelta(seconds=delay)
            else:
                row.earliest_next_attempt_at = None
                row.finished_at = now
        row.error_code = error_code
        row.error_message = error_message
        row.last_error_at = now
        row.updated_at = now
        self._record(
            session,
            row,
            to_status=WorkUnitStatus.FAILED,
            kind=self._kind("FAILED"),
            payload=build_payload(
                self._definition.failed_payload,
                row,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
            ),
        )

    # --- operator transitions ---------------------------------------------------

    def requeue(
        self,
        session: "Session",
        row: Model,
        *,
        reason: str,
        principal: str,
        from_statuses: frozenset[WorkUnitStatus] = RETRYABLE_FROM,
    ) -> None:
        """Return ``row`` to ``queued`` for another attempt, keeping its attempt count.

        Clears the retry wait, the error fields, and the finished time; sets
        ``queued_at``; leaves ``attempts`` and ``started_at`` untouched (the
        next claim increments and overwrites them); records one ``requeued``
        event carrying ``reason`` and ``principal``. Does not commit.

        Raises:
            IneligibleTransitionError: ``job_not_retryable`` when ``row.status``
                is not in ``from_statuses``.
        """
        status = WorkUnitStatus(row.status)
        if status not in from_statuses:
            raise IneligibleTransitionError("job_not_retryable", status=status)
        now = self._clock()
        row.error_code = None
        row.error_message = None
        row.earliest_next_attempt_at = None
        row.finished_at = None
        row.last_error_at = None
        row.queued_at = now
        row.updated_at = now
        self._record(
            session,
            row,
            to_status=WorkUnitStatus.QUEUED,
            kind=self._kind("REQUEUED"),
            payload=build_payload(self._definition.requeued_payload, row, requeue_reason=reason, principal=principal),
        )

    def cancel(
        self,
        session: "Session",
        row: Model,
        *,
        reason: str | None = "operator_cancel",
        cancellable: Callable[[Model], bool] | None = None,
        principal: str | None = None,
    ) -> None:
        """Cancel ``row`` at an operator's request, keeping its error fields.

        Allowed from ``queued``, ``failed`` (either retry class), and
        ``running`` unless the stage's ``cancellable(row)`` says the unit is in
        a part of ``running`` it declares non-cancellable. Clears the retry
        wait, sets ``finished_at``, keeps ``error_code`` / ``error_message`` /
        ``last_error_at``, records one ``cancelled`` event with ``reason`` (and
        ``principal`` when given), and runs the transition hook when the unit
        was ``running``. Does not commit.

        Raises:
            IneligibleTransitionError: ``job_not_cancellable`` when the status
                refuses it or the stage's narrowing does.
        """
        status = WorkUnitStatus(row.status)
        refused_by_stage = status is WorkUnitStatus.RUNNING and cancellable is not None and not cancellable(row)
        if status not in CANCELLABLE_FROM or refused_by_stage:
            raise IneligibleTransitionError("job_not_cancellable", status=status)
        now = self._clock()
        row.earliest_next_attempt_at = None
        row.finished_at = now
        row.updated_at = now
        fields: dict[str, Any] = {"cancellation_reason": reason}
        if principal is not None:
            fields["principal"] = principal
        self._record(
            session,
            row,
            to_status=WorkUnitStatus.CANCELLED,
            kind=self._kind("CANCELLED"),
            payload=build_payload(self._definition.cancelled_payload, row, **fields),
        )
        if status is WorkUnitStatus.RUNNING:
            self._transition(session, row, WorkUnitStatus.CANCELLED)


__all__ = [
    "CANCELLABLE_FROM",
    "RETRYABLE_FROM",
    "STALE_RUNNING_ERROR_CODE",
    "ClaimHook",
    "IneligibleTransitionError",
    "Model",
    "PayloadFactory",
    "TransitionHook",
    "WorkUnitModel",
    "WorkUnitRepository",
    "build_payload",
    "find_by_key",
]
