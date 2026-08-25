"""Enqueue contextualization work-units from conversion outputs.

The graph work-unit's domain enqueue functions (:mod:`aizk.graph.workunit`) take
the durable ``source_id`` explicitly, keeping them decoupled from the conversion
stage. These wrappers are the conversion-coupled entry points: they resolve the
``source_id`` source identity from the conversion output
(``conversion_output_id → ConversionOutput.source_id``) once at enqueue, so it is
carried onto the work-unit's runs and transition events and a source's progress
stays resolvable across stages.

Both modes (incremental single enqueue, bulk/backfill) dedupe on the work-unit's
``idempotency_key`` via the underlying domain functions, and both honor the
stage's declared capacity (:mod:`aizk.graph.capacity`) — the single enqueue by
refusing, the bulk enqueue by truncating to the batch's headroom. They ``add`` /
``flush`` on the caller's session and never commit.

:func:`latest_output_ids_per_source` is the corpus-scan target selection a bulk
backfill enqueues over, and :func:`pending_contextualization_outputs` narrows the
same selection to the outputs that have no work-unit yet — the stage's
pending-work derivation. Both are kept here rather than in a caller so every
surface that scans the corpus for contextualization work resolves the same
target set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import func, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.capacity import within_headroom
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.workunit import enqueue_document
from aizk.pipeline.invalidation import require_reprocessing_confirmation

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from sqlalchemy import Select
    from sqlmodel import Session


def _require_bounding_limit(limit: int | None) -> None:
    """Reject a ``limit`` that would widen a scan instead of narrowing it.

    SQLite reads ``LIMIT -1`` as no limit, so a non-positive bound turns a
    bounded selection into a corpus-wide one.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit}")


