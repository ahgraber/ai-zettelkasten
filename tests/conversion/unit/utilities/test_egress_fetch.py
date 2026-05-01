"""Unit tests for the egress-validated HTTP fetch helper."""

from __future__ import annotations

import socket
from typing import Any, Callable

import httpx
import pytest

from aizk.conversion.core.errors import (
    DenyListDestination,
    FetchError,
    FetchTooLargeError,
    RedirectEgressViolation,
)
from aizk.conversion.utilities.egress import ValidatedDestination
from aizk.conversion.utilities.egress_fetch import (
    _strip_auth_headers,
    egress_fetch_bytes,
)


def _stub_dns_returning(monkeypatch: pytest.MonkeyPatch, host_to_ip: dict[str, str]) -> None:
    """Override `socket.getaddrinfo` so each host resolves to the listed IP."""

    def fake(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        ip = host_to_ip.get(host, "8.8.8.8")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _mock_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[ValidatedDestination], httpx.AsyncBaseTransport]:
    """Return a transport factory that delegates to `httpx.MockTransport`.

    Captures the request the helper would have sent so tests can assert
    against URLs, headers, etc., without any real network.
    """

    def factory(_destination: ValidatedDestination) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(handler)

    return factory


# --- Header stripping --------------------------------------------------------


def test_strip_auth_headers_removes_authorization_and_cookie() -> None:
    headers = {
        "Authorization": "Bearer secret",
        "Cookie": "session=abc",
        "X-Internal-Auth-Token": "internal",
        "User-Agent": "aizk-test",
        "Accept": "*/*",
    }
    stripped = _strip_auth_headers(headers)
    assert "Authorization" not in stripped
    assert "Cookie" not in stripped
    assert "X-Internal-Auth-Token" not in stripped
    assert stripped["User-Agent"] == "aizk-test"
    assert stripped["Accept"] == "*/*"


def test_strip_auth_headers_is_case_insensitive() -> None:
    headers = {"AUTHORIZATION": "x", "cookie": "y", "x-foo-AUTH-bar": "z"}
    stripped = _strip_auth_headers(headers)
    assert stripped == {}


@pytest.mark.parametrize(
    "header_name",
    [
        "X-Auth-Token",  # OpenStack / Akamai
        "X-Auth-Key",  # Cloudflare
        "X-Auth-User",
        "X-Auth-Email",
        "X-Auth",  # bare
        "x-auth-token",  # case sanity
    ],
)
def test_strip_auth_headers_removes_canonical_x_auth_variants(header_name: str) -> None:
    # Regression for the `^x-.*-auth.*$` miss: the original pattern required
    # `-auth` to appear after a hyphen-separated middle component, so canonical
    # X-Auth-* credential headers leaked across cross-host redirects.
    headers = {header_name: "secret", "User-Agent": "aizk-test"}
    stripped = _strip_auth_headers(headers)
    assert header_name not in stripped
    assert stripped["User-Agent"] == "aizk-test"


# --- Happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_egress_fetch_returns_body_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"example.com": "8.8.8.8"})
    payload = b"<!doctype html><html><body>ok</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=payload)

    body, response_headers = await egress_fetch_bytes(
        "https://example.com/page",
        max_response_bytes=10_000,
        transport_factory=_mock_factory(handler),
    )
    assert body == payload
    assert response_headers["content-type"] == "text/html"


# --- Initial-hop validation --------------------------------------------------


@pytest.mark.asyncio
async def test_initial_url_in_deny_set_raises_deny_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"metadata.local": "169.254.169.254"})
    fired = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fired["calls"] += 1
        return httpx.Response(200)

    with pytest.raises(DenyListDestination):
        await egress_fetch_bytes(
            "http://metadata.local/latest/meta-data/",
            max_response_bytes=10_000,
            transport_factory=_mock_factory(handler),
        )
    # Initial validation rejected — no transport call.
    assert fired["calls"] == 0


# --- Redirect handling: deny set ---------------------------------------------


