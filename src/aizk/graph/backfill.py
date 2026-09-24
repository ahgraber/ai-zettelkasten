"""Run bounded contextualization and extraction backfills.

Explicit targets are enqueued directly. Corpus scans require confirmation.
Both operations respect stage capacity and return counts for targeted, enqueued,
and reused work-units.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
from typing import TYPE_CHECKING, Any, TypeVar

from sqlmodel import Session, func, select

from aizk.db.engine import begin_immediate_on
from aizk.graph.extraction_workunit import EXTRACTION_DEFINITION, enqueue_extraction, enqueue_extraction_backfill

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from sqlalchemy import Engine

    from aizk.pipeline.stage import StageDefinition

logger = logging.getLogger(__name__)

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


def run_extraction_backfill(
    engine: "Engine",
    *,
    source_ids: "Sequence[UUID] | None",
    confirmed: bool,
    dry_run: bool,
    queue_max_depth: int,
    principal: str,
) -> BackfillResult:
    """Enqueue extraction work-units for selected sources.

    Explicit ``source_ids`` are enqueued without confirmation. A corpus scan
    requires ``confirmed`` unless ``dry_run`` is true and is bounded by capacity.

    Args:
        engine: Shared database engine.
        source_ids: Explicit sources, or ``None`` for a corpus scan.
        confirmed: Approves a corpus scan.
        dry_run: Rolls back after resolving and enqueueing.
        queue_max_depth: Maximum actionable backlog; ``0`` disables the limit.
        principal: Identity recorded on creation events.

    Returns:
        The resulting :class:`BackfillResult`.

    Raises:
        ReprocessingConfirmationError: If a corpus scan is not confirmed.
        StageAtCapacityError: If a named source cannot be enqueued.
    """
    result = run_backfill(
        engine,
        EXTRACTION_DEFINITION,
        targets=source_ids,
        enqueue_one=lambda session, source_id: enqueue_extraction(
            session,
            source_id=source_id,
            queue_max_depth=queue_max_depth,
            origin="backfill",
            principal=principal,
        ),
        enqueue_corpus=lambda session: enqueue_extraction_backfill(
            session, confirmed=confirmed or dry_run, queue_max_depth=queue_max_depth, principal=principal
        ),
        dry_run=dry_run,
    )
    logger.info(
        "Extraction backfill complete",
        extra={
            "stage": "extraction",
            "targeted": result.targeted,
            "enqueued": result.enqueued,
            "reused": result.reused,
            "dry_run": dry_run,
        },
    )
    return result


def __getattr__(name: str) -> object:
    """Import the contextualization backfill on first access to ``run_contextualization_backfill`` and return it."""
    if name != "run_contextualization_backfill":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module("aizk.contextualization.backfill"), name)
