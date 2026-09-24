"""Provide FastAPI dependencies for the graph operator API.

The shared providers are re-exported from
:mod:`aizk.service.api.dependencies` as the same objects, so a dependency
override keyed on either path resolves the same callable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from aizk.service.api.dependencies import get_db_session, get_object_storage

if TYPE_CHECKING:
    from aizk.graph.config import AdmissionConfig

__all__ = [
    "get_admission_config",
    "get_db_session",
    "get_object_storage",
]


def get_admission_config(request: Request) -> "AdmissionConfig":
    """Return the graph admission settings from application state."""
    return request.app.state.admission_config
