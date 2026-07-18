"""Console descriptor for the mention-extraction stage."""

from __future__ import annotations

from aizk.console.stages._graph import build_graph_descriptor
from aizk.graph.api.routes.extraction import _apply_cancel, _apply_retry
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import EXTRACTION_STAGE

#: The extraction work-unit's stage coincides with exactly one underlying run stage.
_DRILLDOWN_STAGES = [(EXTRACTION_STAGE, "Mention Extraction")]

DESCRIPTOR = build_graph_descriptor(
    key="extraction",
    label="Extraction",
    model=ExtractionJob,
    events_stage=EXTRACTION_STAGE,
    drilldown_stages=_DRILLDOWN_STAGES,
    apply_retry=_apply_retry,
    apply_cancel=_apply_cancel,
    id_search_columns=[],
)
