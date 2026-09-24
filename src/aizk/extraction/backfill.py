"""Run a bounded extraction backfill."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aizk.extraction.stage import EXTRACTION_DEFINITION, enqueue_extraction, enqueue_extraction_backfill
from aizk.graph.backfill import BackfillResult, run_backfill

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


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
