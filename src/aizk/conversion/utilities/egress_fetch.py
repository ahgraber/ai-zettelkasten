"""Egress-validated HTTP fetch with manual redirect loop and header hygiene.

The single shared entry point for outbound HTTP from the conversion pipeline.
Every fetcher that issues HTTP traffic against external content SHALL go
through `egress_fetch_bytes` so that the four security properties hold
uniformly:

1. Pre-flight egress validation (`async_assert_egress_allowed`) — DNS-resolved
   target classified before any I/O.
2. Connection-pinned transport (`EgressPinnedTransport`) — TCP connect dials
   the validated IP, defeating DNS-rebinding TOCTOU.
3. Manual redirect loop with per-hop validation — every 3xx hop gets a fresh
   egress check; `https → http` downgrades are rejected; redirect chain
   capped at 5 hops with a 120 s wall-clock budget.
4. Header hygiene — `Authorization`, `Cookie`, and `X-*-Auth*` headers are
   stripped on cross-host redirects so credentials cannot leak to a
   redirect-controlled host.

See `.specs/changes/network-egress-policy/design.md` § "Manual redirect loop
with per-hop validation" and § "Connection pinning via custom httpx transport".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import io
import logging
import re
import time
from typing import Final
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

import httpx

from aizk.conversion.core.errors import (
    DenyListDestination,
    DisallowedScheme,
    FetchError,
    FetchTooLargeError,
    RedirectEgressViolation,
)
from aizk.conversion.utilities.egress import (
    ValidatedDestination,
    async_assert_egress_allowed,
)
from aizk.conversion.utilities.egress_transport import EgressPinnedTransport
from aizk.utilities.url_utils import sanitize_url_for_log

_DEFAULT_MAX_REDIRECTS: Final[int] = 5
_DEFAULT_TOTAL_BUDGET_SECONDS: Final[float] = 120.0
_REDIRECT_STATUS_CODES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

_AUTH_HEADER_NAMES: Final[frozenset[str]] = frozenset({"authorization", "cookie"})
_X_AUTH_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^x-.*auth.*$", re.IGNORECASE)

TransportFactory = Callable[[ValidatedDestination], httpx.AsyncBaseTransport]


@dataclass(frozen=True)
class _Terminal:
    """A terminal (non-3xx) response: body bytes and lowercase-keyed headers."""

    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class _Redirect:
    """A 3xx response carrying its raw `Location` header value."""

    location: str


def _default_timeout() -> httpx.Timeout:
    """Return the per-hop timeout pinned by the design (connect 5 s, read 30 s)."""
    return httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


def _default_transport_factory(destination: ValidatedDestination) -> httpx.AsyncBaseTransport:
    """Return an `EgressPinnedTransport` for the validated destination."""
    return EgressPinnedTransport(destination)


def _strip_auth_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `headers` with auth-bearing entries removed.

    Strips any `Authorization` or `Cookie` header (case-insensitive) and any
    header whose name matches `X-*-Auth*` per the design. Used on cross-host
    redirects so credentials do not leak to a redirect-controlled host.
    """
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _AUTH_HEADER_NAMES and not _X_AUTH_HEADER_PATTERN.match(name)
    }


async def _validate_for_hop(url: str, *, hop_index: int) -> ValidatedDestination:
    """Validate ``url`` against the egress policy for the given hop index.

    On hop 0 (initial request) the original `EgressPolicyError` propagates so
    callers see ``DenyListDestination`` / ``DisallowedScheme`` directly. On
    every later hop the violation is wrapped as ``RedirectEgressViolation``
    so retry classification distinguishes initial-target rejection from
    mid-chain redirect rejection.
    """
    try:
        return await async_assert_egress_allowed(url)
    except DenyListDestination:
        if hop_index == 0:
            raise
        logger.warning(
            "Egress denied: redirect hop target is in deny set",
            extra={"url": sanitize_url_for_log(url), "hop_index": hop_index},
        )
        raise RedirectEgressViolation(reason="deny_list") from None
    except DisallowedScheme:
        if hop_index == 0:
            raise
        logger.warning(
            "Egress denied: redirect hop target has disallowed scheme",
            extra={"url": sanitize_url_for_log(url), "hop_index": hop_index},
        )
        raise RedirectEgressViolation(reason="disallowed_scheme") from None


