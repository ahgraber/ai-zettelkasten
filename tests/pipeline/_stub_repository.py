"""In-memory stub :class:`StageRepository` for harness tests.

This is **test support, not shipped code**. It implements a minimal stage-owned
work-unit table over SQLite and drives its claim/transition through the real
:func:`aizk.pipeline.events.record_transition` co-commit helper, so harness
tests exercise the genuine "harness owns the session + ``BEGIN IMMEDIATE``,
repository never commits" seam rather than a mock.

The stub is deliberately controllable per test:

- ``enqueue`` seeds eligible (or retry-delayed) work-units in submission order;
- each unit carries a ``behavior`` callable the stub runs in ``execute`` so a
  test can make a unit slow, block on an event, or raise a classified failure;
- a subprocess-isolated variant (:class:`SubprocessStubRepository`) spawns a
  real child process so the no-orphan-descendants guarantee is exercised.

The stub work-unit table lives on its own private :class:`MetaData` (not
``SQLModel.metadata``) so it never pollutes the shared metadata that the
migration parity tests compare against ``create_all``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import datetime as dt
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlmodel import Session, select

from aizk.pipeline.events import record_transition
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.repository import Isolation

if TYPE_CHECKING:
    from sqlalchemy import Engine


def _utcnow() -> dt.datetime:
    """Return a timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


class _StubBase(DeclarativeBase):
    """Private declarative base keeping the stub table off ``SQLModel.metadata``."""


class StubWorkUnit(_StubBase):
    """A stage-owned work-unit row with the columns the harness loop needs.

    ``status`` carries the stub stage's own status text (the generic lifecycle
    values are reused for simplicity, but the harness never reads them — it
    drives everything through the repository). ``queued_at`` defines submission
    order; ``earliest_next_attempt_at`` gates retry-wait eligibility;
    ``started_at`` marks running units for stale recovery.
    """

    __tablename__ = "_stub_work_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column()
    aizk_uuid: Mapped[str] = mapped_column()
    label: Mapped[str] = mapped_column()
    queued_at: Mapped[dt.datetime] = mapped_column()
    earliest_next_attempt_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    retry_class: Mapped[str | None] = mapped_column(nullable=True, default=None)