def _newest_output_per_source() -> "Select":
    """Return the selection of each source's newest conversion output, in a reproducible order.

    Within a source, the newest output is the one with the greatest
    ``created_at``, with the greater ``id`` breaking a timestamp tie. Across
    sources the result is ordered by that output's ``created_at`` with
    ``source_id`` as the tiebreaker — a total order, so a ``limit`` sample stays
    reproducible even when several outputs share one ``created_at`` timestamp.

    Selects ``(output_id, source_id, created_at)`` so callers can bound it, or
    anti-join it against the work-unit table, without restating the tie-breaking.
    """
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
    return (
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


def latest_output_ids_per_source(session: "Session", *, limit: int | None = None) -> list[int]:
    """Return each source's newest conversion output id, in a reproducible order.

    A source accumulates a conversion output per conversion job, so a converter
    version bump leaves earlier outputs behind. Only the newest one describes the
    document as it stands, and contextualizing a superseded output spends a
    summary call plus a call per chunk on an artifact nothing reads — so the
    corpus scan selects one output per source rather than every historical one.

    Args:
        session: Active, read-only session.
        limit: Caps the selection to its first N sources when supplied. Must be
            one or more.

    Returns:
        The selected ``conversion_outputs.id`` values, in selection order.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    _require_bounding_limit(limit)
    winners = _newest_output_per_source()
    if limit is not None:
        winners = winners.limit(limit)
    return [row[0] for row in session.exec(winners).all()]


def pending_contextualization(session: "Session", *, limit: int | None = None) -> list[tuple[int, "UUID"]]:
    """Return the work that should have a contextualization work-unit but does not.

    A source is pending exactly when its newest conversion output has no
    work-unit. Anti-joining on the artifact locator rather than the source is
    what makes a re-converted source pending again: the new output is a distinct
    locator, so the work-unit that covered the previous one does not cover it.
    A source with no conversion output has nothing to contextualize and never
    appears.

    Derived from current artifact and work-unit state alone — nothing records
    that a previous evaluation saw a source, so work this evaluation leaves out
    is still pending for the next one.

    This is the stage's single pending-work derivation. Admission reads the
    output locators from it and the console reads the source identities, so what
    an operator is shown and what a pass would admit cannot disagree.

    Args:
        session: Active, read-only session.
        limit: Caps the result to its first N pending outputs when supplied. The
            bound applies after the anti-join, so a bounded evaluation returns
            pending work rather than a sample of the corpus that may hold none.
            Must be one or more.

    Returns:
        The pending ``(conversion_output_id, source_id)`` pairs, in selection order.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    _require_bounding_limit(limit)
    winners = _newest_output_per_source().subquery()
    pending = (
        select(winners.c.output_id, winners.c.source_id)
        .outerjoin(ContextualizationJob, ContextualizationJob.conversion_output_id == winners.c.output_id)
        .where(ContextualizationJob.id.is_(None))
        .order_by(winners.c.created_at, winners.c.source_id)
    )
    if limit is not None:
        pending = pending.limit(limit)
    return [(output_id, source_id) for output_id, source_id in session.exec(pending).all()]


def pending_contextualization_outputs(session: "Session", *, limit: int | None = None) -> list[int]:
    """Return the conversion outputs the stage owes a work-unit, in selection order.

    The admission-facing projection of :func:`pending_contextualization`: the
    enqueue primitive is keyed by the artifact locator, so that is what a pass
    consumes.
    """
    return [output_id for output_id, _source_id in pending_contextualization(session, limit=limit)]


def pending_contextualization_sources(session: "Session", *, limit: int | None = None) -> list["UUID"]:
    """Return the sources the stage owes a work-unit, in selection order.

    The operator-facing projection of :func:`pending_contextualization`: coverage
    is reported per source, since that is the thing an operator recognizes.
    """
    return [source_id for _output_id, source_id in pending_contextualization(session, limit=limit)]


def enqueue_output(
    session: "Session",
    conversion_output_id: int,
    *,
    queue_max_depth: int = 0,
) -> "ContextualizationJob":
    """Enqueue one document's work-unit, resolving its source identity from the output.

    Looks up the conversion output to resolve ``source_id``, then enqueues (or
    reuses, on ``idempotency_key``) the work-unit. Does not commit.

    Args:
        session: Active session; the caller owns commit/rollback.
        conversion_output_id: The conversion artifact locator to process.
        queue_max_depth: The stage's declared capacity over its actionable
            backlog; ``0`` (the default) declares no limit.

    Raises:
        ValueError: If no conversion output exists for ``conversion_output_id``.
        StageAtCapacityError: When the stage is at capacity and the request does
            not resolve to an existing work-unit.
    """
    output = session.get(ConversionOutput, conversion_output_id)
    if output is None:
        raise ValueError(f"conversion output {conversion_output_id} not found")
    return enqueue_document(
        session,
        conversion_output_id=conversion_output_id,
        source_id=output.source_id,
        queue_max_depth=queue_max_depth,
    )


def enqueue_backfill_outputs(
    session: "Session",
    conversion_output_ids: "Iterable[int]",
    *,
    confirmed: bool = False,
    queue_max_depth: int = 0,
) -> list["ContextualizationJob"]:
    """Enqueue work-units for many conversion outputs (bulk/backfill mode).

    Each output is resolved and enqueued via :func:`enqueue_output`, so the same
    source-identity resolution and ``idempotency_key`` dedupe apply and the
    resulting units are identical to incremental enqueue — only volume and
    scheduling differ. Throttling and per-document commit batching are the
    caller's concern; this only stages the rows and does not commit.

    Remaining capacity is read once for the batch and the input truncated to it,
    rather than counting the backlog per row. Outputs beyond the headroom are
    left unenqueued and remain pending for a later batch.

    A corpus-wide backfill has a large downstream blast radius, so it is gated
    behind explicit confirmation: nothing is enqueued unless ``confirmed`` is True.

    Args:
        session: Active session; the caller owns commit/rollback.
        conversion_output_ids: The conversion artifact locators to enqueue, in
            admission order.
        confirmed: Explicit human approval for the corpus-wide operation; when
            ``False`` (the default) nothing is enqueued and the gate raises.
        queue_max_depth: The stage's declared capacity over its actionable
            backlog; ``0`` (the default) declares no limit.

    Returns:
        The enqueued (or reused) work-units, one per admitted output.

    Raises:
        ReprocessingConfirmationError: When ``confirmed`` is ``False``.
    """
    require_reprocessing_confirmation("corpus-wide contextualization backfill", confirmed=confirmed)
    admitted = within_headroom(
        session,
        ContextualizationJob,
        conversion_output_ids,
        stage=CONTEXTUALIZATION_STAGE,
        limit=queue_max_depth,
    )
    return [enqueue_output(session, conversion_output_id) for conversion_output_id in admitted]