@pytest.mark.asyncio
async def test_redirect_to_deny_set_raises_violation_and_does_not_fetch_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns_returning(
        monkeypatch,
        {"public.example.com": "93.184.216.34", "metadata.local": "169.254.169.254"},
    )
    fetched_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # The transport sees the URL with the IP swapped in by EgressPinnedTransport,
        # but with MockTransport in tests there's no swap — record the URL as-is.
        fetched_urls.append(str(request.url))
        if "public.example.com" in str(request.url):
            # Use https so the rejection is purely deny-list, not scheme_downgrade.
            return httpx.Response(302, headers={"location": "https://metadata.local/secret"})
        return httpx.Response(200, content=b"PRIVATE")

    with pytest.raises(RedirectEgressViolation) as excinfo:
        await egress_fetch_bytes(
            "https://public.example.com/start",
            max_response_bytes=10_000,
            transport_factory=_mock_factory(handler),
        )
    assert excinfo.value.reason == "deny_list"
    # Only the first hop was issued; the redirect target was never fetched.
    assert len(fetched_urls) == 1
    assert "public.example.com" in fetched_urls[0]


# --- Redirect handling: scheme downgrade -------------------------------------


@pytest.mark.asyncio
async def test_https_to_http_redirect_rejected_as_scheme_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://a.example/insecure"})

    with pytest.raises(RedirectEgressViolation) as excinfo:
        await egress_fetch_bytes(
            "https://a.example/start",
            max_response_bytes=10_000,
            transport_factory=_mock_factory(handler),
        )
    assert excinfo.value.reason == "scheme_downgrade"


# --- Redirect handling: hop cap ----------------------------------------------


@pytest.mark.asyncio
async def test_sixth_redirect_hop_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        # Return 302 forever — the helper should reject before issuing a 7th request.
        return httpx.Response(302, headers={"location": f"https://a.example/hop{counter['n']}"})

    with pytest.raises(RedirectEgressViolation) as excinfo:
        await egress_fetch_bytes(
            "https://a.example/start",
            max_response_bytes=10_000,
            transport_factory=_mock_factory(handler),
        )
    # max_redirects=5 → 6 GETs total (initial + 5 redirects), then the helper
    # rejects the 7th before issuing it.
    assert counter["n"] == 6
    # Hop-cap exhaustion has its own discriminating `reason` distinct from
    # `deny_list` so dashboards do not conflate the two failure modes.
    assert excinfo.value.reason == "hop_cap"


# --- Redirect handling: multi-hop scheme downgrade ---------------------------


