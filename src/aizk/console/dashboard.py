"""Console dashboard and app-root redirect.

The dashboard (``GET /ui``) renders every registered stage's work-unit counts,
each stage's native-status counts folded onto the generic lifecycle vocabulary so
the pipeline reads uniformly across stages. Where a stage declares a
``failed_split`` capability, its ``FAILED`` count is subdivided into units awaiting
an automatic retry and units that have exhausted retries. Where a stage declares
a pending-work or staleness derivation, its count of sources behind the stage is
shown beside the lifecycle columns — work the stage owes but has no unit for, and
work it finished against upstream state that has since moved on. The app root
(``/``) redirects here. Both sit behind the app's trusted-host perimeter and
resolve the same request :class:`~aizk.conversion.auth.Principal` the JSON APIs
require.
"""

from __future__ import annotations

from typing import Annotated, Any

from sqlmodel import Session

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from aizk.console.descriptors import registered_stages, rollup_counts
from aizk.console.rendering import TEMPLATES
import aizk.console.stages  # noqa: F401 -- import registers the stage descriptors
from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal
from aizk.graph.api.dependencies import get_db_session
from aizk.pipeline.lifecycle import WorkUnitStatus

#: The generic lifecycle categories the dashboard columns render, in order.
CATEGORIES: list[str] = [status.value for status in WorkUnitStatus]

router = APIRouter(tags=["console"])


def _stage_rows(session: Session, principal: Principal) -> list[dict[str, Any]]:
    """Build one dashboard row per registered stage from its principal-scoped counts.

    Each stage's native-status counts are folded onto the generic lifecycle
    vocabulary (so no unit is dropped or double-counted); a stage declaring a
    ``failed_split`` contributes the awaiting-retry / permanent subdivision of its
    ``FAILED`` count.

    A stage declaring a pending-work or staleness derivation also contributes that
    count, or ``None`` when it declares neither. Both counts sit outside the
    lifecycle rollup — neither counts a work-unit — so the per-stage total stays the
    number of the stage's work-units.
    """
    rows: list[dict[str, Any]] = []
    for descriptor in registered_stages():
        generic = rollup_counts(descriptor, descriptor.count_by_status(session, principal))
        awaiting_retry = permanent = None
        if descriptor.failed_split is not None:
            awaiting_retry, permanent = descriptor.failed_split(session, principal)
        rows.append(
            {
                "key": descriptor.key,
                "label": descriptor.label,
                "counts": {status.value: generic[status] for status in WorkUnitStatus},
                "total": sum(generic.values()),
                "failed_awaiting_retry": awaiting_retry,
                "failed_permanent": permanent,
                "pending": None if descriptor.pending_count is None else descriptor.pending_count(session, principal),
                "stale": None if descriptor.stale_count is None else descriptor.stale_count(session, principal),
            }
        )
    return rows


@router.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    """Redirect the app root to the operator console dashboard."""
    return RedirectResponse(url="/ui", status_code=307)


@router.get("/ui", include_in_schema=False)
def dashboard(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[Principal, Depends(get_principal)],
):
    """Render the per-stage work-unit dashboard."""
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {"stages": _stage_rows(session, principal), "categories": CATEGORIES},
    )
