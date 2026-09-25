"""Generic work-unit lifecycle and retry classification for pipeline stages.

This module defines the stage-agnostic state machine the runner reasons about
uniformly across stages. A unit is queued, then running, then reaches exactly
one terminal outcome — succeeded, failed, cancelled, or timed out.

:class:`WorkUnitColumns` is the shared declaration of the lifecycle columns a
stage's work-unit table carries; each stage keeps its own table and adds its
identity and extension columns beside them.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum as SAEnum, Text, func
from sqlmodel import Field, SQLModel

from aizk.utilities.clock import utcnow

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

#: Maximum work-units one bulk action may target, on any operator surface.
#: Bounds the write transaction a single action opens. Stated once so the JSON
#: bulk endpoint and the console cannot drift to different ceilings.
MAX_BULK_SELECTION = 100


class WorkUnitStatus(str, Enum):
    """Generic lifecycle states a work-unit passes through.

    A unit is ``QUEUED``, then ``RUNNING``, then reaches exactly one terminal
    outcome: ``SUCCEEDED``, ``FAILED``, ``CANCELLED``, or ``TIMED_OUT``.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES: frozenset[WorkUnitStatus] = frozenset(
    {
        WorkUnitStatus.SUCCEEDED,
        WorkUnitStatus.FAILED,
        WorkUnitStatus.CANCELLED,
        WorkUnitStatus.TIMED_OUT,
    }
)
"""The terminal subset of :class:`WorkUnitStatus`."""


class RetryClass(str, Enum):
    """Retry disposition of a failed terminal outcome."""

    RETRYABLE = "retryable"
    PERMANENT = "permanent"


def is_terminal(status: WorkUnitStatus) -> bool:
    """Return ``True`` when ``status`` is a terminal lifecycle outcome."""
    return status in TERMINAL_STATUSES


def derive_retry_class(
    status: WorkUnitStatus, earliest_next_attempt_at: datetime.datetime | None
) -> RetryClass | None:
    """Return ``retryable`` for a ``failed`` unit with a retry wait, ``permanent`` without one, else ``None``."""
    if status != WorkUnitStatus.FAILED:
        return None
    return RetryClass.RETRYABLE if earliest_next_attempt_at is not None else RetryClass.PERMANENT


def retry_class_filter(model: Any, retry_class: RetryClass) -> ColumnElement[bool]:
    """Return the SQL condition selecting ``model``'s ``failed`` units of ``retry_class``."""
    wait = model.earliest_next_attempt_at
    has_wait = wait.is_not(None) if retry_class is RetryClass.RETRYABLE else wait.is_(None)
    return (model.status == WorkUnitStatus.FAILED) & has_wait


#: The error code of a unit the runner terminated because its drain timeout
#: elapsed; the unit is ``failed`` with an immediate retry wait.
WORKER_DRAIN_TERMINATED = "worker_drain_terminated"


class WorkUnitColumns(SQLModel):
    """Lifecycle columns shared by every stage's work-unit table.

    A stage's table class inherits this beside ``table=True`` and declares its
    own primary key, idempotency key, ``source_id``, extension columns,
    ``__tablename__``, and ``__table_args__``. ``status`` and the scheduling
    fields carry runtime claim, lease, and retry state; the runtime owns
    lifecycle transitions.

    Columns are declared with ``sa_type`` and ``sa_column_kwargs`` rather than
    a ``Column`` object, because a ``Column`` instance binds to one table and
    cannot be shared. Datetime columns are naive UTC; ``created_at`` and
    ``updated_at`` also carry a database-side ``CURRENT_TIMESTAMP`` default.
    """

    # values_callable stores enum values ("queued") not names ("QUEUED"), matching RunStatus.
    status: WorkUnitStatus = Field(
        default=WorkUnitStatus.QUEUED,
        sa_type=SAEnum(WorkUnitStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    attempts: int = Field(default=0, nullable=False)
    error_code: str | None = Field(default=None, nullable=True)
    error_message: str | None = Field(default=None, sa_type=Text, nullable=True)
    earliest_next_attempt_at: datetime.datetime | None = Field(default=None, nullable=True)
    last_error_at: datetime.datetime | None = Field(default=None, nullable=True)
    queued_at: datetime.datetime | None = Field(default=None, nullable=True)
    started_at: datetime.datetime | None = Field(default=None, nullable=True)
    finished_at: datetime.datetime | None = Field(default=None, nullable=True)
    created_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(),
        nullable=False,
        sa_column_kwargs={"server_default": func.current_timestamp()},
    )
    updated_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(),
        nullable=False,
        sa_column_kwargs={"server_default": func.current_timestamp()},
    )


@dataclass(frozen=True)
class TerminalOutcome:
    """A terminal lifecycle outcome together with its retry classification.

    Bundles the ``(terminal outcome, retry class)`` pair a stage's
    ``StageHandler.map_result`` produces. ``retry_class`` is required when —
    and only when — ``status`` is ``FAILED``; the other terminal outcomes
    (``SUCCEEDED``, ``CANCELLED``, ``TIMED_OUT``) carry no retry disposition,
    matching the spec rule that only a failed outcome is classified retryable
    or permanent.

    ``error_code`` names a runtime-caused failure (the runner terminated the
    unit, as with :data:`WORKER_DRAIN_TERMINATED`) that replaces the stage's
    own classification of the exception; it is meaningful only for a
    ``FAILED`` outcome.
    """

    status: WorkUnitStatus
    retry_class: RetryClass | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        """Coerce inputs to their enums, then validate terminality and classification.

        Coercion first: ``WorkUnitStatus`` is a ``str`` enum, so a raw string
        like ``"failed"`` is a member of :data:`TERMINAL_STATUSES` yet is not
        identical to ``WorkUnitStatus.FAILED``. Without normalizing, the
        identity checks below would silently skip the failed-classification
        rule. Coercion also rejects unknown strings.

        Raises:
            ValueError: If ``status`` (or ``retry_class``) is not a valid enum
                value, if ``status`` is not terminal, if a ``FAILED`` outcome
                carries no ``retry_class``, or if a non-failed outcome carries
                a ``retry_class`` or an ``error_code``.
        """
        object.__setattr__(self, "status", WorkUnitStatus(self.status))
        if self.retry_class is not None:
            object.__setattr__(self, "retry_class", RetryClass(self.retry_class))
        if not is_terminal(self.status):
            raise ValueError(f"{self.status!r} is not a terminal outcome")
        if self.status is WorkUnitStatus.FAILED and self.retry_class is None:
            raise ValueError("A failed outcome must be classified retryable or permanent")
        if self.status is not WorkUnitStatus.FAILED and self.retry_class is not None:
            raise ValueError(f"retry_class is only meaningful for a failed outcome, not {self.status!r}")
        if self.status is not WorkUnitStatus.FAILED and self.error_code is not None:
            raise ValueError(f"error_code is only meaningful for a failed outcome, not {self.status!r}")

    @property
    def is_retryable(self) -> bool:
        """Return ``True`` for a retryable failed outcome.

        Classification only: the runner layers retry-wait gating on top of
        this when selecting work (see the bounded-concurrency requirement).
        """
        return self.status is WorkUnitStatus.FAILED and self.retry_class is RetryClass.RETRYABLE
