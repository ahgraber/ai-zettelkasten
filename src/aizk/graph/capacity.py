"""Capacity limits over a graph stage's actionable backlog.

The graph stages construct work-unit rows in exactly two places — the
contextualization and extraction enqueue primitives — so the capacity check
belongs there rather than in front of any one caller. Every path that creates
work is then subject to the same limit with no bypass: the intake routes, an
admission pass, a backfill command, or a notebook.

A stage's **actionable backlog** is the work it still owes: units in
``QUEUED`` plus failed units awaiting an automatic retry (those carrying an
``earliest_next_attempt_at``). This mirrors the conversion service's actionable
set (``QUEUED`` + ``FAILED_RETRYABLE``), so one queue-depth vocabulary covers
the fleet. Succeeded, cancelled, timed-out, and retry-exhausted units are not
backlog; they do not hold the queue closed.

A limit of ``0`` (or below) declares no limit.

The limit is a throttle, not an invariant: the count and the insert are not
serialized across the API and worker processes, so a concurrently-admitting
writer can overshoot by the units in flight.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar

from sqlmodel import func, or_, select

from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlmodel import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StageAtCapacityError(RuntimeError):
    """Raised when a stage's actionable backlog is at or above its declared limit.

    Each surface maps this to its own refusal: intake answers HTTP 503 with a
    ``Retry-After`` header, an admission pass stops admitting and leaves the
    remainder pending, and a command exits non-zero.
    """

    def __init__(self, stage: str, *, depth: int, limit: int) -> None:
        """Record the refusing stage and the backlog reading that refused it."""
        super().__init__(f"{stage} stage is at capacity: {depth} actionable work-units, limit {limit}")
        self.stage = stage
        self.depth = depth
        self.limit = limit


def actionable_backlog(session: "Session", model: Any) -> int:
    """Return the count of ``model`` work-units the stage still owes.

    Args:
        session: Active session.
        model: The stage's work-unit table.
    """
    return session.exec(
        select(func.count())
        .select_from(model)
        .where(
            or_(
                model.status == WorkUnitStatus.QUEUED,
                (model.status == WorkUnitStatus.FAILED) & model.earliest_next_attempt_at.is_not(None),
            )
        )
    ).one()


def check_capacity(session: "Session", model: Any, *, stage: str, limit: int) -> None:
    """Refuse a new work-unit when the stage's actionable backlog has reached ``limit``.

    Callers invoke this only after their idempotency-dedupe branch, so a request
    resolving to an existing unit is returned rather than refused — it adds no
    work to the backlog.

    Args:
        session: Active session.
        model: The stage's work-unit table.
        stage: The stage name carried on the refusal.
        limit: The declared capacity; ``0`` or below declares no limit.

    Raises:
        StageAtCapacityError: When ``limit`` is positive and the backlog is at
            or above it.
    """
    if limit <= 0:
        return
    depth = actionable_backlog(session, model)
    if depth >= limit:
        raise StageAtCapacityError(stage, depth=depth, limit=limit)


def headroom(session: "Session", model: Any, *, limit: int) -> int | None:
    """Return how many new work-units the stage can still accept, or ``None`` when unlimited.

    Bulk callers read this once per batch and truncate their input to it, rather
    than counting the backlog per row.

    Args:
        session: Active session.
        model: The stage's work-unit table.
        limit: The declared capacity; ``0`` or below declares no limit.
    """
    if limit <= 0:
        return None
    return max(0, limit - actionable_backlog(session, model))


def within_headroom(
    session: "Session",
    model: Any,
    items: "Iterable[T]",
    *,
    stage: str,
    limit: int,
) -> list[T]:
    """Truncate a bulk enqueue's input to the stage's remaining capacity.

    Reads the headroom once for the whole batch. What is dropped is logged, not
    silently discarded; the remainder stays pending for a later batch.

    Args:
        session: Active session.
        model: The stage's work-unit table.
        items: The work the caller intends to enqueue, in admission order.
        stage: The stage name carried on the truncation log record.
        limit: The declared capacity; ``0`` or below declares no limit.

    Returns:
        The prefix of ``items`` the stage has room for.
    """
    candidates = list(items)
    room = headroom(session, model, limit=limit)
    if room is None or room >= len(candidates):
        return candidates
    logger.info(
        "Truncating %s bulk enqueue at capacity: admitting %d of %d, %d left pending",
        stage,
        room,
        len(candidates),
        len(candidates) - room,
        extra={"stage": stage, "admitted": room, "requested": len(candidates), "limit": limit},
    )
    return candidates[:room]
