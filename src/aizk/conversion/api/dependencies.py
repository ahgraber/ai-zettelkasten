"""FastAPI dependencies for database sessions, S3 clients, and request Principal."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session

from fastapi import Depends, Request

from aizk.conversion.auth import Principal
from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import AuthSettings, ConversionConfig
from aizk.conversion.wiring.capabilities import SubmissionCapabilities
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine, get_session


def get_config(request: Request) -> ConversionConfig:
    """Return the shared config instance from application state."""
    return request.app.state.config


def get_auth_settings(request: Request) -> AuthSettings:
    """Return the shared AuthSettings instance from application state."""
    return request.app.state.auth_settings


def get_principal(auth_settings: AuthSettings = Depends(get_auth_settings)) -> Principal:  # noqa: B008
    """Resolve the Principal for the current request based on the active auth mode.

    In `trust_network` mode (the only mode implemented at this build), every
    request resolves to the same Principal: subject = `default_principal`,
    provenance = `"trust_network"`. The request body and auth-bearing headers
    are NOT consulted in this mode by design.

    The `_` arm exists as a safety net: the AuthSettings validator rejects
    unimplemented modes at startup, so this branch should be unreachable at
    runtime. Adding a new auth mode means widening the literal AND adding a
    case here — the safety net flags the omission loudly.
    """
    match auth_settings.auth_mode:
        case "trust_network":
            return Principal(subject=auth_settings.default_principal, provenance="trust_network")
        case _:
            raise NotImplementedError(f"auth mode {auth_settings.auth_mode!r} has no resolver branch")


def get_db_session() -> Iterator[Session]:
    """Provide a database session for request handling."""
    yield from get_session(get_engine(DatabaseConfig().database_url))


def get_s3_client(request: Request) -> S3Client:
    """Provide an S3Client configured from application state."""
    return S3Client(get_config(request))


def get_submission_capabilities(request: Request) -> SubmissionCapabilities:
    """Return the SubmissionCapabilities from application state."""
    return request.app.state.submission_capabilities
