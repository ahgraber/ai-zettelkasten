"""Enqueue contextualization work-units from conversion outputs.

The helpers resolve source identities, deduplicate by ``idempotency_key``, and
respect stage capacity. They stage rows in the caller's transaction and never
commit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlmodel import func, select

from aizk.contextualization.events import CONTEXTUALIZATION_STAGE
from aizk.contextualization.stage import (
    CONTEXTUALIZATION_DEFINITION,
    ContextualizationParams,
    _newest_output_per_source,
    _require_bounding_limit,
    pending_contextualization_outputs,
)
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.admission import AdmissionAdapter
from aizk.pipeline.capacity import within_headroom
from aizk.pipeline.invalidation import require_reprocessing_confirmation
from aizk.pipeline.stage import enqueue

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from uuid import UUID

    from sqlmodel import Session

    from aizk.pipeline.admission import AdmissionConfig
    from aizk.pipeline.events import CreationOrigin

logger = logging.getLogger(__name__)


def latest_output_id(session: "Session", source_id: "UUID") -> int | None:
    """Return the newest conversion output id for ``source_id``, or ``None`` if absent.

    The newest output has the greatest id.
    """
    return session.exec(select(func.max(ConversionOutput.id)).where(ConversionOutput.source_id == source_id)).one()


def latest_output_ids_per_source(session: "Session", *, limit: int | None = None) -> list[int]:
    """Return newest conversion output ids in reproducible order.

    A positive ``limit`` bounds the selected sources. Each source contributes only
    its newest output.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    _require_bounding_limit(limit)
    winners = _newest_output_per_source()
    if limit is not None:
        winners = winners.limit(limit)
    return [row[0] for row in session.exec(winners).all()]


def enqueued_outputs(session: "Session", conversion_output_ids: "Sequence[int]") -> set[int]:
    """Return conversion output IDs that already have contextualization work units.

    Bulk enqueue uses this set to distinguish reuse from creation before capacity
    checks.
    """
    if not conversion_output_ids:
        return set()
    return set(
        session.exec(
            select(ContextualizationJob.conversion_output_id).where(
                ContextualizationJob.conversion_output_id.in_(conversion_output_ids)
            )
        ).all()
    )


def enqueue_document(
    session: "Session",
    *,
    conversion_output_id: int,
    source_id: "UUID",
    queue_max_depth: int,
    origin: "CreationOrigin",
    principal: str,
) -> tuple[ContextualizationJob, bool]:
    """Enqueue or reuse one contextualization work unit.

    Existing units return unchanged without a capacity check. Otherwise the
    function checks capacity, stages a queued unit and its creation event, and
    does not commit.

    Returns:
        The existing or new job and whether this call created it.

    Raises:
        StageAtCapacityError: If a new unit would exceed ``queue_max_depth``.
    """
    job, created = enqueue(
        session,
        CONTEXTUALIZATION_DEFINITION,
        ContextualizationParams(conversion_output_id=conversion_output_id, source_id=source_id),
        origin=origin,
        principal=principal,
        queue_max_depth=queue_max_depth,
    )
    if not created:
        logger.debug(
            "Reusing contextualization work-unit id=%s for conversion_output_id=%s",
            job.id,
            conversion_output_id,
        )
        return job, False
    logger.debug(
        "Enqueued contextualization work-unit id=%s for conversion_output_id=%s", job.id, conversion_output_id
    )
    return job, True


def enqueue_backfill(
    session: "Session",
    documents: "Iterable[tuple[int, UUID]]",
    *,
    confirmed: bool = False,
    queue_max_depth: int,
    principal: str,
) -> list[ContextualizationJob]:
    """Enqueue or reuse work units for many documents.

    Each pair uses :func:`enqueue_document`, so deduplication is shared with
    incremental enqueue. Capacity limits new units and leaves excess candidates
    pending. The function stages rows, does not commit, and requires
    ``confirmed=True``.

    Returns:
        The enqueued or reused work units.

    Raises:
        ReprocessingConfirmationError: When ``confirmed`` is false.
    """
    require_reprocessing_confirmation("corpus-wide contextualization backfill", confirmed=confirmed)
    candidates = list(documents)
    enqueued = enqueued_outputs(session, [conversion_output_id for conversion_output_id, _ in candidates])
    admitted = within_headroom(
        session,
        ContextualizationJob,
        candidates,
        stage=CONTEXTUALIZATION_STAGE,
        limit=queue_max_depth,
        already_enqueued={pair for pair in candidates if pair[0] in enqueued},
    )
    return [
        enqueue_document(
            session,
            conversion_output_id=conversion_output_id,
            source_id=source_id,
            queue_max_depth=queue_max_depth,
            origin="backfill",
            principal=principal,
        )[0]
        for conversion_output_id, source_id in admitted
    ]


def enqueue_output(
    session: "Session",
    conversion_output_id: int,
    *,
    queue_max_depth: int,
    origin: "CreationOrigin",
    principal: str,
) -> "tuple[ContextualizationJob, bool]":
    """Enqueue or reuse the contextualization unit for ``conversion_output_id``.

    Resolves the source identity from the conversion output and does not commit.
    Existing units return with ``created=False``.

    Raises:
        ValueError: If the conversion output does not exist.
        StageAtCapacityError: If the stage is full and no unit exists.
    """
    output = session.get(ConversionOutput, conversion_output_id)
    if output is None:
        raise ValueError(f"conversion output {conversion_output_id} not found")
    return enqueue_document(
        session,
        conversion_output_id=conversion_output_id,
        source_id=output.source_id,
        queue_max_depth=queue_max_depth,
        origin=origin,
        principal=principal,
    )


def enqueue_backfill_outputs(
    session: "Session",
    conversion_output_ids: "Iterable[int]",
    *,
    confirmed: bool = False,
    queue_max_depth: int,
    principal: str,
) -> list["ContextualizationJob"]:
    """Enqueue or reuse contextualization units for a batch of outputs.

    Uses the same deduplication as ``enqueue_output`` and does not commit. A
    non-confirmed call raises ``ReprocessingConfirmationError``. The batch is
    limited by available capacity; omitted outputs remain pending.
    """
    require_reprocessing_confirmation("corpus-wide contextualization backfill", confirmed=confirmed)
    candidates = list(conversion_output_ids)
    admitted = within_headroom(
        session,
        ContextualizationJob,
        candidates,
        stage=CONTEXTUALIZATION_STAGE,
        limit=queue_max_depth,
        already_enqueued=enqueued_outputs(session, candidates),
    )
    return [
        enqueue_output(
            session, conversion_output_id, queue_max_depth=queue_max_depth, origin="backfill", principal=principal
        )[0]
        for conversion_output_id in admitted
    ]


def contextualization_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the contextualization stage's admission over conversion outputs."""
    return AdmissionAdapter(
        stage=CONTEXTUALIZATION_STAGE,
        model=ContextualizationJob,
        pending_work=lambda session, limit: pending_contextualization_outputs(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth, principal: enqueue_output(
            session, key, queue_max_depth=queue_max_depth, origin="admission", principal=principal
        ),
        queue_max_depth=config.contextualization_queue_max_depth,
        enabled=config.admission_contextualization_enabled,
    )