class StubTransitionPayload(BaseModel):
    """Typed per-kind payload for the stub stage's transition events."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["stub_transition"] = "stub_transition"
    note: str
    cause: str | None = None


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


_ELIGIBLE_STATUSES = (WorkUnitStatus.QUEUED.value, WorkUnitStatus.FAILED.value)


class StubStageRepository:
    """An in-memory :class:`~aizk.pipeline.repository.StageRepository` for tests.

    All claim/transition writes run inside the harness-owned transaction via
    ``record_transition`` and never commit, matching the production contract.
    Execution behavior is per-unit and supplied by the test through
    :meth:`enqueue`.
    """

    stage = "stub"

    def __init__(
        self,
        engine: "Engine",
        *,
        timeout: dt.timedelta = dt.timedelta(seconds=30),
        concurrency_limit: int = 4,
        stale_after: dt.timedelta = dt.timedelta(minutes=5),
        dependencies_ok: bool = True,
        isolation: Isolation = Isolation.IN_PROCESS,
        stage_name: str | None = None,
    ) -> None:
        """Create a stub repository bound to ``engine``.

        Args:
            engine: SQLite engine holding the stub work-unit table.
            timeout: Wall-clock timeout the harness enforces per unit.
            concurrency_limit: Maximum simultaneously-executing units.
            stale_after: Age past which a ``running`` unit is treated as stale.
            dependencies_ok: When ``False``, ``validate_dependencies`` raises.
            isolation: In-process or subprocess execution model.
            stage_name: Optional override for the ``stage`` identifier so two
                stub repositories can share a process with distinct stages.
        """
        self._engine = engine
        self._timeout = timeout
        self._concurrency_limit = concurrency_limit
        self._stale_after = stale_after
        self._dependencies_ok = dependencies_ok
        self._isolation = isolation
        if stage_name is not None:
            self.stage = stage_name
        self._behaviors: dict[int, Behavior] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self.recorded = _Recorded()
        self._lock = threading.Lock()

    # --- test control surface -------------------------------------------------

    def enqueue(
        self,
        label: str,
        *,
        behavior: Behavior = succeed,
        earliest_next_attempt_at: dt.datetime | None = None,
        status: str = WorkUnitStatus.QUEUED.value,
        queued_at: dt.datetime | None = None,
    ) -> int:
        """Seed one work-unit and return its id.

        ``queued_at`` defaults to a monotonically increasing instant so units
        enqueued earlier sort first (submission order). ``behavior`` is the
        callable ``execute`` runs for this unit.
        """
        now = queued_at or _utcnow()
        with Session(self._engine) as session:
            unit = StubWorkUnit(
                status=status,
                aizk_uuid=str(uuid4()),
                label=label,
                queued_at=now,
                earliest_next_attempt_at=earliest_next_attempt_at,
            )
            session.add(unit)
            session.commit()
            unit_id = unit.id
        self._behaviors[unit_id] = behavior
        self._cancel_events[unit_id] = threading.Event()
        return unit_id

    def cancel_event(self, unit_id: int) -> threading.Event:
        """Return the cooperative-cancel event a unit's behavior may poll."""
        return self._cancel_events[unit_id]

    def get_status(self, unit_id: int) -> str:
        """Return the unit's current persisted status text."""
        with Session(self._engine) as session:
            unit = session.get(StubWorkUnit, unit_id)
            assert unit is not None
            return unit.status

    def force_running_stale(self, unit_id: int) -> None:
        """Persist a unit as ``running`` with a backdated ``started_at``.

        Simulates a unit stranded by an interrupted runtime so stale recovery
        has something to reclaim.
        """
        with Session(self._engine) as session:
            unit = session.get(StubWorkUnit, unit_id)
            assert unit is not None
            unit.status = WorkUnitStatus.RUNNING.value
            unit.started_at = _utcnow() - self._stale_after - dt.timedelta(seconds=1)
            session.add(unit)
            session.commit()

    # --- StageRepository protocol --------------------------------------------

    def validate_dependencies(self) -> None:
        """Raise when configured to simulate a missing dependency."""
        if not self._dependencies_ok:
            raise RuntimeError("stub dependency unavailable")

    def claim_next(self, session: Session) -> int | None:
        """Claim the next eligible unit in submission order, or ``None``.

        Runs inside the harness-owned ``BEGIN IMMEDIATE`` transaction: selects
        the oldest eligible unit (queued or retryable-failed past its
        retry-wait) and transitions it to ``running`` via ``record_transition``.
        Does not commit.
        """
        now = _utcnow()
        unit = session.exec(
            select(StubWorkUnit)
            .where(StubWorkUnit.status.in_(_ELIGIBLE_STATUSES))
            # Only retryable-failed is re-eligible; a permanent failure never re-claims.
            .where((StubWorkUnit.retry_class.is_(None)) | (StubWorkUnit.retry_class != "permanent"))
            .where((StubWorkUnit.earliest_next_attempt_at.is_(None)) | (StubWorkUnit.earliest_next_attempt_at <= now))
            .order_by(StubWorkUnit.queued_at, StubWorkUnit.id)
        ).first()
        if unit is None:
            return None
        unit.started_at = now
        unit.attempts += 1
        record_transition(
            session,
            unit,
            stage=self.stage,
            work_unit_ref=str(unit.id),
            aizk_uuid=UUID(unit.aizk_uuid),
            to_status=WorkUnitStatus.RUNNING.value,
            kind="stub_transition",
            attempt=unit.attempts,
            payload=StubTransitionPayload(note="claimed"),
        )
        return unit.id

    def recover_stale(self, session: Session) -> list[int]:
        """Return ``running`` units older than ``stale_after`` to eligible.

        Records the recovery cause in each unit's transition event. Runs inside
        the harness-owned transaction; does not commit.
        """
        threshold = _utcnow() - self._stale_after
        units = session.exec(
            select(StubWorkUnit)
            .where(StubWorkUnit.status == WorkUnitStatus.RUNNING.value)
            .where(StubWorkUnit.started_at.is_not(None))
            .where(StubWorkUnit.started_at < threshold)
        ).all()
        recovered: list[int] = []
        for unit in units:
            record_transition(
                session,
                unit,
                stage=self.stage,
                work_unit_ref=str(unit.id),
                aizk_uuid=UUID(unit.aizk_uuid),
                to_status=WorkUnitStatus.QUEUED.value,
                kind="stub_transition",
                attempt=unit.attempts,
                payload=StubTransitionPayload(note="recovered", cause="worker_stale_running"),
            )
            recovered.append(unit.id)
        return recovered

    def execute(self, handle: int) -> Any:
        """Run the unit's per-test behavior callable."""
        with self._lock:
            self.recorded.execute_started.append(str(handle))
        result = self._behaviors[handle](str(handle))
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

    def finalize(self, session: Session, handle: int, outcome: TerminalOutcome) -> None:
        """Transition the unit to its terminal status via ``record_transition``.

        For a retryable failure, set a retry-wait so the unit is not immediately
        re-eligible. Runs inside the harness-owned transaction; does not commit.
        """
        unit = session.get(StubWorkUnit, handle)
        assert unit is not None
        now = _utcnow()
        if outcome.status is WorkUnitStatus.FAILED and outcome.is_retryable:
            unit.earliest_next_attempt_at = now + dt.timedelta(seconds=60)
            unit.retry_class = "retryable"
        elif outcome.status is WorkUnitStatus.FAILED:
            # Permanent failure: cleared retry-wait but marked so claim_next excludes it.
            unit.earliest_next_attempt_at = None
            unit.started_at = None
            unit.retry_class = "permanent"
        else:
            unit.earliest_next_attempt_at = None
            unit.started_at = None
        record_transition(
            session,
            unit,
            stage=self.stage,
            work_unit_ref=str(unit.id),
            aizk_uuid=UUID(unit.aizk_uuid),
            to_status=outcome.status.value,
            kind="stub_transition",
            attempt=unit.attempts,
            payload=StubTransitionPayload(note=f"finalize:{outcome.status.value}"),
        )

    def cleanup(self, handle: int) -> None:
        """Record that transient resources were released for ``handle``."""
        with self._lock:
            self.recorded.cleaned_up.append(str(handle))

    def cancel(self, handle: int) -> None:
        """Cooperatively signal the unit to stop; record the request."""
        with self._lock:
            self.recorded.cancelled.append(str(handle))
        event = self._cancel_events.get(handle)
        if event is not None:
            event.set()

    def scope_key(self, handle: int) -> str:
        """Return a per-unit scope key for ``handle``."""
        return f"unit:{handle}"

    @property
    def timeout(self) -> dt.timedelta:
        """Wall-clock timeout after which a running unit is terminated."""
        return self._timeout

    @property
    def concurrency_limit(self) -> int:
        """Maximum number of work-units executed simultaneously."""
        return self._concurrency_limit

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
    (``setpgrp``) lets the harness terminate the whole descendant tree with a
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


class SubprocessStubRepository(StubStageRepository):
    """Subprocess-isolated stub: ``execute`` spawns a real child + grandchild.

    Exercises the harness's graceful-before-forceful termination and the
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
        # Block until the child is terminated by the harness (timeout/cancel).
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
    same schema is visible across the harness's worker threads and claim
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
