"""Generic work-unit lifecycle and retry classification for pipeline stages.

This module defines the stage-agnostic state machine the harness reasons about
uniformly across stages. A unit is queued, then running, then reaches exactly
one terminal outcome — succeeded, failed, cancelled, or timed out. Each stage's
own status enum maps onto this generic lifecycle via
``StageRepository.map_result``; the harness never needs to know a stage's
private statuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True)
class TerminalOutcome:
    """A terminal lifecycle outcome together with its retry classification.

    Bundles the ``(terminal outcome, retry class)`` pair a stage's
    ``StageRepository.map_result`` produces. ``retry_class`` is required when —
    and only when — ``status`` is ``FAILED``; the other terminal outcomes
    (``SUCCEEDED``, ``CANCELLED``, ``TIMED_OUT``) carry no retry disposition,
    matching the spec rule that only a failed outcome is classified retryable
    or permanent.
    """

    status: WorkUnitStatus
    retry_class: RetryClass | None = None

    def __post_init__(self) -> None:
        """Validate the outcome is terminal and its classification matches.

        Raises:
            ValueError: If ``status`` is not terminal, if a ``FAILED`` outcome
                carries no ``retry_class``, or if a non-failed outcome carries
                one.
        """
        if not is_terminal(self.status):
            raise ValueError(f"{self.status!r} is not a terminal outcome")
        if self.status is WorkUnitStatus.FAILED and self.retry_class is None:
            raise ValueError("A failed outcome must be classified retryable or permanent")
        if self.status is not WorkUnitStatus.FAILED and self.retry_class is not None:
            raise ValueError(f"retry_class is only meaningful for a failed outcome, not {self.status!r}")

    @property
    def is_retryable(self) -> bool:
        """Return ``True`` for a retryable failed outcome.

        Classification only: the harness layers retry-wait gating on top of
        this when selecting work (see the bounded-concurrency requirement).
        """
        return self.status is WorkUnitStatus.FAILED and self.retry_class is RetryClass.RETRYABLE
