"""Corpus backfill runs for the graph stages.

A backfill resolves a target set from current state, enqueues it through the
stage's existing enqueue primitives, and reports how much of that target set was
newly enqueued versus reused. It only enqueues: the work-units it creates stay
``QUEUED`` until a worker claims them.

Both runs follow the target-selection convention
:mod:`aizk.graph.dataset_extraction` established — an explicit, operator-named
target set is deliberate intent and is never confirmation-gated, while an
implicit corpus scan passes through
:func:`aizk.pipeline.invalidation.require_reprocessing_confirmation`. Because
every enqueue primitive dedupes on its ``idempotency_key``, re-running over an
unchanged corpus enqueues nothing new.

``dry_run`` performs the same work in a transaction that is rolled back instead
of committed, so the reported counts are the real ones rather than an estimate
computed by a second code path that could disagree with the first. A dry run
persists nothing and therefore has no blast radius to approve, so it satisfies
the confirmation gate on its own — previewing what a corpus sweep would do is
how an operator decides whether to confirm it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

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
    """How a backfill run resolved and enqueued its target set.

    Attributes:
        targeted: Work the run put through the stage's enqueue. A corpus scan
            truncated by the stage's capacity reports the admitted batch; what
            capacity left out is logged and stays eligible for a later run.
        enqueued: Targets that did not have a work-unit and now do.
        reused: Targets whose work-unit already existed, in any status.
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
    queue_max_depth: int = 0,
) -> BackfillResult:
    """Enqueue contextualization work-units for conversion outputs.

    An explicit ``output_ids`` enumeration is enqueued verbatim through the
    ungated single-enqueue path. Otherwise the target set is each source's newest
    conversion output (see
    :func:`aizk.graph.enqueue.latest_output_ids_per_source`), optionally capped by
    ``limit``, and the corpus-wide enqueue is confirmation-gated.

    Both paths are bounded by the stage's declared capacity: a corpus scan
    truncates to the batch's headroom, while a named enumeration refuses once the
    backlog is full, so the run commits nothing rather than partially.

    Args:
        engine: The shared engine.
        output_ids: Explicit target outputs, or ``None`` to scan the corpus.
        limit: Caps a corpus scan's target set; ignored when ``output_ids`` is
            supplied.
        confirmed: Explicit approval for a corpus scan; ignored when
            ``output_ids`` is supplied or when ``dry_run`` is set.
        dry_run: Resolve and enqueue, then roll back instead of committing.
        queue_max_depth: The stage's declared capacity over its actionable
            backlog; ``0`` (the default) declares no limit.

    Returns:
        The run's :class:`BackfillResult`.

    Raises:
        ReprocessingConfirmationError: If ``output_ids`` is ``None`` and neither
            ``confirmed`` nor ``dry_run`` is set.
        StageAtCapacityError: If a named output cannot be enqueued because the
            stage is at capacity.
        ValueError: If a named output id has no conversion output.
    """
    with Session(engine) as session:
        targets = list(output_ids) if output_ids is not None else latest_output_ids_per_source(session, limit=limit)
        before = _unit_count(session, ContextualizationJob)

        if output_ids is not None:
            units = [enqueue_output(session, output_id, queue_max_depth=queue_max_depth) for output_id in targets]
        else:
            units = enqueue_backfill_outputs(
                session, targets, confirmed=confirmed or dry_run, queue_max_depth=queue_max_depth
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
    queue_max_depth: int = 0,
) -> BackfillResult:
    """Enqueue extraction work-units for sources with something to extract.

    An explicit ``source_ids`` enumeration is enqueued verbatim through the
    ungated single-enqueue path. Otherwise every source with an active chunking
    run is enqueued through the confirmation-gated corpus-wide path (see
    :func:`aizk.graph.extraction_workunit.enqueue_extraction_backfill`), which
    resolves its own target set.

    Both paths are bounded by the stage's declared capacity: a corpus scan
    truncates to the batch's headroom, while a named enumeration refuses once the
    backlog is full, so the run commits nothing rather than partially.

    Args:
        engine: The shared engine.
        source_ids: Explicit target sources, or ``None`` to scan the corpus.
        confirmed: Explicit approval for a corpus scan; ignored when
            ``source_ids`` is supplied or when ``dry_run`` is set.
        dry_run: Resolve and enqueue, then roll back instead of committing.
        queue_max_depth: The stage's declared capacity over its actionable
            backlog; ``0`` (the default) declares no limit.

    Returns:
        The run's :class:`BackfillResult`.

    Raises:
        ReprocessingConfirmationError: If ``source_ids`` is ``None`` and neither
            ``confirmed`` nor ``dry_run`` is set.
        StageAtCapacityError: If a named source cannot be enqueued because the
            stage is at capacity.
    """
    with Session(engine) as session:
        before = _unit_count(session, ExtractionJob)

        if source_ids is not None:
            units = [
                enqueue_extraction(session, source_id=source_id, queue_max_depth=queue_max_depth)
                for source_id in source_ids
            ]
        else:
            units = enqueue_extraction_backfill(
                session, confirmed=confirmed or dry_run, queue_max_depth=queue_max_depth
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
