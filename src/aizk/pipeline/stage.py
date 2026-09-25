"""The stage kit: a static stage definition, the shared enqueue, and the handler base.

:class:`StageDefinition` is the one static object a stage publishes about
itself: its name, the shape of one unit's parameters, the key derivation, its
work-unit table, its event kinds and shared payload classes, and optionally its
pending-work query. It holds metadata and store-side callables only and is
resolvable without a worker, a model client, or a database, so routes, the
console, and command-line tools can name a stage without starting it.

:func:`enqueue` creates or reuses a unit through the definition;
:class:`HandlerBase` implements :class:`~aizk.pipeline.handler.StageHandler`
over the :class:`~aizk.pipeline.repository.WorkUnitRepository`, so a stage
supplies only its dependencies, its unit of work, its outcome mapping, its
success event, and any narrowing it declares. :func:`require_stale_threshold`
is the startup check that keeps stale recovery from reclaiming a unit a live
runner still holds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import datetime
from enum import Enum
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel
from sqlmodel import Session

from aizk.pipeline.capacity import check_capacity
from aizk.pipeline.events import CreationOrigin, record_transition
from aizk.pipeline.handler import Isolation, StageResult
from aizk.pipeline.lifecycle import TerminalOutcome, WorkUnitStatus
from aizk.pipeline.repository import Model, PayloadFactory, WorkUnitRepository, build_payload, find_by_key
from aizk.utilities.clock import utcnow
from aizk.utilities.errors import StartupValidationError

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

Params = TypeVar("Params", bound=BaseModel)


@dataclass(frozen=True)
class StageDefinition(Generic[Model, Params]):
    """What a stage publishes about itself, with no behavior attached.

    Attributes:
        name: The stage string stamped on events, runs, and log lines.
        params: The pydantic model of one unit's stage-specific inputs; its
            field names are the work-unit table's column names, so a unit is
            constructed from ``params.model_dump()``.
        unit_key: Derives a unit's idempotency key from its parameters.
        work_unit_model: The stage's work-unit table, carrying
            :class:`~aizk.pipeline.lifecycle.WorkUnitColumns`.
        event_kinds: The stage's event kind enum. The repository looks up the
            members named ``QUEUED``, ``CLAIMED``, ``RECOVERED_STALE``,
            ``FAILED``, ``CANCELLED``, ``TIMED_OUT``, and ``REQUEUED`` by name.
            Each ``*_payload`` below is a payload class, called with the named
            fields, or a factory, called with the work-unit row followed by
            the same fields.
        queued_payload: Payload class of the creation event (``origin``,
            ``principal``).
        claimed_payload: Payload class of the claim event (``claimed_at``,
            ``worker_pid``).
        recovered_stale_payload: Payload class of the stale-recovery event
            (``stale_after_minutes``, ``last_started_at``).
        failed_payload: Payload class of the failure event (``error_code``,
            ``error_message``, ``retryable``).
        cancelled_payload: Payload class of the cancel event
            (``cancellation_reason``, and ``principal`` when the canceller
            names one).
        timed_out_payload: Payload class of the timeout event
            (``timeout_seconds``).
        requeued_payload: Payload class of the operator-retry event
            (``requeue_reason``, ``principal``).
        pending: The admission query returning the parameters of units the
            stage still owes, up to a limit, or ``None`` when the stage
            declares none.
        find_existing: The lookup ``(session, key, principal)`` returning the
            unit an enqueue reuses, or ``None`` to reuse any unit carrying the
            key.
    """

    name: str
    params: type[Params]
    unit_key: Callable[[Params], str]
    work_unit_model: type[Model]
    event_kinds: type[Enum]
    queued_payload: PayloadFactory
    claimed_payload: PayloadFactory
    recovered_stale_payload: PayloadFactory
    failed_payload: PayloadFactory
    cancelled_payload: PayloadFactory
    timed_out_payload: PayloadFactory
    requeued_payload: PayloadFactory
    pending: Callable[[Session, int | None], list[Params]] | None = None
    find_existing: Callable[[Session, str, str], Model | None] | None = None


def enqueue(
    session: Session,
    definition: StageDefinition[Model, Params],
    params: Params,
    *,
    origin: CreationOrigin,
    principal: str,
    queue_max_depth: int,
    clock: Callable[[], datetime.datetime] = utcnow,
    columns: Mapping[str, Any] | None = None,
    zero_limit_closes: bool = False,
) -> tuple[Model, bool]:
    """Create or reuse the work-unit for ``params`` through the stage's definition.

    A unit the definition's lookup finds for the derived key is returned
    unchanged with ``created=False`` and no capacity check. Otherwise the
    capacity check runs (a non-positive ``queue_max_depth`` is unlimited, or a
    closed queue when ``zero_limit_closes`` is set), a ``queued`` unit is
    staged from ``params`` and the stage's own ``columns`` and flushed with
    ``queued_at`` read from ``clock``, and its creation event is recorded with
    ``origin`` and ``principal``. Stages rows in the caller's transaction; does
    not commit.

    Returns:
        The existing or new unit and whether this call created it.

    Raises:
        StageAtCapacityError: If a new unit would exceed ``queue_max_depth``.
    """
    key = definition.unit_key(params)
    if definition.find_existing is not None:
        existing = definition.find_existing(session, key, principal)
    else:
        existing = find_by_key(session, definition.work_unit_model, key)
    if existing is not None:
        return existing, False

    check_capacity(
        session,
        definition.work_unit_model,
        stage=definition.name,
        limit=queue_max_depth,
        zero_limit_closes=zero_limit_closes,
    )

    unit = definition.work_unit_model(
        idempotency_key=key,
        status=WorkUnitStatus.QUEUED,
        queued_at=clock(),
        **params.model_dump(),
        **(columns or {}),
    )
    session.add(unit)
    session.flush()
    record_transition(
        session,
        unit,
        stage=definition.name,
        work_unit_ref=str(unit.id),
        source_id=unit.source_id,
        to_status=WorkUnitStatus.QUEUED,
        kind=definition.event_kinds["QUEUED"],
        attempt=0,
        payload=build_payload(definition.queued_payload, unit, origin=origin, principal=principal),
        from_status=None,
    )
    return unit, True


def require_stale_threshold(
    *,
    stale_after: datetime.timedelta,
    timeout: datetime.timedelta,
    drain_timeout: float,
    cancel_grace: float,
) -> None:
    """Refuse a stale window a live runner could still be inside.

    A unit is reclaimed by stale recovery once it has been ``running`` for
    ``stale_after``; a healthy runner may hold it for the wall-clock
    ``timeout``, then the drain timeout, then the cancellation allowance. The
    stale window must exceed that sum, or a unit could be reclaimed and
    claimed again while its first execution is still spending.

    Raises:
        StartupValidationError: If ``stale_after`` does not exceed ``timeout``
            plus ``drain_timeout`` plus ``cancel_grace``.
    """
    stale_seconds = stale_after.total_seconds()
    timeout_seconds = timeout.total_seconds()
    if stale_seconds <= timeout_seconds + drain_timeout + cancel_grace:
        raise StartupValidationError(
            f"stale threshold of {stale_seconds} s must exceed the wall-clock timeout of {timeout_seconds} s "
            f"plus the drain timeout of {drain_timeout} s plus the cancellation allowance of {cancel_grace} s"
        )


class HandlerBase(Generic[Model]):
    """A :class:`~aizk.pipeline.handler.StageHandler` over the work-unit repository.

    The base owns everything the lifecycle needs from a handler: the stage
    name, the claim / stale-recovery / finalize transitions, the per-handle
    bridges between ``execute`` and ``finalize``, the cooperative cancel set,
    and the timing declarations. A subclass supplies :meth:`execute`,
    :meth:`map_result`, :meth:`succeeded_event`, and :attr:`default_error`,
    and may override :meth:`dependencies`, :meth:`cancellable`,
    :meth:`on_claim`, and :meth:`on_transition`.

    The work-unit handle is the integer row id. The attempt number the claim
    wrote is kept in a per-handle map: :meth:`claim_next` records it,
    :meth:`finalize` hands it to the repository as the attempt token, and
    :meth:`cleanup` releases it. A ``finalize`` for a handle with no recorded
    token (a call made outside the runner's claim) passes ``attempt=None`` and
    the repository skips that guard. A process never reclaims or re-claims a
    unit it still holds: :meth:`claim_next` and :meth:`recover_stale` exclude
    every handle in the map, so a held unit is left to the wall-clock timeout,
    the drain, or another process's sweep, and its token cannot be overwritten
    from inside the process. Another process's claim increments the row's
    ``attempts``; this process's ``finalize`` then presents the old token and
    writes nothing.

    ``_errors`` maps a handle to the scrubbed ``(error_code, error_message)``
    pair ``execute`` stashes for a failed unit; ``finalize`` reads it without
    clearing (the runner re-invokes ``finalize`` after a finalize-time database
    error) and ``cleanup`` releases it once the terminal transition committed.
    """

    #: The ``(error_code, error_message)`` pair recorded for a failure whose
    #: ``execute`` stashed no scrubbed pair.
    default_error: ClassVar[tuple[str, str]]

    def __init__(
        self,
        engine: "Engine",
        definition: StageDefinition[Model, Any],
        *,
        timeout_seconds: float,
        concurrency: int,
        stale_after_minutes: float,
        retry_base_delay_seconds: float,
        retry_max_attempts: int,
        clock: Callable[[], datetime.datetime] = utcnow,
    ) -> None:
        """Bind the engine and the definition; build the repository from the retry policy.

        Args:
            engine: The shared engine; ``execute`` and the durable cancel probe
                open their own short sessions on it.
            definition: The stage's static definition.
            timeout_seconds: Wall-clock timeout per unit (the runner enforces it).
            concurrency: Maximum units the runner may execute simultaneously.
            stale_after_minutes: Minutes in ``running`` before recovery reclaims
                a unit.
            retry_base_delay_seconds: Retry backoff base; the wait is
                ``base * 2**attempts``.
            retry_max_attempts: Attempt cap after which a retryable failure
                becomes permanent.
            clock: Source of every timestamp the lifecycle writes.
        """
        self._engine = engine
        self._definition = definition
        self._timeout_seconds = timeout_seconds
        self._concurrency = concurrency
        self._clock = clock
        self._repository: WorkUnitRepository[Model] = WorkUnitRepository(
            definition,
            retry_max_attempts=retry_max_attempts,
            retry_base_delay_seconds=retry_base_delay_seconds,
            stale_after_minutes=stale_after_minutes,
            timeout_seconds=timeout_seconds,
            clock=clock,
            on_claim=self.on_claim,
            on_transition=self.on_transition,
        )
        self._errors: dict[int, tuple[str, str]] = {}
        self._attempts: dict[int, int] = {}
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    # --- what the stage supplies ------------------------------------------------

    def dependencies(self) -> None:
        """Probe the stage's external dependencies; raise when one is missing."""
        return None

    def execute(self, handle: int) -> StageResult:
        """Run the stage's unit of work for ``handle``."""
        raise NotImplementedError

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map an execution result or exception to a terminal outcome."""
        raise NotImplementedError

    def succeeded_event(self, handle: int) -> "tuple[Any, BaseModel, int | None]":
        """Return the ``(kind, payload, run_id)`` of the success event for ``handle``."""
        raise NotImplementedError

    def cancellable(self, row: Model) -> bool:
        """Report whether a ``running`` unit is outside any part the stage declares non-cancellable."""
        return True

    def on_claim(self, session: Session, row: Model) -> None:
        """Write stage-owned columns in the claim transaction; no-op by default."""
        return None

    def on_transition(self, session: Session, row: Model, to_status: WorkUnitStatus) -> None:
        """Write stage-owned columns in the transaction that leaves ``running``; no-op by default."""
        return None

    # --- what the base owns -------------------------------------------------------

    @property
    def definition(self) -> StageDefinition[Model, Any]:
        """The stage's static definition."""
        return self._definition

    @property
    def repository(self) -> WorkUnitRepository[Model]:
        """The lifecycle repository the handler drives; operator surfaces reach it here."""
        return self._repository

    @property
    def clock(self) -> Callable[[], datetime.datetime]:
        """The source of every timestamp the lifecycle writes."""
        return self._clock

    @property
    def stage(self) -> str:
        """Return the stable stage name used in the correlation spine."""
        return self._definition.name

    def validate_dependencies(self) -> None:
        """Run the stage's dependency probe before any work is accepted."""
        self.dependencies()

    def scope_id(self, handle: int) -> str:
        """Return the run ``scope_id`` for ``handle``: the unit's ``source_id``.

        Returns the bare handle when the row is missing, so correlation logging
        never raises.
        """
        with Session(self._engine) as session:
            row = session.get(self._definition.work_unit_model, handle)
            return str(row.source_id) if row is not None else str(handle)

    @property
    def timeout(self) -> datetime.timedelta:
        """Return the wall-clock timeout after which a running unit is terminated."""
        return datetime.timedelta(seconds=self._timeout_seconds)

    @property
    def concurrency_limit(self) -> int:
        """Return the maximum number of units the runner may execute simultaneously."""
        return self._concurrency

    @property
    def isolation(self) -> Isolation:
        """Return the execution isolation; the base runs units in-process."""
        return Isolation.IN_PROCESS

    def claim_next(self, session: Session) -> int | None:
        """Claim the oldest eligible unit through the repository; return its id or ``None``.

        Never claims a unit this process still holds. Records the attempt
        number the claim wrote so :meth:`finalize` can present it as the
        attempt token. Runs inside the runner-owned transaction; does not
        commit.
        """
        row = self._repository.claim_next(session, worker_pid=os.getpid(), holding=self._held())
        if row is None:
            return None
        with self._lock:
            self._attempts[row.id] = row.attempts
        return row.id

    def recover_stale(self, session: Session) -> list[int]:
        """Reclaim stale ``running`` units this process does not hold; return their ids."""
        return [row.id for row in self._repository.recover_stale(session, holding=self._held())]

    def _held(self) -> list[int]:
        """Return the handles this process still holds a claim token for."""
        with self._lock:
            return list(self._attempts)

    def finalize(self, session: Session, handle: int, outcome: TerminalOutcome) -> bool:
        """Write the unit's terminal transition through the repository.

        Reads the per-handle bridges without clearing them: the runner retains
        the slot and re-invokes ``finalize`` on a finalize-time database
        error, and :meth:`cleanup` releases the bridges once the durable
        commit landed. Runs inside the runner-owned transaction; does not
        commit.

        Returns:
            Whether the terminal transition was written.
        """
        with self._lock:
            error = self._errors.get(handle, self.default_error)
            attempt = self._attempts.get(handle)
        succeeded = self.succeeded_event(handle) if outcome.status is WorkUnitStatus.SUCCEEDED else None
        return self._repository.finalize(session, handle, attempt, outcome, error=error, succeeded=succeeded)

    def cleanup(self, handle: int) -> None:
        """Release the unit's per-handle bridges and attempt token; called on every outcome."""
        with self._lock:
            self._errors.pop(handle, None)
            self._attempts.pop(handle, None)
            self._cancelled.discard(handle)

    def cancel(self, handle: int) -> None:
        """Record a cooperative cancellation request for ``handle``.

        The unit runs in-process and cannot be force-terminated; ``execute``
        honors the request at entry and wherever the unit of work polls
        :meth:`_is_cancelled`. The runner resolves the terminal status from its
        own slot state.
        """
        with self._lock:
            self._cancelled.add(handle)

    def _is_cancelled(self, handle: int) -> bool:
        """Return whether cancellation was requested for ``handle`` from either source.

        Consults the in-process request set (the runner's cooperative cancel and
        timeout enforcement) and then the durable status, which an operator
        cancel committed by another process sets. The status read runs on its
        own connection; under WAL it never blocks on an in-flight write.
        """
        with self._lock:
            if handle in self._cancelled:
                return True
        with Session(self._engine) as session:
            row = session.get(self._definition.work_unit_model, handle)
            return row is not None and WorkUnitStatus(row.status) is WorkUnitStatus.CANCELLED


__all__ = [
    "HandlerBase",
    "Params",
    "StageDefinition",
    "enqueue",
    "require_stale_threshold",
]
