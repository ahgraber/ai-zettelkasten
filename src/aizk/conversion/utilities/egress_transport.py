"""Connection-pinned httpx transport that defeats DNS-rebinding TOCTOU.

The transport carries a `ValidatedDestination` already approved by the egress
helper and dials that exact IP for the TCP connect. The Host header (set by
httpx at request construction time) and the TLS SNI hostname are preserved as
the original hostname so HTTP routing and certificate verification both see
the right name.

Per the design:

  - certificate verification is ON
  - minimum TLS 1.2 (Python 3.12 default; pinned explicitly)
  - hostname verification uses the original URL hostname, not the pinned IP

`verify=False` is explicitly rejected at construction time — the subclass
must not weaken TLS posture. Callers needing a custom CA bundle pass an
`ssl.SSLContext` via `ssl_context=...`.
"""

from __future__ import annotations

import ssl

import httpx

from aizk.conversion.utilities.egress import ValidatedDestination


def _build_default_ssl_context() -> ssl.SSLContext:
    """Return the default SSL context for egress: TLS 1.2 minimum, verify ON."""
    context = ssl.create_default_context()
    # Python 3.12 deprecates OP_NO_SSLv3/OP_NO_TLSv1/OP_NO_TLSv1_1 in favor of
    # minimum_version. Pinning TLS 1.2 minimum is auditable and forward-compatible.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class EgressPinnedTransport(httpx.AsyncHTTPTransport):
    """`AsyncHTTPTransport` that pins TCP connect target to a validated IP.

    On every outbound request the transport rewrites `request.url.host` to the
    validated IP just before delegating to `super().handle_async_request`,
    sets `request.extensions["sni_hostname"]` to the original hostname (so TLS
    SNI and cert SAN verification use the right name), and restores the
    original URL on the request afterward.
    """

    def __init__(
        self,
        destination: ValidatedDestination,
        *,
        ssl_context: ssl.SSLContext | None = None,
        verify: bool = True,
        **kwargs: object,
    ) -> None:
        """Initialize the transport with a pre-validated destination.

        Args:
            destination: The IP/host/scheme that egress validation already
                approved. The transport pins the TCP connect to ``destination.ip``.
            ssl_context: Optional custom SSL context. Must have hostname checking
                enabled and certificate verification required, or construction
                fails. Defaults to `_build_default_ssl_context()` (TLS 1.2+).
            verify: Must be ``True``. Passing ``False`` raises ``ValueError`` —
                the transport refuses to weaken TLS posture for egress traffic.
            **kwargs: Forwarded to ``httpx.AsyncHTTPTransport`` (timeouts,
                connection limits, etc.).

        Raises:
            ValueError: if ``verify=False`` is passed, or if ``ssl_context`` has
                hostname checking disabled or verification turned off.
        """
        if verify is not True:
            raise ValueError(
                "EgressPinnedTransport refuses verify=False; certificate verification is mandatory for egress traffic."
            )
        ctx = ssl_context if ssl_context is not None else _build_default_ssl_context()
        if not ctx.check_hostname or ctx.verify_mode == ssl.CERT_NONE:
            raise ValueError("EgressPinnedTransport refuses an SSL context with verification disabled.")
        super().__init__(verify=ctx, **kwargs)  # type: ignore[arg-type]
        self._destination = destination

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Pin the TCP connect target to the validated IP, then dispatch.

        Rewrites ``request.url.host`` to ``self._destination.ip`` so the
        socket layer dials the pre-validated address (defeats DNS-rebinding
        TOCTOU). Sets ``request.extensions["sni_hostname"]`` to the original
        hostname so TLS handshake SNI and cert-SAN verification both use the
        right name. Restores the original URL on the request before returning
        so callers (and the manual redirect loop) see the unmodified target.

        Note: the Host header is set by httpx at request build time and is
        therefore preserved as the original hostname automatically — the URL
        swap inside this method does not propagate back to the headers.
        """
        original_url = request.url
        original_host = original_url.host
        # Swap URL host to the pinned IP for the TCP connect. The Host header
        # was already set on the request by httpx at construction time and is
        # preserved as the original hostname; httpcore reads `sni_hostname`
        # from extensions for the TLS handshake.
        request.url = original_url.copy_with(host=self._destination.ip)
        request.extensions = {**request.extensions, "sni_hostname": original_host}
        try:
            response = await super().handle_async_request(request)
        finally:
            # Restore the original URL on the request so callers (and the
            # manual redirect loop in the fetcher) see the original target,
            # not the pinned IP.
            request.url = original_url
        return response


__all__ = ["EgressPinnedTransport", "_build_default_ssl_context"]
