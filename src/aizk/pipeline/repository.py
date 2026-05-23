"""The stage-supplied repository protocol the harness drives.

The harness owns the loop, concurrency bound, signal handling and graceful
drain, wall-clock timeout enforcement, stale-recovery scheduling, transition
co-commit, and observability. Each stage supplies a :class:`StageRepository`
over its own work-unit tables that owns: startup dependency validation,
work-unit discovery + claim, the unit-of-work execution, mapping its execution
result to a generic terminal outcome, cancellation, transient-resource cleanup,
the timeout/concurrency configuration, and the run ``scope_key``.

The **harness owns the session and transaction boundary**: it opens a
``BEGIN IMMEDIATE`` transaction and passes the session into ``claim_next`` /
``recover_stale`` / ``finalize`` so the repository's status transition (routed
through :func:`aizk.pipeline.events.record_transition`) is co-committed with the
harness's bookkeeping in one transaction. The repository never commits.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.pipeline.lifecycle import TerminalOutcome

WorkUnitHandle = TypeVar("WorkUnitHandle")
"""Opaque, stage-defined handle to a claimed work-unit.

The harness passes handles through (``execute`` → ``map_result`` → ``finalize``
→ ``cleanup``) without inspecting them; only the stage's repository understands
their shape.
"""

StageResult: TypeAlias = Any
"""Opaque, stage-defined result of a unit-of-work execution.

Flows from ``execute`` straight into ``map_result`` without harness inspection.
"""


class Isolation(str, Enum):
    """Execution isolation a stage opts its unit-of-work into."""

    IN_PROCESS = "in_process"
    SUBPROCESS = "subprocess"


@runtime_checkable
class StageRepository(Protocol[WorkUnitHandle]):
    """The seam a stage implements over its own store for the harness to drive.

    Implementations run their claim/transition queries inside the
    harness-owned transaction and never commit. The methods split the
    functional core (unit-of-work + result mapping) from the harness's
    imperative shell (claim/commit/transition/cleanup loop).
    """

    def validate_dependencies(self) -> None:
        """Validate required external dependencies; raise if any are missing.

        Called at startup before any work is accepted. Raising blocks the
        harness from claiming work.
        """
        ...

    def claim_next(self, session: "Session") -> WorkUnitHandle | None:
        """Claim the next eligible work-unit in submission order, or ``None``.

        Runs inside the harness-owned ``BEGIN IMMEDIATE`` transaction: selects
        the next eligible unit, transitions it to ``running`` via
        ``record_transition``, and returns an opaque handle (or ``None`` when
        no unit is eligible). Does not commit.
        """
        ...

    def recover_stale(self, session: "Session") -> list[WorkUnitHandle]:
        """Transition units stranded in ``running`` back to eligible.

        Records the recovery cause in each unit's transition event. Runs inside
        the harness-owned transaction and does not commit.
        """
        ...

    def execute(self, handle: WorkUnitHandle) -> StageResult:
        """Run the stage's unit-of-work for ``handle``.

        Performs the (pure-ish) work plus the side effects the adapter owns; it
        does not write the unit's status (the harness routes status changes
        through ``finalize``).
        """
        ...

    def map_result(self, result_or_exc: "StageResult | BaseException") -> "TerminalOutcome":
        """Map an execution result or exception to a terminal outcome.

        Returns a :class:`~aizk.pipeline.lifecycle.TerminalOutcome` bundling the
        terminal status with the ``retryable``/``permanent`` classification for
        a failed outcome.
        """
        ...

    def finalize(self, session: "Session", handle: WorkUnitHandle, outcome: "TerminalOutcome") -> None:
        """Transition the work-unit to its terminal status via ``record_transition``.

        Runs inside the harness-owned transaction and does not commit.
        """
        ...

    def cleanup(self, handle: WorkUnitHandle) -> None:
        """Release the work-unit's transient resources; called on every outcome."""
        ...

    def cancel(self, handle: WorkUnitHandle) -> None:
        """Cooperatively request cancellation of a running work-unit."""
        ...

    def scope_key(self, handle: WorkUnitHandle) -> str:
        """Return the run ``scope_key`` for ``handle`` (per-document, per-chunk, ...)."""
        ...

    @property
    def timeout(self) -> datetime.timedelta:
        """Wall-clock timeout after which a running unit is terminated."""
        ...

    @property
    def concurrency_limit(self) -> int:
        """Maximum number of work-units the harness may execute simultaneously."""
        ...

    @property
    def isolation(self) -> Isolation:
        """Whether the unit-of-work runs in-process or in an isolated subprocess."""
        ...
