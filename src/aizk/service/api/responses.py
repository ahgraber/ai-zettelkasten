"""HTTP responses shared by every stage's intake routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from fastapi import status
from fastapi.responses import JSONResponse


class QueueFullResponse(BaseModel):
    """Response schema when the job queue is at capacity."""

    detail: str
    retry_after: int


#: The capacity refusal, declared so a generated client knows to read the header
#: rather than discovering it at runtime. Each intake route adds its own 200 and
#: 404 to this, since both are returned from the handler body and neither is
#: inferable from the decorator's ``status_code``.
INTAKE_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {
        "model": QueueFullResponse,
        "description": "Stage is at capacity",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before resubmitting.",
                "schema": {"type": "integer"},
            }
        },
    },
}


def queue_full_response(retry_after_seconds: int) -> JSONResponse:
    """Return a 503 capacity response with ``Retry-After`` and the standard body."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Queue is at capacity", "retry_after": retry_after_seconds},
        headers={"Retry-After": str(retry_after_seconds)},
    )
