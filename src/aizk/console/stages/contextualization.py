"""Console descriptor for the contextualization stage."""

from __future__ import annotations

from aizk.console.stages._graph import build_graph_descriptor
from aizk.graph.contextualization import SUMMARY_STAGE, VARIANT_STAGE
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.enqueue import pending_contextualization_sources
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.job_actions import apply_contextualization_cancel, apply_contextualization_retry
from aizk.graph.persistence import CHUNKING_STAGE

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
    # The stage's own pending-work derivation, read here rather than re-expressed:
    # what an operator sees behind is exactly what an admission pass would take up.
    # No staleness derivation — a re-converted source becomes pending again on its
    # own, so the stage has no notion of completed-but-behind work.
    pending_sources=pending_contextualization_sources,
)