def _follow_redirect(
    location: str,
    current_url: str,
    current_headers: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    """Compute the next-hop URL and headers for a 3xx ``Location``.

    Resolves ``location`` against ``current_url`` (handling relative
    locations), rejects ``https → http`` scheme downgrades, and strips
    auth-bearing headers when the redirect crosses hosts.
    """
    next_url = urljoin(current_url, location)
    prev = urlparse(current_url)
    nxt = urlparse(next_url)

    if prev.scheme.lower() == "https" and nxt.scheme.lower() == "http":
        logger.warning(
            "Egress denied: https→http scheme downgrade on redirect",
            extra={
                "from_url": sanitize_url_for_log(current_url),
                "to_url": sanitize_url_for_log(next_url),
            },
        )
        raise RedirectEgressViolation(reason="scheme_downgrade")

    # Same-origin = same (scheme, hostname, port). Comparing only hostname would
    # forward Authorization/Cookie across same-host but different-port redirects
    # (e.g. https://host:443 → https://host:8443), which can cross a service
    # boundary on shared infrastructure.
    prev_origin = (prev.scheme.lower(), (prev.hostname or "").lower(), prev.port)
    next_origin = (nxt.scheme.lower(), (nxt.hostname or "").lower(), nxt.port)
    next_headers = dict(current_headers) if prev_origin == next_origin else _strip_auth_headers(current_headers)
    return next_url, next_headers


async def _read_capped_body(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    """Read response body up to ``max_bytes``, raising ``FetchTooLargeError`` on overrun.

    Performs a pre-flight ``Content-Length`` check (declared-too-large is
    rejected eagerly) and an incrementally-enforced size cap on the chunk
    iterator. The cap is the load-bearing check: ``Content-Length`` is
    never trusted alone, and the cap aborts mid-stream as soon as the
    running total exceeds ``max_bytes``.

    The body is accumulated into an in-memory buffer and returned as a
    ``bytes`` value bounded by ``max_bytes`` + one chunk; callers needing
    a true streaming output should iterate ``response.aiter_bytes`` directly.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            length = None
        if length is not None and length > max_bytes:
            safe = sanitize_url_for_log(url)
            logger.warning(
                "Egress fetch aborted: Content-Length exceeds configured cap",
                extra={"url": safe, "configured_cap_bytes": max_bytes, "declared_length_bytes": length},
            )
            raise FetchTooLargeError(f"Response from {safe!r} exceeds configured limit of {max_bytes} bytes")

    buffer = io.BytesIO()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            safe = sanitize_url_for_log(url)
            logger.warning(
                "Egress fetch aborted: streaming response body exceeds configured cap",
                extra={"url": safe, "configured_cap_bytes": max_bytes, "observed_size_bytes_bound": total},
            )
            raise FetchTooLargeError(f"Response from {safe!r} exceeds configured limit of {max_bytes} bytes")
        buffer.write(chunk)
    return buffer.getvalue()


async def _do_one_hop(
    url: str,
    headers: Mapping[str, str],
    destination: ValidatedDestination,
    factory: TransportFactory,
    timeout: httpx.Timeout,
    max_response_bytes: int,
) -> _Terminal | _Redirect:
    """Execute a single GET against ``url`` via the pinned transport.

    Returns ``_Terminal`` for a non-3xx response (with body bytes and
    lowercase-keyed headers) or ``_Redirect`` for a 3xx response (carrying
    the raw ``Location`` header value). Raises ``FetchError`` if the
    response is a 3xx without a ``Location`` header.
    """
    transport = factory(destination)
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        try:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if not location:
                        logger.warning(
                            "Egress fetch: 3xx response missing Location header",
                            extra={"url": sanitize_url_for_log(url), "status_code": response.status_code},
                        )
                        raise FetchError("3xx response missing Location header")
                    return _Redirect(location=location)
                response.raise_for_status()
                body = await _read_capped_body(response, url, max_response_bytes)
                lowered = {k.lower(): v for k, v in response.headers.items()}
                return _Terminal(body=body, headers=lowered)
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP error fetching {sanitize_url_for_log(url)!r}: {exc}") from exc


async def egress_fetch_bytes(
    url: str,
    *,
    max_response_bytes: int,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    total_budget_seconds: float = _DEFAULT_TOTAL_BUDGET_SECONDS,
    transport_factory: TransportFactory | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Fetch ``url`` bytes through the egress gate with a manual redirect loop.

    Args:
        url: The initial URL to fetch. Validated against the egress policy
            before any I/O is performed.
        max_response_bytes: Hard cap on the number of bytes read from the
            terminal response body. Streaming-enforced; ``Content-Length`` is
            also checked but never trusted alone.
        headers: Optional outbound HTTP headers. On a cross-host redirect,
            ``Authorization``, ``Cookie``, and ``X-*-Auth*`` headers are
            stripped before the next hop.
        timeout: Per-hop timeout. Defaults to ``connect=5 s, read=30 s``.
        max_redirects: Hard cap on redirect hops. A 3xx beyond this cap raises
            ``RedirectEgressViolation``.
        total_budget_seconds: Wall-clock budget for the full redirect chain.
        transport_factory: Override for the transport (test seam). Default
            constructs ``EgressPinnedTransport`` per validated destination.

    Returns:
        A ``(content_bytes, response_headers_lowercased)`` tuple from the
        terminal (non-3xx) response.

    Raises:
        DisallowedScheme, DenyListDestination, DnsTimeout: initial validation.
        RedirectEgressViolation: per-hop validation, scheme downgrade, or
            redirect-cap exhaustion.
        FetchTooLargeError: response body exceeds ``max_response_bytes``.
        FetchError: HTTP error from the terminal hop, missing ``Location``
            on a 3xx, or total-budget exhaustion.
    """
    factory = transport_factory or _default_transport_factory
    pinned_timeout = timeout or _default_timeout()
    current_url = url
    current_headers: dict[str, str] = dict(headers or {})
    deadline = time.monotonic() + total_budget_seconds

    for hop in range(max_redirects + 1):
        if time.monotonic() > deadline:
            logger.warning(
                "Egress fetch aborted: total redirect-chain budget exhausted",
                extra={"url": sanitize_url_for_log(url), "budget_seconds": total_budget_seconds, "hop": hop},
            )
            raise FetchError(f"Egress fetch exceeded total redirect budget of {total_budget_seconds}s")

        destination = await _validate_for_hop(current_url, hop_index=hop)
        result = await _do_one_hop(
            current_url,
            current_headers,
            destination,
            factory,
            pinned_timeout,
            max_response_bytes,
        )

        if isinstance(result, _Terminal):
            return result.body, result.headers

        # 3xx: enforce hop cap, then follow.
        if hop >= max_redirects:
            logger.warning(
                "Egress fetch aborted: redirect hop cap exhausted",
                extra={"url": sanitize_url_for_log(current_url), "max_redirects": max_redirects},
            )
            raise RedirectEgressViolation(reason="hop_cap")
        current_url, current_headers = _follow_redirect(result.location, current_url, current_headers)

    # Unreachable: every loop iteration either returns or raises.
    raise FetchError("Egress fetch terminated without producing a response")  # pragma: no cover


__all__ = ["egress_fetch_bytes"]
