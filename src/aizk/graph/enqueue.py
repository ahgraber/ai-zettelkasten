"""Enqueue contextualization work-units from conversion outputs.

The helpers resolve source identities, deduplicate by ``idempotency_key``, and
respect stage capacity. They stage rows in the caller's transaction and never
commit. Pending-output selection is shared by backfill, admission, and operator
surfaces.
"""

from aizk.contextualization.core.intake import (
    enqueue_backfill_outputs,
    enqueue_output,
    latest_output_id,
    latest_output_ids_per_source,
)
from aizk.contextualization.stage import (
    pending_contextualization,
    pending_contextualization_count,
    pending_contextualization_outputs,
    pending_contextualization_sources,
)

__all__ = [
    "enqueue_backfill_outputs",
    "enqueue_output",
    "latest_output_id",
    "latest_output_ids_per_source",
    "pending_contextualization",
    "pending_contextualization_count",
    "pending_contextualization_outputs",
    "pending_contextualization_sources",
]
