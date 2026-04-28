"""Unit tests for the connection-pinned httpx transport."""

from __future__ import annotations

import ssl
from typing import Any

import httpx
import pytest

from aizk.conversion.utilities.egress import ValidatedDestination
from aizk.conversion.utilities.egress_transport import (
    EgressPinnedTransport,
    _build_default_ssl_context,
)

_DEST = ValidatedDestination(
    ip="93.184.216.34",
    port=443,
    host="example.com",
    scheme="https",
)


class _CapturingTransport(EgressPinnedTransport):
    """Subclass that captures the request seen by `super().handle_async_request`.

    Returns a synthetic 200 OK without any network I/O so we can assert on the
    URL and extensions that the parent transport would have used.
    """

    def __init__(self, destination: ValidatedDestination, **kwargs: Any) -> None:
        super().__init__(destination, **kwargs)
        self.captured_url: httpx.URL | None = None
        self.captured_sni: str | None = None
        self.captured_host_header: str | None = None

    async def _parent_handle(self, request: httpx.Request) -> httpx.Response:  # noqa: ARG002
        return httpx.Response(200, request=request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_url = request.url
        original_host = original_url.host
        request.url = original_url.copy_with(host=self._destination.ip)
        request.extensions = {**request.extensions, "sni_hostname": original_host}
        # Snapshot what `super().handle_async_request` would observe.
        self.captured_url = request.url
        self.captured_sni = request.extensions.get("sni_hostname")
        self.captured_host_header = request.headers.get("host")
        try:
            return await self._parent_handle(request)
        finally:
            request.url = original_url


@pytest.mark.asyncio
async def test_transport_swaps_url_host_to_pinned_ip() -> None:
    transport = _CapturingTransport(_DEST)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://example.com/path")
    assert transport.captured_url is not None
    assert transport.captured_url.host == "93.184.216.34"
    # URL.port is None when the port matches the scheme default — exercise an
    # explicit non-default port to confirm the swap preserves the port too.
    transport2 = _CapturingTransport(
        ValidatedDestination(ip="93.184.216.34", port=8443, host="example.com", scheme="https")
    )
    async with httpx.AsyncClient(transport=transport2) as client:
        await client.get("https://example.com:8443/path")
    assert transport2.captured_url is not None
    assert transport2.captured_url.host == "93.184.216.34"
    assert transport2.captured_url.port == 8443


@pytest.mark.asyncio
async def test_transport_sets_sni_hostname_to_original_host() -> None:
    transport = _CapturingTransport(_DEST)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://example.com/path")
    assert transport.captured_sni == "example.com"


@pytest.mark.asyncio
async def test_transport_preserves_host_header_as_original() -> None:
    transport = _CapturingTransport(_DEST)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://example.com:443/path")
    # httpx auto-set the Host header at request build time; the URL swap inside
    # the transport must NOT change it (otherwise downstream would see the IP).
    assert transport.captured_host_header is not None
    assert "example.com" in transport.captured_host_header
    assert "93.184.216.34" not in transport.captured_host_header


@pytest.mark.asyncio
async def test_transport_restores_url_on_request_after_call() -> None:
    transport = _CapturingTransport(_DEST)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.com/path")
    # The response.request URL the caller sees must be the original target.
    assert response.request.url.host == "example.com"


def test_transport_rejects_verify_false() -> None:
    with pytest.raises(ValueError, match="verify=False"):
        EgressPinnedTransport(_DEST, verify=False)


def test_transport_rejects_unverified_ssl_context() -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verification disabled"):
        EgressPinnedTransport(_DEST, ssl_context=ctx)


def test_default_ssl_context_pins_tls_minimum() -> None:
    ctx = _build_default_ssl_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_transport_constructs_with_default_ssl_context() -> None:
    # Smoke test: verify the constructor runs cleanly with default args.
    transport = EgressPinnedTransport(_DEST)
    assert transport._destination is _DEST  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Real transport — exercise the actual `EgressPinnedTransport.handle_async_request`
# (not a subclass override) by stubbing only the parent class's method. This
# verifies the real swap-then-restore path including the `finally` block that
# restores `request.url` and `request.extensions` (M8 from the security review).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_transport_dial_url_carries_pinned_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real `EgressPinnedTransport.handle_async_request` rewrites URL host to the pinned IP.

    Stubs `httpx.AsyncHTTPTransport.handle_async_request` (the parent class's
    method) so the real `EgressPinnedTransport.handle_async_request` runs end
    to end including the `finally` restoration. Captures what the parent
    transport would have dialed.
    """
    captured: dict[str, Any] = {}

    async def fake_parent(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        captured["url_host"] = request.url.host
        captured["url_port"] = request.url.port
        captured["sni_hostname"] = request.extensions.get("sni_hostname")
        captured["host_header"] = request.headers.get("host")
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent)

    transport = EgressPinnedTransport(_DEST)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://example.com/path")

    # The parent transport (which would open the real socket) saw the IP, not
    # the original hostname — defeats DNS-rebinding TOCTOU.
    assert captured["url_host"] == "93.184.216.34"
    # SNI extension carries the original hostname so TLS verification works.
    assert captured["sni_hostname"] == "example.com"
    # Host header was set at request build time and was NOT swapped.
    assert captured["host_header"] is not None
    assert "example.com" in captured["host_header"]
    assert "93.184.216.34" not in captured["host_header"]
    # Restoration: the response.request URL the CALLER sees is the original.
    assert response.request.url.host == "example.com"


@pytest.mark.asyncio
async def test_real_transport_restores_extensions_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the parent transport raises, request URL and extensions are still restored.

    Regression for M8: prior to the Stage A fix, the `finally` restored
    `request.url` but not `request.extensions`. This test pins both halves
    of the restoration even on the exception path.
    """
    seen: dict[str, Any] = {}

    async def raising_parent(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        # Snapshot what the parent saw, then raise.
        seen["mid_url_host"] = request.url.host
        seen["mid_sni"] = request.extensions.get("sni_hostname")
        raise httpx.ConnectError("simulated connect failure")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", raising_parent)

    transport = EgressPinnedTransport(_DEST)
    request = httpx.Request("GET", "https://example.com/path")
    pre_url = request.url
    pre_extensions = request.extensions

    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(request)

    # During the call, the parent saw the swapped URL and added sni_hostname.
    assert seen["mid_url_host"] == "93.184.216.34"
    assert seen["mid_sni"] == "example.com"
    # After the call, the request object is back to its pre-call state on
    # both URL and extensions — so callers (and any retry layer) cannot see
    # the leftover swap.
    assert request.url == pre_url
    assert request.extensions == pre_extensions
