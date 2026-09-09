"""Resolve who an inbound API request belongs to.

The module holds the deployment trust mode (:class:`AuthSettings`), the
resolved subject (:class:`Principal`), and the two request dependencies that
connect them. ``get_auth_settings`` sits here rather than beside the other
request dependencies because ``get_principal``'s ``Depends`` default is
evaluated at import; :mod:`aizk.service.api.dependencies` re-exports it.

At this build the only implemented auth mode is ``trust_network``: the API
treats every inbound request as authenticated and resolves a single
deployment-wide :class:`Principal` whose ``subject`` is
``AIZK_DEFAULT_PRINCIPAL``. That ``subject`` is what lands in the database as
``owner_id``; the ``provenance`` discriminator is request-scoped and is
intentionally NOT persisted. ``provenance`` widens in future changes
(``token``, ``proxy_headers``, ``oidc``) so exhaustive ``match`` statements in
the resolver fail to type-check until each new mode is handled.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi import Depends, Request

from aizk.utilities.errors import ConfigurationError


class AuthSettings(BaseSettings):
    """Deployment trust-mode configuration for the API.

    The `auth_mode` field reserves names for future modes (`token`,
    `proxy_headers`, `oidc`) at the type level so that exhaustive `match`
    statements in the principal resolver can be type-checked against the full
    eventual surface, while a runtime validator rejects any value that lacks
    an implemented resolver branch.
    """

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

    auth_mode: Literal["trust_network", "token", "proxy_headers", "oidc"] = "trust_network"
    default_principal: str = "self"

    _IMPLEMENTED_MODES: ClassVar[frozenset[str]] = frozenset({"trust_network"})

    @field_validator("auth_mode")
    @classmethod
    def _reject_unimplemented_modes(cls, value: str) -> str:
        """Reject auth modes whose resolver branch is not implemented in this build."""
        if value not in cls._IMPLEMENTED_MODES:
            raise ConfigurationError(
                f"auth mode '{value}' is reserved for a future build but not implemented at this cutover"
            )
        return value

    @field_validator("default_principal")
    @classmethod
    def _reject_blank_default_principal(cls, value: str) -> str:
        """Reject empty/whitespace `default_principal`.

        The value lands in `owner_id` for every backfilled and newly-minted row;
        a blank value would silently orphan rows from any non-blank principal
        and pass the migration's `WHERE owner_id IS NULL` post-check.
        """
        if not value or not value.strip():
            raise ConfigurationError("AIZK_DEFAULT_PRINCIPAL must be a non-empty, non-whitespace string")
        return value


class Principal(BaseModel):
    """The authenticated subject of an inbound API request.

    `subject` is the durable identifier persisted as `owner_id` on Source,
    Job, and Output rows. `provenance` is request-scoped and records which
    auth mode produced this Principal; it is intentionally NOT persisted.

    Frozen to prevent accidental mutation downstream of the resolver.
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    provenance: Literal["trust_network"]


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


__all__ = ["AuthSettings", "Principal", "get_auth_settings", "get_principal"]
