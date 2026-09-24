"""Console descriptor for the mention-extraction stage."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from uuid import UUID

from aizk.console.descriptors import StageAction
from aizk.console.stages._graph import build_graph_descriptor
from aizk.extraction.core.currentness import classify_extraction_sources
from aizk.extraction.core.extractors import resolve_extraction_config
from aizk.extraction.events import EXTRACTION_STAGE
from aizk.extraction.settings import ExtractionConfig
from aizk.extraction.stage import pending_extraction_count, pending_extraction_sources
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.job_actions import (
    apply_extraction_cancel,
    apply_extraction_readmission,
    apply_extraction_retry,
)
from aizk.graph.mentions import ResolvedExtractionConfig

if TYPE_CHECKING:
    from sqlmodel import Session

#: The extraction work-unit's stage coincides with exactly one underlying run stage.
_DRILLDOWN_STAGES = [(EXTRACTION_STAGE, "Mention Extraction")]


@functools.cache
def _resolved_config() -> ResolvedExtractionConfig:
    """Resolve the process's extraction configuration once, lazily.

    Resolved only when a request needs it (never at import), so importing this
    module never reads ``.env``. Cached because it changes only when the
    process's environment does; a console test overrides this function to avoid
    reading the real environment entirely.
    """
    return resolve_extraction_config(ExtractionConfig())


def _currentness(session: "Session") -> dict[UUID, str]:
    """Classify every source's currentness under the process's resolved configuration, as plain strings."""
    return {
        source_id: value.value
        for source_id, value in classify_extraction_sources(session, resolved=_resolved_config()).items()
    }


#: Re-extracting a source whose upstream has moved on beneath it, or that needs
#: only reconciliation. Offered only by this stage: contextualization's
#: work-unit is per conversion output, so a re-converted source becomes pending
#: again on its own and needs no operator action. Eligibility lives in
#: :func:`~aizk.graph.job_actions.apply_extraction_readmission`.
_READMISSION = StageAction(
    key="re-extract",
    applied_label="re-extracted",
    apply=lambda session, unit, principal: apply_extraction_readmission(
        session, unit, resolved=_resolved_config(), principal=principal.subject
    ),
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
    # what is marked stale/needs-reconciliation is exactly what the re-extract
    # action accepts.
    pending_count=pending_extraction_count,
    pending_sources=pending_extraction_sources,
    currentness=_currentness,
)
