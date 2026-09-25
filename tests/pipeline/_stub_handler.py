"""In-memory stub stage for runner and lifecycle tests.

This is **test support, not shipped code**. It is a stage built purely by
supplying an adapter: a work-unit table carrying the shared lifecycle columns,
a :class:`~aizk.pipeline.stage.StageDefinition`, and a
:class:`~aizk.pipeline.stage.HandlerBase` subclass that adds only its unit of
work, its outcome mapping, and its success event. Every claim and transition
runs through the real repository and :func:`aizk.pipeline.events.record_transition`
co-commit helper, so runner tests exercise the genuine "runner owns the session
+ ``BEGIN IMMEDIATE``, handler never commits" seam rather than a mock.

The stub is deliberately controllable per test:

- ``enqueue`` seeds eligible (or retry-delayed) work-units in submission order;
- each unit carries a ``behavior`` callable the stub runs in ``execute`` so a
  test can make a unit slow, block on an event, or raise a classified failure;
- a subprocess-isolated variant (:class:`SubprocessStubRepository`) spawns a
  real child process so the no-orphan-descendants guarantee is exercised.

The stub work-unit table lives on its own private ``MetaData`` (not
``SQLModel.metadata``) so it never pollutes the shared metadata that the
migration parity tests compare against ``create_all``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import datetime as dt
from enum import Enum
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import MetaData
from sqlmodel import Field, Session, SQLModel

from aizk.pipeline.events import CreationOrigin
from aizk.pipeline.handler import Isolation
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitColumns, WorkUnitStatus
from aizk.pipeline.stage import HandlerBase, StageDefinition, enqueue
from aizk.utilities.clock import utcnow

if TYPE_CHECKING:
    from sqlalchemy import Engine


class _StubBase(SQLModel):
    """Private base keeping the stub table off ``SQLModel.metadata``."""

    metadata = MetaData()


class StubWorkUnit(WorkUnitColumns, _StubBase, table=True):
    """A stage-owned work-unit row: the shared lifecycle columns plus the stub's own.

    ``label`` is the test's name for the unit; ``source_id`` is the cross-stage
    source identity every event carries.
    """

    __tablename__ = "_stub_work_units"

    id: int | None = Field(default=None, primary_key=True)
    idempotency_key: str = Field(nullable=False)
    source_id: UUID = Field(nullable=False)
    label: str = Field(nullable=False)


class StubParams(BaseModel):
    """The stub stage's unit parameters; the field names are the table's columns."""

    model_config = ConfigDict(frozen=True)
    source_id: UUID
    label: str


class StubEventKind(str, Enum):
    """Event kinds recorded for a stub work-unit's lifecycle."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RECOVERED_STALE = "recovered_stale"
    REQUEUED = "requeued"


class StubQueuedPayload(BaseModel):
    """Payload for the creation event."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["queued"] = "queued"
    origin: CreationOrigin
    principal: str


class StubClaimedPayload(BaseModel):
    """Payload for the claim transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["claimed"] = "claimed"
    claimed_at: dt.datetime
    worker_pid: int | None = None


class StubSucceededPayload(BaseModel):
    """Payload for a successful transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["succeeded"] = "succeeded"


class StubFailedPayload(BaseModel):
    """Payload for a failed transition, including retry eligibility."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["failed"] = "failed"
    error_code: str
    error_message: str
    retryable: bool


class StubCancelledPayload(BaseModel):
    """Payload for a cancelled transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["cancelled"] = "cancelled"
    cancellation_reason: str


class StubTimedOutPayload(BaseModel):
    """Payload for a timed-out transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["timed_out"] = "timed_out"
    timeout_seconds: float


class StubRecoveredStalePayload(BaseModel):
    """Payload for a stale ``running`` unit reclaimed by stale recovery."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["recovered_stale"] = "recovered_stale"
    stale_after_minutes: float
    last_started_at: dt.datetime | None = None


class StubRequeuedPayload(BaseModel):
    """Payload for an operator retry, carrying the reason and the acting principal."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["requeued"] = "requeued"
    requeue_reason: str
    principal: str


