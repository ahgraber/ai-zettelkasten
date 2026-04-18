"""Tests for the SSRF-hardened HTTP GET helper.

Exercise every rejection path without hitting the network: DNS resolution
is monkeypatched, and httpx requests are served from an in-process
``httpx.MockTransport``. Hardened rejections (unsafe URL, body too
large) always non-retryable and surface with the typed error class.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import pytest

from aizk.conversion.utilities.safe_http import (
    BodyTooLargeError,
    UnsafeUrlError,
    safe_get,
)


# ---------------------------------------------------------------------------
# DNS stubs
# ---------------------------------------------------------------------------


def _fake_getaddrinfo(mapping: dict[str, str]):
    """Return a ``socket.getaddrinfo`` stand-in that resolves hosts from *mapping*."""

    def _stub(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"fake DNS: no entry for {host!r}")
        ip = mapping[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    return _stub


@pytest.fixture()
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``good.example.com`` to a public IP by default."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo({"good.example.com": "93.184.216.34"}),
    )


# ---------------------------------------------------------------------------
# Scheme allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["file", "gopher", "ftp", "ldap"])
def test_rejects_non_http_schemes(scheme: str) -> None:
    with pytest.raises(UnsafeUrlError, match="Scheme"):
        safe_get(f"{scheme}://good.example.com/x", timeout=1.0)


def test_rejects_http_by_default(public_dns) -> None:
    with pytest.raises(UnsafeUrlError, match="Scheme"):
        safe_get("http://good.example.com/x", timeout=1.0)


def test_allow_http_opt_in(public_dns) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    with patch("httpx.Client", _patched_client(_handler)):
        result = safe_get(
            "http://good.example.com/x", timeout=1.0, allow_http=True
        )
    assert result.content == b"ok"


# ---------------------------------------------------------------------------
# Private/loopback IP rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",  # AWS/GCP metadata service — canonical SSRF target
        "192.168.1.1",
        "172.16.0.1",
        "::1",
        "fc00::1",
    ],
)
def test_rejects_private_or_loopback_ips(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"metadata.internal": ip})
    )
    with pytest.raises(UnsafeUrlError, match="non-public IP"):
        safe_get("https://metadata.internal/latest", timeout=1.0)


def test_rejects_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
    with pytest.raises(UnsafeUrlError, match="DNS resolution failed"):
        safe_get("https://nowhere.invalid/x", timeout=1.0)


# ---------------------------------------------------------------------------
# Redirect validation
# ---------------------------------------------------------------------------


def test_rejects_redirect_to_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_getaddrinfo(
            {
                "good.example.com": "93.184.216.34",
                "metadata.internal": "169.254.169.254",
            }
        ),
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        # First hop sends us at the metadata service.
        return httpx.Response(
            302, headers={"location": "https://metadata.internal/secrets"}
        )

    with patch("httpx.Client", _patched_client(_handler)):
        with pytest.raises(UnsafeUrlError, match="non-public IP"):
            safe_get("https://good.example.com/x", timeout=1.0)


def test_rejects_https_to_http_downgrade_on_redirect(public_dns) -> None:
    """Even when ``allow_http=True`` admits HTTP, starting from HTTPS and
    redirecting down to HTTP is always refused — a downgrade-on-redirect is
    the SSRF-and-tampering vector this guard exists for."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://good.example.com/x"})

    with patch("httpx.Client", _patched_client(_handler)):
        with pytest.raises(UnsafeUrlError, match="HTTPS → HTTP downgrade"):
            safe_get("https://good.example.com/x", timeout=1.0, allow_http=True)


def test_redirect_cap_enforced(public_dns) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        # Infinite self-redirect.
        return httpx.Response(302, headers={"location": str(request.url)})

    with patch("httpx.Client", _patched_client(_handler)):
        with pytest.raises(UnsafeUrlError, match="Redirect cap"):
            safe_get("https://good.example.com/x", timeout=1.0, max_redirects=2)


# ---------------------------------------------------------------------------
# Body size caps
# ---------------------------------------------------------------------------


def test_rejects_oversized_content_length_pre_download(public_dns) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": "1000000"}, content=b"x" * 10
        )

    with patch("httpx.Client", _patched_client(_handler)):
        with pytest.raises(BodyTooLargeError, match="Content-Length"):
            safe_get("https://good.example.com/x", timeout=1.0, max_body_bytes=1024)


def test_rejects_oversized_streamed_body_without_content_length(public_dns) -> None:
    big = b"x" * 5000

    def _handler(request: httpx.Request) -> httpx.Response:
        # No Content-Length — simulates chunked / unknown-length responses.
        return httpx.Response(200, content=big)

    with patch("httpx.Client", _patched_client(_handler)):
        with pytest.raises(BodyTooLargeError, match="exceeds cap"):
            safe_get("https://good.example.com/x", timeout=1.0, max_body_bytes=1024)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_successful_get_returns_content_and_final_url(public_dns) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>ok</html>", headers={"content-type": "text/html"})

    with patch("httpx.Client", _patched_client(_handler)):
        result = safe_get("https://good.example.com/page", timeout=1.0)

    assert result.status_code == 200
    assert result.content == b"<html>ok</html>"
    assert result.headers["content-type"] == "text/html"
    assert result.final_url == "https://good.example.com/page"


def test_successful_get_follows_safe_redirect(public_dns) -> None:
    called: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        if request.url.path == "/first":
            return httpx.Response(
                302, headers={"location": "https://good.example.com/second"}
            )
        return httpx.Response(200, content=b"final")

    with patch("httpx.Client", _patched_client(_handler)):
        result = safe_get("https://good.example.com/first", timeout=1.0)

    assert result.content == b"final"
    assert result.final_url == "https://good.example.com/second"
    assert called == [
        "https://good.example.com/first",
        "https://good.example.com/second",
    ]


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _patched_client(handler):
    """Return a factory that produces ``httpx.Client`` instances using a MockTransport.

    The production code constructs ``httpx.Client(timeout=..., follow_redirects=...)``
    inside ``safe_get``; we intercept the class and hand back a client that
    routes every request through the supplied handler, so the tests exercise
    the real redirect and body-streaming paths.
    """
    # Capture the real class before it gets patched — the factory itself will
    # shadow ``httpx.Client`` in the ``safe_http`` module namespace.
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    return _factory
