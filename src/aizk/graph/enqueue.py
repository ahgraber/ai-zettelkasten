"""Enqueue contextualization work-units from conversion outputs.

The graph work-unit's domain enqueue functions (:mod:`aizk.graph.workunit`) take
the durable ``source_id`` explicitly, keeping them decoupled from the conversion
stage. These wrappers are the conversion-coupled entry points: they resolve the
``source_id`` source identity from the conversion output
(``conversion_output_id → ConversionOutput.source_id``) once at enqueue, so it is
carried onto the work-unit's runs and transition events and a source's progress
stays resolvable across stages.

Both modes (incremental single enqueue, bulk/backfill) dedupe on the work-unit's
``idempotency_key`` via the underlying domain functions. They ``add`` / ``flush``
on the caller's session and never commit.

:func:`latest_output_ids_per_source` is the corpus-scan target selection a bulk
backfill enqueues over, kept here rather than in a caller so every surface that
scans the corpus for contextualization work resolves the same target set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import func, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.workunit import enqueue_document
from aizk.pipeline.invalidation import require_reprocessing_confirmation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlmodel import Session

    from aizk.graph.datamodel import ContextualizationJob


def latest_output_ids_per_source(session: "Session", *, limit: int | None = None) -> list[int]:
    """Return each source's newest conversion output id, in a reproducible order.

    A source accumulates a conversion output per conversion job, so a converter
    version bump leaves earlier outputs behind. Only the newest one describes the
    document as it stands, and contextualizing a superseded output spends a
    summary call plus a call per chunk on an artifact nothing reads — so the
    corpus scan selects one output per source rather than every historical one.

    Within a source, the newest output is the one with the greatest
    ``created_at``, with the greater ``id`` breaking a timestamp tie. Across
    sources the result is ordered by that output's ``created_at`` with
    ``source_id`` as the tiebreaker — a total order, so a ``limit`` sample stays
    reproducible even when several outputs share one ``created_at`` timestamp.

    Args:
        session: Active, read-only session.
        limit: Caps the selection to its first N sources when supplied. Must be
            one or more; SQLite reads ``LIMIT -1`` as no limit, so a non-positive
            bound would widen the scan instead of narrowing it.

    Returns:
        The selected ``conversion_outputs.id`` values, in selection order.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit}")
    newest_per_source = (
        select(
            ConversionOutput.source_id,
            func.max(ConversionOutput.created_at).label("created_at"),
        )
        .group_by(ConversionOutput.source_id)
        .subquery()
    )
    # max(id) resolves a same-timestamp tie within a source; the outer ordering is
    # over the winning output's own created_at, then source_id.
    winners = (
        select(
            func.max(ConversionOutput.id).label("output_id"),
            ConversionOutput.source_id,
            ConversionOutput.created_at,
        )
        .join(
            newest_per_source,
            (ConversionOutput.source_id == newest_per_source.c.source_id)
            & (ConversionOutput.created_at == newest_per_source.c.created_at),
        )
        .group_by(ConversionOutput.source_id, ConversionOutput.created_at)
        .order_by(ConversionOutput.created_at, ConversionOutput.source_id)
    )
    if limit is not None:
        winners = winners.limit(limit)
    return [row[0] for row in session.exec(winners).all()]


def enqueue_output(session: "Session", conversion_output_id: int) -> "ContextualizationJob":
    """Enqueue one document's work-unit, resolving its source identity from the output.

    Looks up the conversion output to resolve ``source_id``, then enqueues (or
    reuses, on ``idempotency_key``) the work-unit. Does not commit.

    Raises:
        ValueError: If no conversion output exists for ``conversion_output_id``.
    """
    output = session.get(ConversionOutput, conversion_output_id)
    if output is None:
        raise ValueError(f"conversion output {conversion_output_id} not found")
    return enqueue_document(session, conversion_output_id=conversion_output_id, source_id=output.source_id)


def enqueue_backfill_outputs(
    session: "Session",
    conversion_output_ids: "Iterable[int]",
    *,
    confirmed: bool = False,
) -> list["ContextualizationJob"]:
    """Enqueue work-units for many conversion outputs (bulk/backfill mode).

    Each output is resolved and enqueued via :func:`enqueue_output`, so the same
    source-identity resolution and ``idempotency_key`` dedupe apply and the
    resulting units are identical to incremental enqueue — only volume and
    scheduling differ. Throttling and per-document commit batching are the
    caller's concern; this only stages the rows and does not commit.

    A corpus-wide backfill has a large downstream blast radius, so it is gated
    behind explicit confirmation: nothing is enqueued unless ``confirmed`` is True.

    Raises:
        ReprocessingConfirmationError: When ``confirmed`` is ``False``.
    """
    require_reprocessing_confirmation("corpus-wide contextualization backfill", confirmed=confirmed)
    return [enqueue_output(session, conversion_output_id) for conversion_output_id in conversion_output_ids]