STUB_DEFINITION: StageDefinition[StubWorkUnit, StubParams] = StageDefinition(
    name="stub",
    params=StubParams,
    unit_key=lambda params: f"stub:{params.source_id}",
    work_unit_model=StubWorkUnit,
    event_kinds=StubEventKind,
    queued_payload=StubQueuedPayload,
    claimed_payload=StubClaimedPayload,
    recovered_stale_payload=StubRecoveredStalePayload,
    failed_payload=StubFailedPayload,
    cancelled_payload=StubCancelledPayload,
    timed_out_payload=StubTimedOutPayload,
    requeued_payload=StubRequeuedPayload,
)
"""The stub stage's static definition."""


# A unit's execute behavior: receives the unit label, returns an opaque result
# or raises. Tests supply these to drive success / slow / blocking / failing
# units without the stub needing to know each scenario.
Behavior = Callable[[str], Any]


def succeed(label: str) -> str:  # noqa: D103 - test helper
    return f"ok:{label}"


@dataclass
class _Recorded:
    """Per-instance observation buffers a test can assert against."""

    cleaned_up: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    execute_started: list[str] = field(default_factory=list)
    finalize_attempts: list[str] = field(default_factory=list)
    finalize_committed: list[str] = field(default_factory=list)


class StubStageHandler(HandlerBase[StubWorkUnit]):
    """The stub stage's handler: the base plus a per-unit test behavior.

    All claim/transition writes run inside the runner-owned transaction through
    the repository and never commit, matching the production contract.
    Execution behavior is per-unit and supplied by the test through
    :meth:`enqueue`.
    """

    default_error = ("stub_error", "unknown error")

    def __init__(
        self,
        engine: "Engine",
        *,
        timeout: dt.timedelta = dt.timedelta(seconds=30),
        concurrency_limit: int = 4,
        stale_after: dt.timedelta = dt.timedelta(minutes=5),
        retry_base_delay_seconds: float = 60.0,
        retry_max_attempts: int = 3,
        clock: Callable[[], dt.datetime] = utcnow,
        dependencies_ok: bool = True,
        isolation: Isolation = Isolation.IN_PROCESS,
        stage_name: str | None = None,
        cancellable_labels: set[str] | None = None,
    ) -> None:
        """Create a stub handler bound to ``engine``.

        Args:
            engine: SQLite engine holding the stub work-unit table.
            timeout: Wall-clock timeout the runner enforces per unit.
            concurrency_limit: Maximum simultaneously-executing units.
            stale_after: Age past which a ``running`` unit is treated as stale.
            retry_base_delay_seconds: Retry backoff base; the wait is
                ``base * 2**attempts``.
            retry_max_attempts: Attempt cap after which a retryable failure is
                permanent.
            clock: Source of every timestamp the lifecycle writes.
            dependencies_ok: When ``False``, ``validate_dependencies`` raises.
            isolation: In-process or subprocess execution model.
            stage_name: Optional override for the ``stage`` identifier so two
                stub repositories can share a process with distinct stages.
            cancellable_labels: When given, only ``running`` units with these
                labels are cancellable by an operator; the stage's declared
                narrowing of cancel.
        """
        definition = STUB_DEFINITION if stage_name is None else replace(STUB_DEFINITION, name=stage_name)
        super().__init__(
            engine,
            definition,
            timeout_seconds=timeout.total_seconds(),
            concurrency=concurrency_limit,
            stale_after_minutes=stale_after.total_seconds() / 60,
            retry_base_delay_seconds=retry_base_delay_seconds,
            retry_max_attempts=retry_max_attempts,
            clock=clock,
        )
        self._stale_after = stale_after
        self._dependencies_ok = dependencies_ok
        self._isolation = isolation
        self._cancellable_labels = cancellable_labels
        self._behaviors: dict[int, Behavior] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self.recorded = _Recorded()
        # Fault-injection counters (test support): how many leading calls to
        # finalize / claim_next should raise a simulated DB fault before the call
        # is allowed to proceed. Default 0 = no injected fault.
        self._finalize_faults_remaining = 0
        self._finalize_fault_error: BaseException | None = None
        self._claim_calls = 0
        self._claim_fault_on: set[int] = set()
        self._claim_fault_error: BaseException | None = None

    # --- test control surface -------------------------------------------------

    def enqueue(
        self,
        label: str,
        *,
        behavior: Behavior = succeed,
        earliest_next_attempt_at: dt.datetime | None = None,
        status: str = WorkUnitStatus.QUEUED.value,
        queued_at: dt.datetime | None = None,
        source_id: UUID | None = None,
    ) -> int:
        """Seed one work-unit through the shared enqueue and return its id.

        ``queued_at`` defaults to the handler's clock; units enqueued at the
        same instant are claimed in id order, which is submission order.
        ``status`` and ``earliest_next_attempt_at`` override the created
        unit's lifecycle fields so a test can seed durable terminal state
        directly. ``behavior`` is the callable ``execute`` runs for this unit.
        """
        params = StubParams(source_id=source_id or uuid4(), label=label)
        with Session(self._engine) as session:
            unit, _created = enqueue(
                session,
                self._definition,
                params,
                origin="intake",
                principal="stub",
                queue_max_depth=0,
                clock=self.clock,
            )
            unit.status = WorkUnitStatus(status)
            unit.earliest_next_attempt_at = earliest_next_attempt_at
            if queued_at is not None:
                unit.queued_at = queued_at
            session.add(unit)
            session.commit()
            unit_id = unit.id
        self._behaviors[unit_id] = behavior
        self._cancel_events[unit_id] = threading.Event()
        return unit_id

    def cancel_event(self, unit_id: int) -> threading.Event:
        """Return the cooperative-cancel event a unit's behavior may poll."""
        return self._cancel_events[unit_id]

    def register_behavior(self, unit_id: int, behavior: Behavior = succeed) -> None:
        """Attach an ``execute`` behavior to an *existing* persisted unit id.

        A fresh handler constructed over an existing store (simulating a
        process restart) has no per-unit behavior closures — those live only in
        the original instance's memory. Production ``execute`` is always available
        after a restart; this re-supplies a behavior so a reclaimed unit can run.
        """
        self._behaviors[unit_id] = behavior
        self._cancel_events.setdefault(unit_id, threading.Event())

    def inject_finalize_fault(self, *, times: int, error: BaseException) -> None:
        """Make the next ``times`` calls to :meth:`finalize` raise ``error``.

        Simulates a DB error / lock on the terminal-transition commit so a test
        can assert the runner keeps the slot and retries ``finalize`` on the
        next reap until the durable transition lands. After ``times`` faults the
        call proceeds normally. ``error`` should be one the runner classifies as
        a finalize DB error (``OperationalError`` / ``DBAPIError``).
        """
        self._finalize_faults_remaining = times
        self._finalize_fault_error = error

    def inject_claim_fault(self, *, on_calls: set[int], error: BaseException) -> None:
        """Raise ``error`` on the 1-indexed claim calls listed in ``on_calls``.

        Simulates a DB lock on specific claim transactions so a test can assert
        the runner tolerates a contended claim (logs and re-polls) without
        losing already-running work. Targeting a specific call (e.g. the 2nd,
        after a first unit is already claimed and in flight) keeps the contention
        distinct from a "no work" first poll. ``error`` should be an
        ``OperationalError`` / ``DBAPIError`` the runner classifies as a claim
        DB error.
        """
        self._claim_fault_on = set(on_calls)
        self._claim_fault_error = error

    def get_status(self, unit_id: int) -> str:
        """Return the unit's current persisted status text."""
        with Session(self._engine) as session:
            unit = session.get(StubWorkUnit, unit_id)
            assert unit is not None
            return WorkUnitStatus(unit.status).value

    def force_running_stale(self, unit_id: int) -> None:
        """Persist a unit as ``running`` with a backdated ``started_at``.

        Simulates a unit stranded by an interrupted runtime so stale recovery
        has something to reclaim.
        """
        with Session(self._engine) as session:
            unit = session.get(StubWorkUnit, unit_id)
            assert unit is not None
            unit.status = WorkUnitStatus.RUNNING
            unit.started_at = self._clock() - self._stale_after - dt.timedelta(seconds=1)
            session.add(unit)
            session.commit()

    # --- the stage's own contributions ---------------------------------------

    def dependencies(self) -> None:
        """Raise when configured to simulate a missing dependency."""
        if not self._dependencies_ok:
            raise RuntimeError("stub dependency unavailable")

    def cancellable(self, row: StubWorkUnit) -> bool:
        """Report whether a ``running`` unit is outside the stage's declared non-cancellable part."""
        return self._cancellable_labels is None or row.label in self._cancellable_labels

    def claim_next(self, session: Session) -> int | None:
        """Claim through the base, after honoring an injected claim fault.

        The fault (see :meth:`inject_claim_fault`) raises the configured DB
        error before the claim runs, simulating a contended claim lock.
        """
        self._claim_calls += 1
        if self._claim_calls in self._claim_fault_on:
            assert self._claim_fault_error is not None
            raise self._claim_fault_error
        return super().claim_next(session)

    def execute(self, handle: int) -> Any:
        """Run the unit's per-test behavior callable; stash a failure's code for the event."""
        with self._lock:
            self.recorded.execute_started.append(str(handle))
        try:
            result = self._behaviors[handle](str(handle))
        except Exception as exc:
            with self._lock:
                self._errors[handle] = (type(exc).__name__, "stub behavior raised")
            raise
        with self._lock:
            self.recorded.executed.append(str(handle))
        return result

    def map_result(self, result_or_exc: Any) -> TerminalOutcome:
        """Map a result or exception to a generic terminal outcome.

        Exceptions carry an optional ``retryable`` attribute (defaulting to
        retryable) so a test can drive both retryable and permanent failures.
        """
        if isinstance(result_or_exc, BaseException):
            retryable = bool(getattr(result_or_exc, "retryable", True))
            return TerminalOutcome(
                WorkUnitStatus.FAILED,
                RetryClass.RETRYABLE if retryable else RetryClass.PERMANENT,
            )
        return TerminalOutcome(WorkUnitStatus.SUCCEEDED)

    def succeeded_event(self, handle: int) -> tuple[Any, BaseModel, int | None]:
        """Return the stub's success event; it carries no run."""
        return StubEventKind.SUCCEEDED, StubSucceededPayload(), None

    def finalize(self, session: Session, handle: int, outcome: TerminalOutcome) -> bool:
        """Finalize through the base, after honoring an injected finalize fault.

        The fault (see :meth:`inject_finalize_fault`) raises the configured DB
        error *before* any write is staged, so the runner rolls back, keeps the
        slot, and retries on the next reap.
        """
        with self._lock:
            self.recorded.finalize_attempts.append(str(handle))
        if self._finalize_faults_remaining > 0:
            self._finalize_faults_remaining -= 1
            assert self._finalize_fault_error is not None
            raise self._finalize_fault_error
        written = super().finalize(session, handle, outcome)
        with self._lock:
            self.recorded.finalize_committed.append(str(handle))
        return written

    def cleanup(self, handle: int) -> None:
        """Record that transient resources were released for ``handle``."""
        super().cleanup(handle)
        with self._lock:
            self.recorded.cleaned_up.append(str(handle))

    def cancel(self, handle: int) -> None:
        """Cooperatively signal the unit to stop; record the request."""
        super().cancel(handle)
        with self._lock:
            self.recorded.cancelled.append(str(handle))
        event = self._cancel_events.get(handle)
        if event is not None:
            event.set()

    @property
    def isolation(self) -> Isolation:
        """Whether the unit-of-work runs in-process or in a subprocess."""
        return self._isolation


