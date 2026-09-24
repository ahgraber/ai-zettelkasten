"""Provide contextualization work-unit routes.

Submission, listing, retry, and cancellation use the graph work-unit domain and
co-commit lifecycle events with status changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aizk.contextualization.api.dependencies import get_admission_config, get_db_session
from aizk.contextualization.api.schemas import (
    ContextualizationJobList,
    ContextualizationJobResponse,
    ContextualizationSubmission,
)
from aizk.contextualization.core.intake import enqueue_output
from aizk.contextualization.job_actions import (
    apply_contextualization_cancel as _apply_cancel,
    apply_contextualization_retry as _apply_retry,
)
from aizk.contextualization.stage import CONTEXTUALIZATION_DEFINITION
from aizk.graph.api.stage_routes import stage_router
from aizk.service.api.responses import INTAKE_RESPONSES, queue_full_response
from aizk.service.auth import get_principal

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.graph.datamodel import ContextualizationJob
    from aizk.pipeline.events import CreationOrigin


def _enqueue(
    session: "Session",
    submission: ContextualizationSubmission,
    *,
    queue_max_depth: int,
    origin: "CreationOrigin",
    principal: str,
) -> "tuple[ContextualizationJob, bool]":
    """Enqueue or reuse the unit for the submitted conversion output."""
    return enqueue_output(
        session,
        submission.conversion_output_id,
        queue_max_depth=queue_max_depth,
        origin=origin,
        principal=principal,
    )


router = stage_router(
    CONTEXTUALIZATION_DEFINITION,
    prefix="/v1/contextualizations",
    tag="contextualizations",
    label="contextualization",
    submit_model=ContextualizationSubmission,
    response_model=ContextualizationJobResponse,
    list_model=ContextualizationJobList,
    enqueue=_enqueue,
    retry=_apply_retry,
    cancel=_apply_cancel,
    capacity_field="contextualization_queue_max_depth",
    submit_doc="""Submit one converted document for contextualization.

    Returns 201 for a new unit, 200 for an existing unit, 404 when the output is
    missing, and 503 when the stage is at capacity.
    """,
    submit_not_found_description="No such conversion output",
    principal_dependency=get_principal,
    session_dependency=get_db_session,
    admission_config_dependency=get_admission_config,
    intake_responses=INTAKE_RESPONSES,
    queue_full_response=queue_full_response,
    value_error_is_404=True,
)

_endpoints = {route.endpoint.__name__: route.endpoint for route in router.routes}
submit_job = _endpoints["submit_job"]
list_jobs = _endpoints["list_jobs"]
get_job = _endpoints["get_job"]
retry_job = _endpoints["retry_job"]
cancel_job = _endpoints["cancel_job"]
