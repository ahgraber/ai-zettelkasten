"""Console descriptor for the mention-extraction stage."""

from __future__ import annotations

from aizk.console.stages._graph import build_graph_descriptor
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.job_actions import apply_extraction_cancel, apply_extraction_retry

#: The extraction work-unit's stage coincides with exactly one underlying run stage.
_DRILLDOWN_STAGES = [(EXTRACTION_STAGE, "Mention Extraction")]

DESCRIPTOR = build_graph_descriptor(
    key="extraction",
    label="Extraction",
    model=ExtractionJob,
    events_stage=EXTRACTION_STAGE,
    drilldown_stages=_DRILLDOWN_STAGES,
    apply_retry=apply_extraction_retry,
    apply_cancel=apply_extraction_cancel,
    id_search_columns=[],
)
