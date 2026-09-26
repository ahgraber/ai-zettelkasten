"""Provide FastAPI dependencies for the graph operator API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from aizk.pipeline.admission import AdmissionConfig

__all__ = [
    "get_admission_config",
]


def get_admission_config(request: Request) -> "AdmissionConfig":
    """Return the graph admission settings from application state."""
    return request.app.state.admission_config
