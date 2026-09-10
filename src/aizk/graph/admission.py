"""Declare the graph stages' admission adapters.

:func:`contextualization_adapter` and :func:`extraction_adapter` bind each
stage's pending-work query and enqueue to the shared
:class:`~aizk.pipeline.admission.AdmissionAdapter`; :func:`admission_adapter_for`
resolves one by stage name. The shared adapter, pass, loop, and selection bound
are re-exported here for import statements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.enqueue import enqueue_output, pending_contextualization_outputs
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_workunit import enqueue_extraction, pending_extraction_sources
from aizk.pipeline.admission import MAX_BULK_SELECTION, AdmissionAdapter, AdmissionLoop, run_admission_pass

if TYPE_CHECKING:
    from collections.abc import Callable

    from aizk.pipeline.admission import AdmissionConfig

__all__ = [
    "MAX_BULK_SELECTION",
    "AdmissionAdapter",
    "AdmissionLoop",
    "admission_adapter_for",
    "contextualization_adapter",
    "extraction_adapter",
    "run_admission_pass",
]


def contextualization_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the contextualization stage's admission over conversion outputs."""
    return AdmissionAdapter(
        stage=CONTEXTUALIZATION_STAGE,
        model=ContextualizationJob,
        pending_work=lambda session, limit: pending_contextualization_outputs(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth, principal: enqueue_output(
            session, key, queue_max_depth=queue_max_depth, origin="admission", principal=principal
        ),
        queue_max_depth=config.contextualization_queue_max_depth,
        enabled=config.admission_contextualization_enabled,
    )


def extraction_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the extraction stage's admission over chunked sources."""
    return AdmissionAdapter(
        stage=EXTRACTION_STAGE,
        model=ExtractionJob,
        pending_work=lambda session, limit: pending_extraction_sources(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth, principal: enqueue_extraction(
            session, source_id=key, queue_max_depth=queue_max_depth, origin="admission", principal=principal
        ),
        queue_max_depth=config.extraction_queue_max_depth,
        enabled=config.admission_extraction_enabled,
    )


_ADAPTER_FACTORIES: "dict[str, Callable[[AdmissionConfig], AdmissionAdapter]]" = {
    CONTEXTUALIZATION_STAGE: contextualization_adapter,
    EXTRACTION_STAGE: extraction_adapter,
}


def admission_adapter_for(stage: str, config: "AdmissionConfig") -> AdmissionAdapter | None:
    """Return the adapter for ``stage``, or ``None`` when it has no admission path.

    The adapter reflects ``config``'s capacity and enabled flag.
    """
    factory = _ADAPTER_FACTORIES.get(stage)
    return factory(config) if factory is not None else None
