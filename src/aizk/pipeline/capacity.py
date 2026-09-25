"""Enforce capacity over a stage's actionable backlog.

Every enqueue path uses the same check over the stage's own work-unit table. A
non-positive limit means unlimited capacity; bulk paths read headroom once and
preserve input order. Capacity is a throttle, so concurrent writers can
overshoot the limit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar

from sqlmodel import func, or_, select

from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from collections.abc import Container, Iterable

    from sqlmodel import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StageAtCapacityError(RuntimeError):
    """Raised when actionable backlog reaches the stage limit."""

    def __init__(self, stage: str, *, depth: int, limit: int) -> None:
        """Record the refusing stage and the backlog reading that refused it."""
        super().__init__(f"{stage} stage is at capacity: {depth} actionable work-units, limit {limit}")
        self.stage = stage
        self.depth = depth
        self.limit = limit


def require_positive_limit(limit: int | None) -> None:
    """Reject a non-positive scan limit.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit}")


def actionable_backlog(session: "Session", model: Any) -> int:
    """Return the number of queued or retry-waiting work-units."""
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


def check_capacity(session: "Session", model: Any, *, stage: str, limit: int, zero_limit_closes: bool = False) -> None:
    """Raise when a new work-unit would exceed the stage capacity.

    Call this after idempotency deduplication. A non-positive ``limit`` disables
    the check, or refuses every new unit when ``zero_limit_closes`` is set.

    Raises:
        StageAtCapacityError: If the actionable backlog reaches ``limit``.
    """
    if limit <= 0 and not zero_limit_closes:
        return
    depth = actionable_backlog(session, model)
    if depth >= limit:
        raise StageAtCapacityError(stage, depth=depth, limit=limit)


def headroom(session: "Session", model: Any, *, limit: int) -> int | None:
    """Return available capacity, or ``None`` when ``limit`` is non-positive."""
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
    already_enqueued: "Container[T]" = (),
) -> list[T]:
    """Return the ordered items that fit the stage's capacity.

    Existing items in ``already_enqueued`` do not consume headroom. New items
    beyond capacity are logged and omitted; a non-positive ``limit`` admits all
    items.
    """
    candidates = list(items)
    room = headroom(session, model, limit=limit)
    if room is None:
        return candidates

    admitted: list[T] = []
    dropped = 0
    for item in candidates:
        if item in already_enqueued:
            admitted.append(item)
        elif room > 0:
            admitted.append(item)
            room -= 1
        else:
            dropped += 1
    if dropped:
        logger.info(
            "Truncating %s bulk enqueue at capacity: admitting %d of %d, %d left pending",
            stage,
            len(admitted),
            len(candidates),
            dropped,
            extra={"stage": stage, "admitted": len(admitted), "requested": len(candidates), "limit": limit},
        )
    return admitted


__all__ = [
    "StageAtCapacityError",
    "actionable_backlog",
    "check_capacity",
    "headroom",
    "require_positive_limit",
    "within_headroom",
]
