"""Integration tests for the TrustedHostMiddleware wired into create_app()."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app


@pytest.fixture()
def _allowed_internal_only(monkeypatch):
    """Restrict the allowlist to a single non-loopback host."""
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["api.example.internal"]')


@pytest.fixture()
def _default_allowlist(monkeypatch):
    """Drop the conftest override so the shipped default `[localhost, 127.0.0.1]` applies."""
    monkeypatch.delenv("AIZK_TRUSTED_HOSTS", raising=False)


@pytest.fixture()
def _wildcard_internal(monkeypatch):
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["*.internal"]')


@pytest.fixture()
def _other_internal_only(monkeypatch):
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["other.example.internal"]')


def _client(base_url: str = "http://testserver") -> TestClient:
    """Build a TestClient with full lifespan against a fresh app instance.

    `base_url` controls the Host header that TestClient sends.
    """
    app = create_app()
    return TestClient(app, base_url=base_url)


@pytest.mark.usefixtures("_allowed_internal_only")
def test_request_to_allowed_host_succeeds():
    with _client(base_url="http://api.example.internal") as client:
        resp = client.get("/health/live")

    assert resp.status_code == 200


@pytest.mark.usefixtures("_allowed_internal_only")
def test_request_to_disallowed_host_returns_400():
    with _client(base_url="http://evil.example.com") as client:
        resp = client.get("/health/live")

    assert resp.status_code == 400
    assert resp.text == "Invalid host header"


@pytest.mark.usefixtures("_default_allowlist")
def test_default_allowlist_permits_localhost():
    with _client(base_url="http://localhost") as client:
        resp = client.get("/health/live")

    assert resp.status_code == 200


@pytest.mark.usefixtures("_default_allowlist")
def test_default_allowlist_permits_loopback_ip():
    with _client(base_url="http://127.0.0.1") as client:
        resp = client.get("/health/live")

    assert resp.status_code == 200


@pytest.mark.usefixtures("_other_internal_only")
def test_x_forwarded_host_is_not_consulted():
    """Even if X-Forwarded-Host names an allowed value, the actual Host must match."""
    with _client(base_url="http://api.example.internal") as client:
        resp = client.get(
            "/health/live",
            headers={"X-Forwarded-Host": "other.example.internal"},
        )

    assert resp.status_code == 400


@pytest.mark.usefixtures("_wildcard_internal")
def test_wildcard_subdomain_match():
    with _client(base_url="http://api.internal") as client:
        resp = client.get("/health/live")

    assert resp.status_code == 200
