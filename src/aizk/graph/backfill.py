"""Run bounded contextualization and extraction backfills.

Explicit targets are enqueued directly. Corpus scans require confirmation.
Both operations respect stage capacity and return counts for targeted, enqueued,
and reused work-units.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlmodel import Session, func, select

from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.enqueue import enqueue_backfill_outputs, enqueue_output, latest_output_ids_per_source
from aizk.graph.extraction_workunit import enqueue_extraction, enqueue_extraction_backfill

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


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


def run_contextualization_backfill(
    engine: "Engine",
    *,
    output_ids: "Sequence[int] | None",
    limit: int | None,
    confirmed: bool,
    dry_run: bool,
    queue_max_depth: int,
    principal: str,
) -> BackfillResult:
    """Enqueue contextualization work-units for selected conversion outputs.

    Explicit ``output_ids`` are enqueued without confirmation. A corpus scan uses
    the newest output per source and requires ``confirmed`` unless ``dry_run`` is
    true. Both paths enforce capacity and commit nothing on a capacity refusal.

    Args:
        engine: Shared database engine.
        output_ids: Explicit outputs, or ``None`` for a corpus scan.
        limit: Maximum number of outputs in a corpus scan.
        confirmed: Approves a corpus scan.
        dry_run: Rolls back after resolving and enqueueing.
        queue_max_depth: Maximum actionable backlog; ``0`` disables the limit.
        principal: Identity recorded on creation events.

    Returns:
        The resulting :class:`BackfillResult`.

    Raises:
        ReprocessingConfirmationError: If a corpus scan is not confirmed.
        StageAtCapacityError: If a named output cannot be enqueued.
        ValueError: If a named output does not exist.
    """
    with Session(engine) as session:
        # Take the writer lock before reading the backlog. The capacity check counts
        # and then inserts; under a deferred transaction another writer can commit
        # between the two, and the insert either overshoots the limit or fails on a
        # snapshot conflict. Holding the lock across the pair makes the bound exact
        # here, as it already is on the intake and admission paths.
        session.exec(text("BEGIN IMMEDIATE"))
        targets = list(output_ids) if output_ids is not None else latest_output_ids_per_source(session, limit=limit)
        before = _unit_count(session, ContextualizationJob)

        if output_ids is not None:
            units = [
                enqueue_output(
                    session, output_id, queue_max_depth=queue_max_depth, origin="backfill", principal=principal
                )[0]
                for output_id in targets
            ]
        else:
            units = enqueue_backfill_outputs(
                session, targets, confirmed=confirmed or dry_run, queue_max_depth=queue_max_depth, principal=principal
            )

        session.flush()
        enqueued = _unit_count(session, ContextualizationJob) - before
        if dry_run:
            session.rollback()
        else:
            session.commit()

    result = BackfillResult(targeted=len(units), enqueued=enqueued, reused=len(units) - enqueued)
    logger.info(
        "Contextualization backfill complete",
        extra={
            "stage": "contextualization",
            "targeted": result.targeted,
            "enqueued": result.enqueued,
            "reused": result.reused,
            "dry_run": dry_run,
        },
    )
    return result


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
    with Session(engine) as session:
        # Hold the writer lock while counting and inserting so the capacity bound stays exact.
        session.exec(text("BEGIN IMMEDIATE"))
        before = _unit_count(session, ExtractionJob)

        if source_ids is not None:
            units = [
                enqueue_extraction(
                    session,
                    source_id=source_id,
                    queue_max_depth=queue_max_depth,
                    origin="backfill",
                    principal=principal,
                )[0]
                for source_id in source_ids
            ]
        else:
            units = enqueue_extraction_backfill(
                session, confirmed=confirmed or dry_run, queue_max_depth=queue_max_depth, principal=principal
            )

        session.flush()
        enqueued = _unit_count(session, ExtractionJob) - before
        if dry_run:
            session.rollback()
        else:
            session.commit()

    result = BackfillResult(targeted=len(units), enqueued=enqueued, reused=len(units) - enqueued)
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
