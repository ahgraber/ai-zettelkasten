"""Console descriptor for the contextualization stage."""

from __future__ import annotations

from aizk.console.stages._graph import build_graph_descriptor
from aizk.graph.api.routes import _apply_cancel, _apply_retry
from aizk.graph.contextualization import SUMMARY_STAGE, VARIANT_STAGE
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
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
    apply_retry=_apply_retry,
    apply_cancel=_apply_cancel,
    id_search_columns=[ContextualizationJob.conversion_output_id],
)
