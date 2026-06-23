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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.workunit import enqueue_document

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlmodel import Session

    from aizk.graph.datamodel import ContextualizationJob


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
) -> list["ContextualizationJob"]:
    """Enqueue work-units for many conversion outputs (bulk/backfill mode).

    Each output is resolved and enqueued via :func:`enqueue_output`, so the same
    source-identity resolution and ``idempotency_key`` dedupe apply and the
    resulting units are identical to incremental enqueue — only volume and
    scheduling differ. Throttling and per-document commit batching are the
    caller's concern; this only stages the rows and does not commit.
    """
    return [enqueue_output(session, conversion_output_id) for conversion_output_id in conversion_output_ids]