@pytest.mark.asyncio
async def test_https_https_http_three_hop_downgrade_rejected_at_third_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3-hop chain `https://A → https://B → http://C` rejects at the third hop.

    The 2-hop downgrade case is covered by
    `test_https_to_http_redirect_rejected_as_scheme_downgrade`. This test
    pins the per-hop guarantee explicitly: each redirect's scheme is
    re-validated against the *previous* hop's scheme, not against the
    initial URL. So an `https → https → http` chain still trips the
    downgrade rule even though the initial URL was https.
    """
    _stub_dns_returning(
        monkeypatch,
        {"a.example": "93.184.216.34", "b.example": "93.184.216.35", "c.example": "93.184.216.36"},
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "a.example" in url:
            return httpx.Response(302, headers={"location": "https://b.example/two"})
        if "b.example" in url:
            return httpx.Response(302, headers={"location": "http://c.example/three"})
        return httpx.Response(200, content=b"unexpected")

    with pytest.raises(RedirectEgressViolation) as excinfo:
        await egress_fetch_bytes(
            "https://a.example/start",
            max_response_bytes=10_000,
            transport_factory=_mock_factory(handler),
        )
    assert excinfo.value.reason == "scheme_downgrade"
    # Hops issued: A (https) and B (https). C (http) was never connected to.
    assert len(seen) == 2
    assert all("c.example" not in u for u in seen)


# --- Redirect handling: relative Location ------------------------------------


@pytest.mark.asyncio
async def test_relative_location_resolves_against_previous_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, content=b"FINAL")

    body, _ = await egress_fetch_bytes(
        "https://a.example/start",
        max_response_bytes=10_000,
        transport_factory=_mock_factory(handler),
    )
    assert body == b"FINAL"
    assert seen[0].endswith("/start")
    assert seen[1].endswith("/next")
    assert seen[1].startswith("https://a.example")


# --- Redirect handling: header hygiene ---------------------------------------


@pytest.mark.asyncio
async def test_cross_host_redirect_strips_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(
        monkeypatch,
        {"a.example": "93.184.216.34", "b.example": "1.1.1.1"},
    )
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers.items()))
        if "a.example" in str(request.url):
            return httpx.Response(302, headers={"location": "https://b.example/secrets"})
        return httpx.Response(200, content=b"OK")

    await egress_fetch_bytes(
        "https://a.example/start",
        max_response_bytes=10_000,
        headers={"Authorization": "Bearer secret-token", "User-Agent": "aizk-test"},
        transport_factory=_mock_factory(handler),
    )
    # Hop 1 (same host as request) carries the auth.
    assert "Bearer secret-token" in captured[0].get("authorization", "")
    # Hop 2 (cross-host redirect) does NOT carry the auth.
    assert "authorization" not in {k.lower() for k in captured[1]}
    assert captured[1].get("user-agent") == "aizk-test"


@pytest.mark.asyncio
async def test_same_host_redirect_preserves_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers.items()))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://a.example/next"})
        return httpx.Response(200, content=b"OK")

    await egress_fetch_bytes(
        "https://a.example/start",
        max_response_bytes=10_000,
        headers={"Authorization": "Bearer secret"},
        transport_factory=_mock_factory(handler),
    )
    assert "Bearer secret" in captured[0]["authorization"]
    assert "Bearer secret" in captured[1]["authorization"]


# --- Body size enforcement ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_exceeding_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    with pytest.raises(FetchTooLargeError):
        await egress_fetch_bytes(
            "https://a.example/big",
            max_response_bytes=10,
            transport_factory=_mock_factory(handler),
        )


@pytest.mark.asyncio
async def test_declared_content_length_over_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        # Lie about content-length but actually return shorter body — the
        # declared-length pre-check should reject before we read.
        return httpx.Response(200, headers={"content-length": "1000000"}, content=b"short")

    with pytest.raises(FetchTooLargeError):
        await egress_fetch_bytes(
            "https://a.example/x",
            max_response_bytes=100,
            transport_factory=_mock_factory(handler),
        )


# --- Misc -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_3xx_without_location_raises_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(FetchError, match="missing Location"):
        await egress_fetch_bytes(
            "https://a.example/x",
            max_response_bytes=100,
            transport_factory=_mock_factory(handler),
        )


@pytest.mark.asyncio
async def test_non_2xx_terminal_raises_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns_returning(monkeypatch, {"a.example": "93.184.216.34"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(FetchError):
        await egress_fetch_bytes(
            "https://a.example/x",
            max_response_bytes=100,
            transport_factory=_mock_factory(handler),
        )


@pytest.mark.asyncio
async def test_dns_rebinding_scenario_uses_captured_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS rebinding: 1st resolution returns public IP, 2nd would return private.

    The helper validates the URL once per hop (returning the public IP), then
    hands the captured ``ValidatedDestination`` to the transport. Even if a
    later resolution would return a deny-set IP, the transport pins to the
    earlier public IP so the connection cannot be hijacked into the deny set.
    The egress helper does NOT issue a second getaddrinfo for the connection
    itself when running through ``EgressPinnedTransport``.

    This test exercises the contract: validation runs once, the captured
    `destination` carries the validated IP, and the transport receives it.
    """
    call_count = {"n": 0}
    captured_ips: list[str] = []

    def fake(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        call_count["n"] += 1
        # First resolution: public IP (passes egress).
        # Subsequent resolutions: private IP (would fail if revalidated).
        ip = "8.8.8.8" if call_count["n"] == 1 else "10.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"OK")

    def capturing_factory(destination: ValidatedDestination) -> httpx.AsyncBaseTransport:
        captured_ips.append(destination.ip)
        return httpx.MockTransport(handler)

    body, _ = await egress_fetch_bytes(
        "https://attacker.example/start",
        max_response_bytes=100,
        transport_factory=capturing_factory,
    )
    assert body == b"OK"
    # The transport received the IP that egress validation captured (the
    # public one), not whatever DNS would return on a later resolution.
    assert captured_ips == ["8.8.8.8"]
    # Egress validation hit DNS exactly once for this single-hop fetch.
    assert call_count["n"] == 1
