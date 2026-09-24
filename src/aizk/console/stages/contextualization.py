"""Console descriptor for the contextualization stage."""

from __future__ import annotations

from aizk.console.stages._graph import build_graph_descriptor
from aizk.contextualization.events import CONTEXTUALIZATION_STAGE
from aizk.contextualization.job_actions import apply_contextualization_cancel, apply_contextualization_retry
from aizk.contextualization.stage import pending_contextualization_count, pending_contextualization_sources
from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.variants import SUMMARY_STAGE, VARIANT_STAGE

#: The contextualization work-unit's drill-down spans three underlying run stages,
#: in pipeline order.
_DRILLDOWN_STAGES = [
    (CHUNKING_STAGE, "Chunking"),
    (SUMMARY_STAGE, "Document Summary"),
    (VARIANT_STAGE, "Chunk Contextualization"),
]

DESCRIPTOR = build_graph_descriptor(
    key="contextualization",
    label="Contextualization",
    model=ContextualizationJob,
    events_stage=CONTEXTUALIZATION_STAGE,
    drilldown_stages=_DRILLDOWN_STAGES,
    apply_retry=apply_contextualization_retry,
    apply_cancel=apply_contextualization_cancel,
    id_search_columns=[ContextualizationJob.conversion_output_id],
    # The stage's own pending-work derivation, not a re-expression of it: what an
    # operator sees behind is exactly what an admission pass would take up. No
    # staleness derivation — a re-converted source becomes pending again on its own.
    pending_count=pending_contextualization_count,
    pending_sources=pending_contextualization_sources,
)
