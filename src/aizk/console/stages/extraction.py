"""Console descriptor for the mention-extraction stage."""

from __future__ import annotations

from aizk.console.descriptors import StageAction
from aizk.console.stages._graph import build_graph_descriptor
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_run import stale_extraction_sources
from aizk.graph.extraction_workunit import pending_extraction_sources
from aizk.graph.job_actions import (
    apply_extraction_cancel,
    apply_extraction_readmission,
    apply_extraction_retry,
)

#: The extraction work-unit's stage coincides with exactly one underlying run stage.
_DRILLDOWN_STAGES = [(EXTRACTION_STAGE, "Mention Extraction")]

#: Re-extracting a source whose upstream has moved on beneath it. Offered only by
#: this stage: contextualization's work-unit is per conversion output, so a
#: re-converted source becomes pending again on its own and needs no operator action.
#: Eligibility (finished, and the source stale) lives in the domain helper, so a
#: bulk application over a mixed selection re-extracts what qualifies and reports
#: the rest skipped.
_READMISSION = StageAction(
    key="re-extract",
    applied_label="re-extracted",
    apply=lambda session, unit, _principal: apply_extraction_readmission(session, unit),
)

DESCRIPTOR = build_graph_descriptor(
    key="extraction",
    label="Extraction",
    model=ExtractionJob,
    events_stage=EXTRACTION_STAGE,
    drilldown_stages=_DRILLDOWN_STAGES,
    apply_retry=apply_extraction_retry,
    apply_cancel=apply_extraction_cancel,
    id_search_columns=[],
    extra_actions=[_READMISSION],
    # The stage's own derivations, read here rather than re-expressed: what an
    # operator sees behind is exactly what an admission pass would take up, and
    # what is marked stale is exactly what the re-extract action accepts.
    pending_sources=pending_extraction_sources,
    stale_sources=stale_extraction_sources,
)
