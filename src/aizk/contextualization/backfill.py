"""Run a bounded contextualization backfill."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aizk.contextualization.core.intake import enqueue_backfill_outputs, enqueue_output, latest_output_ids_per_source
from aizk.contextualization.stage import CONTEXTUALIZATION_DEFINITION
from aizk.graph.backfill import BackfillResult, run_backfill

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


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
    result = run_backfill(
        engine,
        CONTEXTUALIZATION_DEFINITION,
        targets=list(output_ids) if output_ids is not None else None,
        enqueue_one=lambda session, output_id: enqueue_output(
            session, output_id, queue_max_depth=queue_max_depth, origin="backfill", principal=principal
        ),
        enqueue_corpus=lambda session: enqueue_backfill_outputs(
            session,
            latest_output_ids_per_source(session, limit=limit),
            confirmed=confirmed or dry_run,
            queue_max_depth=queue_max_depth,
            principal=principal,
        ),
        dry_run=dry_run,
    )
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
