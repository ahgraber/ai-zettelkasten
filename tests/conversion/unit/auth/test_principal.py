"""Unit tests for the Principal auth abstraction."""

from pydantic import ValidationError
import pytest

from aizk.conversion.auth import Principal


def test_principal_constructs_and_round_trips_json():
    principal = Principal(subject="alice", provenance="trust_network")

    payload = principal.model_dump_json()
    rehydrated = Principal.model_validate_json(payload)

    assert rehydrated == principal
    assert rehydrated.subject == "alice"
    assert rehydrated.provenance == "trust_network"


def test_principal_is_frozen():
    """Mutation after construction must be rejected by pydantic's frozen config."""
    principal = Principal(subject="alice", provenance="trust_network")

    with pytest.raises(ValidationError):
        principal.subject = "mallory"  # type: ignore[misc]


def test_principal_rejects_unsupported_provenance():
    """Provenance is `Literal["trust_network"]`; any other value fails validation.

    Future auth modes (`token`, `proxy_headers`, `oidc`) widen the literal in
    a separate change; today they must be rejected at runtime.
    """
    with pytest.raises(ValidationError):
        Principal(subject="alice", provenance="token")  # type: ignore[arg-type]
