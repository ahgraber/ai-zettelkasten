"""FastAPI dependencies for database sessions, S3 clients, and request Principal.

The shared providers are re-exported from :mod:`aizk.service.api.dependencies`
and :mod:`aizk.service.auth` as the same objects, so a dependency override
keyed on either path resolves the same callable.
"""

from __future__ import annotations

from fastapi import Request

from aizk.conversion.wiring.capabilities import SubmissionCapabilities
from aizk.service.api.dependencies import get_config, get_db_session
from aizk.service.auth import get_auth_settings, get_principal
from aizk.storage.s3 import S3Client

__all__ = [
    "get_auth_settings",
    "get_config",
    "get_db_session",
    "get_principal",
    "get_s3_client",
    "get_submission_capabilities",
]


def get_s3_client(request: Request) -> S3Client:
    """Provide an S3Client configured from application state."""
    return S3Client(get_config(request))


def get_submission_capabilities(request: Request) -> SubmissionCapabilities:
    """Return the SubmissionCapabilities from application state."""
    return request.app.state.submission_capabilities
