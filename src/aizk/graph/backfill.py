"""Run bounded contextualization and extraction backfills.

Explicit targets are enqueued directly. Corpus scans require confirmation.
Both operations respect stage capacity and return counts for targeted, enqueued,
and reused work-units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from sqlmodel import Session, func, select

from aizk.db.engine import begin_immediate_on

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine

    from aizk.pipeline.stage import StageDefinition

_Target = TypeVar("_Target")


@dataclass(frozen=True)
class BackfillResult:
    """Counts for a backfill target set.

    Attributes:
        targeted: Number of targets passed to enqueue after capacity truncation.
        enqueued: Number of new work-units.
        reused: Number of existing work-units.
    """

    targeted: int
    enqueued: int
    reused: int


def _unit_count(session: "Session", model: type) -> int:
    """Return the number of rows in a work-unit table."""
    return session.exec(select(func.count()).select_from(model)).one()


def run_backfill(
    engine: "Engine",
    definition: "StageDefinition[Any, Any]",
    *,
    targets: "Sequence[_Target] | None",
    enqueue_one: "Callable[[Session, _Target], tuple[Any, bool]]",
    enqueue_corpus: "Callable[[Session], Sequence[Any]]",
    dry_run: bool,
) -> BackfillResult:
    """Enqueue the explicit ``targets``, or the corpus when ``targets`` is ``None``, under the writer lock."""
    with Session(engine) as session:
        # Take the writer lock before reading the backlog. The capacity check counts
        # and then inserts; under a deferred transaction another writer can commit
        # between the two, and the insert either overshoots the limit or fails on a
        # snapshot conflict. Holding the lock across the pair makes the bound exact
        # here, as it already is on the intake and admission paths.
        begin_immediate_on(session)
        before = _unit_count(session, definition.work_unit_model)

        if targets is not None:
            units = [enqueue_one(session, target)[0] for target in targets]
        else:
            units = enqueue_corpus(session)

        session.flush()
        enqueued = _unit_count(session, definition.work_unit_model) - before
        if dry_run:
            session.rollback()
        else:
            session.commit()

    return BackfillResult(targeted=len(units), enqueued=enqueued, reused=len(units) - enqueued)
