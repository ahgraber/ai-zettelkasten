"""Provide extraction work-unit routes.

Submission, listing, retry, and cancellation use the extraction work-unit
domain and co-commit lifecycle events with status changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import select

from fastapi import HTTPException

from aizk.conversion.datamodel.source import Source
from aizk.graph.api.dependencies import get_admission_config, get_db_session
from aizk.graph.api.schemas import ExtractionJobList, ExtractionJobResponse, ExtractionSubmission
from aizk.graph.api.stage_routes import stage_router
from aizk.graph.extraction_workunit import EXTRACTION_DEFINITION, enqueue_extraction
from aizk.graph.job_actions import apply_extraction_cancel as _apply_cancel, apply_extraction_retry as _apply_retry
from aizk.service.api.responses import INTAKE_RESPONSES, queue_full_response
from aizk.service.auth import get_principal

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.graph.datamodel import ExtractionJob
    from aizk.pipeline.events import CreationOrigin


def _require_source(session: "Session", submission: ExtractionSubmission) -> None:
    """Raise 404 when the submitted source does not exist."""
    # Resolved on the durable ``source_id`` identity, not the table's row surrogate.
    known_source = session.exec(select(Source).where(Source.source_id == submission.source_id)).first()
    if known_source is None:
        session.rollback()
        raise HTTPException(status_code=404, detail=f"source {submission.source_id} not found")


def _enqueue(
    session: "Session",
    submission: ExtractionSubmission,
    *,
    queue_max_depth: int,
    origin: "CreationOrigin",
    principal: str,
) -> "tuple[ExtractionJob, bool]":
    """Enqueue or reuse the unit for the submitted source."""
    return enqueue_extraction(
        session,
        source_id=submission.source_id,
        queue_max_depth=queue_max_depth,
        origin=origin,
        principal=principal,
    )


router = stage_router(
    EXTRACTION_DEFINITION,
    prefix="/v1/extractions",
    tag="extractions",
    label="extraction",
    submit_model=ExtractionSubmission,
    response_model=ExtractionJobResponse,
    list_model=ExtractionJobList,
    enqueue=_enqueue,
    retry=_apply_retry,
    cancel=_apply_cancel,
    capacity_field="extraction_queue_max_depth",
    submit_doc="""Submit one source for entity-mention extraction.

    Returns 201 for a new unit, 200 for an existing unit, 404 when the source is
    missing, and 503 when the stage is at capacity.
    """,
    submit_not_found_description="No such source",
    principal_dependency=get_principal,
    session_dependency=get_db_session,
    admission_config_dependency=get_admission_config,
    intake_responses=INTAKE_RESPONSES,
    queue_full_response=queue_full_response,
    precheck=_require_source,
)

_endpoints = {route.endpoint.__name__: route.endpoint for route in router.routes}
submit_job = _endpoints["submit_job"]
list_jobs = _endpoints["list_jobs"]
get_job = _endpoints["get_job"]
retry_job = _endpoints["retry_job"]
cancel_job = _endpoints["cancel_job"]
