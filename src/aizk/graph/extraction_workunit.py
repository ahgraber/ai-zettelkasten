"""Enqueue extraction work-units for source identities.

Incremental and bulk paths deduplicate on ``idempotency_key`` and respect stage
capacity. Pending-work selection is read-only and shared by admission and
operator surfaces. Existing terminal units are reused; re-extraction requeues
the existing source unit through the operator action.
"""

from aizk.extraction.stage import (
    EXTRACTION_DEFINITION,
    ExtractionParams,
    enqueue_extraction,
    enqueue_extraction_backfill,
    pending_extraction_count,
    pending_extraction_sources,
)

__all__ = [
    "EXTRACTION_DEFINITION",
    "ExtractionParams",
    "enqueue_extraction",
    "enqueue_extraction_backfill",
    "pending_extraction_count",
    "pending_extraction_sources",
]