def _grandchild_sleeper() -> None:
    """A descendant process that sleeps forever — used to prove no-orphan."""
    time.sleep(600)


def _child_spawns_grandchild_and_sleeps(ready_path: str) -> None:
    """Subprocess entrypoint: become a process-group leader, spawn a grandchild, sleep.

    Writes the child and grandchild PIDs to ``ready_path`` so the test can
    assert both are reaped after termination. Creating a new process group
    (``setpgrp``) lets the runner terminate the whole descendant tree with a
    single ``killpg``, which is what the no-orphan guarantee relies on.
    """
    import multiprocessing as mp

    os.setpgrp()
    ctx = mp.get_context("fork")
    grandchild = ctx.Process(target=_grandchild_sleeper, daemon=False)
    grandchild.start()
    with open(ready_path, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()},{grandchild.pid}")
    time.sleep(600)


@dataclass
class _SubprocessHandle:
    """Opaque handle for a subprocess-isolated stub unit."""

    unit_id: int
    process: Any
    ready_path: str


class SubprocessStubRepository(StubStageHandler):
    """Subprocess-isolated stub: ``execute`` spawns a real child + grandchild.

    Exercises the runner's graceful-before-forceful termination and the
    no-orphan-descendants guarantee for the subprocess path. ``cancel`` sends
    SIGTERM to the child's process group (graceful), waits briefly, then
    SIGKILL (forceful); ``execute`` blocks until the child exits so a timeout
    forces termination.
    """

    def __init__(self, engine: "Engine", ready_dir: str, **kwargs: Any) -> None:
        """Create a subprocess stub writing child PIDs under ``ready_dir``."""
        kwargs.setdefault("isolation", Isolation.SUBPROCESS)
        super().__init__(engine, **kwargs)
        self._ready_dir = ready_dir
        self._processes: dict[int, _SubprocessHandle] = {}
        self.terminated_signals: list[int] = []

    def _ready_path(self, handle: int) -> str:
        """Return the deterministic PID-report path for ``handle``."""
        return os.path.join(self._ready_dir, f"pids-{handle}.txt")

    def execute(self, handle: int) -> Any:
        """Spawn a child (which spawns a grandchild) and block until it exits.

        Uses the ``fork`` start method so the test-only child entrypoint (which
        lives under ``tests/`` and is not importable by a re-execed ``spawn``
        interpreter) is available in the child. The child immediately calls
        ``setpgrp`` and only sleeps, so the classic fork-in-thread hazard does
        not apply here.
        """
        import multiprocessing as mp

        with self._lock:
            self.recorded.execute_started.append(str(handle))
        ready_path = self._ready_path(handle)
        ctx = mp.get_context("fork")
        process = ctx.Process(
            target=_child_spawns_grandchild_and_sleeps,
            args=(ready_path,),
            daemon=False,
        )
        process.start()
        self._processes[handle] = _SubprocessHandle(handle, process, ready_path)
        # Block until the child is terminated by the runner (timeout/cancel).
        process.join()
        return f"subprocess-exit:{handle}"

    def child_pids(self, handle: int) -> tuple[int, int]:
        """Return ``(child_pid, grandchild_pid)`` written by the spawned child.

        Reads the deterministic ready-file path so the test may call this before
        ``execute`` has registered the process handle; polls until the child has
        reported both PIDs.
        """
        ready_path = self._ready_path(handle)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if os.path.exists(ready_path) and os.path.getsize(ready_path) > 0:
                with open(ready_path, encoding="utf-8") as fh:
                    child, grand = fh.read().split(",")
                return int(child), int(grand)
            time.sleep(0.02)
        raise AssertionError(f"child for unit {handle} never reported its PIDs")

    def cancel(self, handle: int) -> None:
        """Terminate the child's process group: SIGTERM, wait, then SIGKILL.

        Graceful-before-forceful: SIGTERM the process group (reaching the
        grandchild too), give it a short grace period, then SIGKILL any
        survivor. Sending to the group leaves no orphaned descendant.
        """
        with self._lock:
            self.recorded.cancelled.append(str(handle))
        sub = self._processes.get(handle)
        if sub is None or not sub.process.pid:
            return
        pid = sub.process.pid
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            return
        os.killpg(pgid, signal.SIGTERM)
        self.terminated_signals.append(signal.SIGTERM)
        sub.process.join(timeout=2.0)
        if sub.process.is_alive():
            os.killpg(pgid, signal.SIGKILL)
            self.terminated_signals.append(signal.SIGKILL)
            sub.process.join(timeout=2.0)

    def cleanup(self, handle: int) -> None:
        """Release the subprocess handle and record cleanup."""
        super().cleanup(handle)
        self._processes.pop(handle, None)


