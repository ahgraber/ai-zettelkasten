"""Health check response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    """Result of an individual health check."""

    name: str
    status: Literal["ok", "unavailable"]
    detail: str | None = None


class HealthResponse(BaseModel):
    """Health endpoint response."""

    status: Literal["ok", "unavailable"]
    checks: list[CheckResult] = Field(default_factory=list)
