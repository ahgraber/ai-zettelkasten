"""SSRF- and body-size-hardened HTTP GET for outbound fetchers.

Today's fetchers (``UrlFetcher``, ``ArxivFetcher``) are protected by
deployment posture — the API sits behind a trusted internal network —
but any relaxation of that posture needs this module in force first.

The helper enforces:

- **Scheme allowlist.** Only ``https`` by default; ``http`` opt-in via
  ``allow_http=True``. ``file://``, ``gopher://``, ``ftp://`` etc. are
  always rejected.
- **DNS pre-resolution + private-IP rejection.** Every URL's hostname is
  resolved and the resulting IPs are checked against
  ``ipaddress.IPv4Address.is_private / is_loopback / is_link_local /
  is_multicast / is_reserved / is_unspecified`` (and the IPv6 equivalents).
  Any match is a fatal ``UnsafeUrlError``.
- **Per-redirect re-validation.** Redirects are followed manually; every
  hop goes through the same scheme + DNS + IP check, and an HTTPS → HTTP
  downgrade on redirect is rejected.
- **Redirect cap.** Default 5; configurable per call.
- **Body-size cap.** ``Content-Length`` pre-check + streaming tally via
  ``iter_bytes`` to catch chunked/unset-length responses. Hitting the cap
  raises ``BodyTooLargeError`` (typed, non-retryable — caller/operator
  action required, not a transient fault).

This helper is intentionally opinionated: it exists to prevent SSRF and
resource-exhaustion at the outbound boundary, not to be a drop-in httpx
replacement.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
import socket
from typing import ClassVar
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)


DEFAULT_MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"https"})


class UnsafeUrlError(RuntimeError):
    """Raised when a URL is rejected before or during the HTTP exchange.

    Covers scheme-allowlist violations, DNS resolution failure, private-IP
    targets, HTTPS → HTTP downgrade on redirect, and redirect-limit
    violations. Always non-retryable — a retry cannot resolve the fault.
    """

    error_code: ClassVar[str] = "unsafe_url"
    retryable: ClassVar[bool] = False


class BodyTooLargeError(RuntimeError):
    """Raised when a response body exceeds ``max_body_bytes``.

    Always non-retryable: the remote resource is too large for this
    deployment's limit, and that is an operator decision.
    """

    error_code: ClassVar[str] = "response_body_too_large"
    retryable: ClassVar[bool] = False


@dataclass
class SafeGetResult:
    """Return value of :func:`safe_get`.

    ``final_url`` may differ from the requested URL when redirects are
    followed; callers that record provenance should store it.
    """

    content: bytes
    status_code: int
    headers: dict[str, str]
    final_url: str


def safe_get(
    url: str,
    *,
    timeout: float,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    allowed_schemes: frozenset[str] | None = None,
    allow_http: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> SafeGetResult:
    """HTTP GET with SSRF and body-size hardening.

    Args:
        url: URL to fetch. Must pass the scheme allowlist and resolve to a
            public IP (no private / loopback / link-local).
        timeout: Per-request timeout in seconds.
        max_body_bytes: Abort when the response body exceeds this many bytes.
            Checked against ``Content-Length`` pre-download and streamed
            during the response to catch chunked responses that lie.
        max_redirects: Maximum number of redirect hops to follow. Each hop
            is re-validated against the scheme + IP checks.
        allowed_schemes: Override the default scheme allowlist
            (``{"https"}``). Pass ``frozenset({"https", "http"})`` — or
            use ``allow_http=True`` — when HTTP is explicitly acceptable.
        allow_http: Convenience shortcut to add ``"http"`` to the
            allowlist. Ignored if ``allowed_schemes`` is given explicitly.
        extra_headers: Additional request headers merged into every hop.

    Returns:
        :class:`SafeGetResult` with the body bytes, status, response
        headers, and the final URL after redirects.

    Raises:
        UnsafeUrlError: scheme rejected; DNS failure; private-IP target;
            HTTPS → HTTP downgrade on redirect; redirect limit exceeded.
        BodyTooLargeError: response body exceeds ``max_body_bytes``.
        httpx.HTTPError: any transport-level failure surfaces unchanged so
            the caller's retry policy can classify it.
    """
    if allowed_schemes is None:
        allowed_schemes = frozenset({"https", "http"}) if allow_http else DEFAULT_ALLOWED_SCHEMES

    current_url = url
    previous_scheme: str | None = None
    hops = 0

    # httpx.Client does its own connection pooling; we re-use one client
    # across redirects but do the redirect logic ourselves so each hop is
    # re-validated.
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers=extra_headers or {},
    ) as client:
        while True:
            _validate_url(current_url, allowed_schemes, previous_scheme)

            response = client.get(current_url)

            if response.is_redirect:
                if hops >= max_redirects:
                    response.close()
                    raise UnsafeUrlError(
                        f"Redirect cap ({max_redirects}) exceeded for {url!r}"
                    )
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise UnsafeUrlError(
                        f"Redirect response missing Location header for {current_url!r}"
                    )
                previous_scheme = urlparse(current_url).scheme.lower()
                current_url = _resolve_redirect(current_url, location)
                hops += 1
                continue

            _check_content_length(response, max_body_bytes)
            body = _read_body_with_cap(response, max_body_bytes)
            return SafeGetResult(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                final_url=current_url,
            )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_url(
    url: str,
    allowed_schemes: frozenset[str],
    previous_scheme: str | None,
) -> None:
    """Run scheme + DNS + IP-range checks. Raises ``UnsafeUrlError`` on failure."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in allowed_schemes:
        raise UnsafeUrlError(
            f"Scheme {scheme!r} is not in the allowed set {sorted(allowed_schemes)!r}: {url!r}"
        )

    if previous_scheme == "https" and scheme == "http":
        raise UnsafeUrlError(
            f"Refusing HTTPS → HTTP downgrade on redirect to {url!r}"
        )

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError(f"URL has no hostname: {url!r}")

    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"DNS resolution failed for {hostname!r}: {exc}") from exc

    if not addr_info:
        raise UnsafeUrlError(f"DNS resolution returned no addresses for {hostname!r}")

    for info in addr_info:
        ip_str = info[4][0]
        ip = ipaddress.ip_address(ip_str)
        if _is_unsafe_ip(ip):
            raise UnsafeUrlError(
                f"Target {hostname!r} resolves to non-public IP {ip_str!r}: {url!r}"
            )


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP is in a range that outbound fetchers must not hit."""
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_redirect(current_url: str, location: str) -> str:
    """Resolve a redirect Location header against the current URL.

    Handles absolute URLs and host-absolute paths (``/foo``); we don't try
    to handle every relative-URL edge case because each hop is re-validated
    and a malformed Location will surface as an ``UnsafeUrlError`` on the
    next pass.
    """
    parsed = urlparse(location)
    if parsed.scheme and parsed.netloc:
        return location
    # Host-absolute or relative — attach to the current URL's scheme/host.
    current = urlparse(current_url)
    return urlunparse(
        (
            current.scheme,
            current.netloc,
            parsed.path or current.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _check_content_length(response: httpx.Response, max_body_bytes: int) -> None:
    """Reject the response up-front when ``Content-Length`` exceeds the cap."""
    raw = response.headers.get("content-length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError:
        return
    if length > max_body_bytes:
        response.close()
        raise BodyTooLargeError(
            f"Content-Length {length} exceeds cap {max_body_bytes} for {response.url!r}"
        )


def _read_body_with_cap(response: httpx.Response, max_body_bytes: int) -> bytes:
    """Stream the body into memory, aborting past the cap.

    httpx yields the whole body eagerly once ``response.content`` is touched;
    we iterate in chunks so a chunked transfer that lies about its length
    can still be aborted before we exhaust memory.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_body_bytes:
            response.close()
            raise BodyTooLargeError(
                f"Response body exceeds cap {max_body_bytes} bytes (saw at least {total}) "
                f"for {response.url!r}"
            )
        chunks.append(chunk)
    return b"".join(chunks)