def create_stub_engine() -> "Engine":
    """Create a fresh SQLite engine with the stub work-unit table.

    Uses a shared-cache file-less in-memory database via a static pool so the
    same schema is visible across the runner's worker threads and claim
    sessions within one engine. Both the stub work-unit table and the shared
    ``pipeline_events`` table are created, since ``record_transition`` co-commits
    an event with every status change.
    """
    from sqlalchemy.pool import StaticPool
    from sqlmodel import create_engine

    from aizk.pipeline.events import PipelineEvent

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _StubBase.metadata.create_all(engine)
    PipelineEvent.__table__.create(engine)
    return engine


def create_stub_file_engine(db_path: str, *, create_schema: bool) -> "Engine":
    """Create a file-based stub SQLite engine over ``db_path``.

    Unlike :func:`create_stub_engine` (an in-memory DB that vanishes when its
    engine is disposed), this persists to a file so durable state survives a
    simulated process restart: dispose the first engine, then construct a fresh
    engine + handler + runner over the same file. ``create_schema`` creates
    the stub work-unit and ``pipeline_events`` tables on first construction; pass
    ``False`` for the "restarted process" engine so it reads the existing rows.
    """
    from sqlmodel import create_engine

    from aizk.pipeline.events import PipelineEvent

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    if create_schema:
        _StubBase.metadata.create_all(engine)
        PipelineEvent.__table__.create(engine)
    return engine
