"""Unit tests for FastAPI request-scoped dependencies."""

from __future__ import annotations

import pytest

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal
from aizk.conversion.utilities.config import AuthSettings


def _trust_network_settings(default_principal: str = "local") -> AuthSettings:
    return AuthSettings(_env_file=None, auth_mode="trust_network", default_principal=default_principal)


def test_get_principal_trust_network_returns_default_principal():
    settings = _trust_network_settings(default_principal="local")

    principal = get_principal(auth_settings=settings)

    assert principal == Principal(subject="local", provenance="trust_network")


def test_get_principal_does_not_consult_request_headers():
    """In trust_network mode, attacker-controlled auth headers must not influence resolution.

    `get_principal` does not take a `Request` parameter at all in this mode, so
    request headers are structurally inaccessible — this is the strongest form
    of "headers are not consulted." The test asserts the contract by passing
    only `auth_settings` and observing the subject equals `default_principal`.
    """
    settings = _trust_network_settings(default_principal="local")

    principal = get_principal(auth_settings=settings)

    assert principal.subject == "local"
    assert principal.provenance == "trust_network"


def test_get_principal_raises_for_unhandled_mode():
    """Bypass the AuthSettings validator to prove the resolver's `_` safety net works.

    In production the validator rejects unimplemented modes at startup, so the
    `_` arm is unreachable. `model_construct` skips validation, simulating the
    case where someone widens the `Literal` but forgets to add a resolver
    branch — the resolver must fail loudly rather than silently default-return.
    """
    settings = AuthSettings.model_construct(auth_mode="token", default_principal="local")

    with pytest.raises(NotImplementedError, match="has no resolver branch"):
        get_principal(auth_settings=settings)
