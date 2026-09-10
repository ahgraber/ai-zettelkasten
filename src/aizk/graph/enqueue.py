"""Enqueue contextualization work-units from conversion outputs.

The helpers resolve source identities, deduplicate by ``idempotency_key``, and
respect stage capacity. They stage rows in the caller's transaction and never
commit. Pending-output selection is shared by backfill, admission, and operator
surfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import func, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.workunit import enqueue_document, enqueued_outputs
from aizk.pipeline.capacity import require_positive_limit, within_headroom
from aizk.pipeline.invalidation import require_reprocessing_confirmation

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from uuid import UUID

    from sqlalchemy import Select
    from sqlmodel import Session

    from aizk.pipeline.events import CreationOrigin


#: The scan-limit check every pending query shares.
_require_bounding_limit = require_positive_limit


def latest_output_id(session: "Session", source_id: "UUID") -> int | None:
    """Return the newest conversion output id for ``source_id``, or ``None`` if absent.

    The newest output has the greatest id.
    """
    return session.exec(select(func.max(ConversionOutput.id)).where(ConversionOutput.source_id == source_id)).one()


def _newest_output_per_source() -> "Select":
    """Return one newest conversion output per source in reproducible order.

    The greatest output id wins within a source. Results use ``created_at`` and
    ``source_id`` as cross-source ordering keys.
    """
    winners = select(func.max(ConversionOutput.id).label("output_id")).group_by(ConversionOutput.source_id).subquery()
    return (
        select(
            ConversionOutput.id.label("output_id"),
            ConversionOutput.source_id,
            ConversionOutput.created_at,
        )
        .join(winners, ConversionOutput.id == winners.c.output_id)
        .order_by(ConversionOutput.created_at, ConversionOutput.source_id)
    )


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


def _pending_contextualization_query() -> "Select":
    """Return newest-per-source outputs without a contextualization work-unit."""
    winners = _newest_output_per_source().subquery()
    return (
        select(winners.c.output_id, winners.c.source_id)
        .outerjoin(ContextualizationJob, ContextualizationJob.conversion_output_id == winners.c.output_id)
        .where(ContextualizationJob.id.is_(None))
        .order_by(winners.c.created_at, winners.c.source_id)
    )


def pending_contextualization(session: "Session", *, limit: int | None = None) -> list[tuple[int, "UUID"]]:
    """Return pending conversion outputs in selection order.

    Only the newest output per source is considered. The result contains
    ``(conversion_output_id, source_id)`` pairs; a bound applies after pending
    filtering.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    _require_bounding_limit(limit)
    pending = _pending_contextualization_query()
    if limit is not None:
        pending = pending.limit(limit)
    return [(output_id, source_id) for output_id, source_id in session.exec(pending).all()]


def pending_contextualization_count(session: "Session") -> int:
    """Count sources pending contextualization."""
    return session.exec(select(func.count()).select_from(_pending_contextualization_query().subquery())).one()


def pending_contextualization_outputs(session: "Session", *, limit: int | None = None) -> list[int]:
    """Return pending conversion output ids in selection order."""
    return [output_id for output_id, _source_id in pending_contextualization(session, limit=limit)]


def pending_contextualization_sources(session: "Session", *, limit: int | None = None) -> list["UUID"]:
    """Return sources pending contextualization in selection order."""
    return [source_id for _output_id, source_id in pending_contextualization(session, limit=limit)]


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
