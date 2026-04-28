"""Network egress policy: deny-list-only DNS resolution and IP classification.

Deny categories enforced by `_classify_address`:

  IPv4
    - loopback (127.0.0.0/8)
    - private (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    - shared address space (100.64.0.0/10, RFC 6598)
    - link-local (169.254.0.0/16, including 169.254.169.254 cloud-metadata)
    - unspecified-source (0.0.0.0/8)
    - broadcast (255.255.255.255)
    - multicast (224.0.0.0/4)
    - documentation/test (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 198.18.0.0/15)

  IPv6
    - loopback (::1)
    - unspecified (::)
    - unique local (fc00::/7)
    - link-local (fe80::/10)
    - multicast (ff00::/8)
    - documentation (2001:db8::/32)
    - cloud metadata (fd00:ec2::254)
    - NAT64 (64:ff9b::/96) — embeds an IPv4 address; classified by the embedded address
    - 6to4 (2002::/16) — embeds an IPv4 address; classified by the embedded address
    - IPv4-mapped (::ffff:0:0/96) — normalized via .ipv4_mapped before classification

The primary classification gate is `not address.is_global`, with the categories
above applied as belt-and-suspenders checks for ranges where stdlib coverage is
inconsistent across Python minor versions (see design "IP classification library").

See `.specs/changes/network-egress-policy/design.md` § "IP classification library"
for the rationale.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import ipaddress
import logging
import socket
import threading
from typing import Final
from urllib.parse import urlparse

from aizk.conversion.core.errors import (
    DenyListDestination,
    DisallowedScheme,
    DnsTimeout,
)

logger = logging.getLogger(__name__)

_DNS_TIMEOUT_SECONDS: Final[float] = 2.0
_DNS_EXECUTOR_WORKERS: Final[int] = 4
_VALIDATION_EXECUTOR_WORKERS: Final[int] = 4

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

# Belt-and-suspenders ranges below.
#
# `ip.is_global` catches the standard private networks:
#   - RFC 1918 (10/8, 172.16/12, 192.168/16)
#   - loopback (127/8, ::1)
#   - link-local (169.254/16, fe80::/10)
#   - IPv6 ULA (fc00::/7), unspecified (::/128)
# Multicast (224/4, ff00::/8) is gated explicitly because Python 3.12 returns
# `is_global=True` for multicast addresses; see `_classify_address`.
#
# The networks/addresses listed here are ranges where stdlib coverage has been
# inconsistent across Python minor versions, or that need explicit handling
# beyond `is_global`:
#   - 0.0.0.0/8      RFC 791 "this network" — `is_unspecified` only matches
#                    the literal 0.0.0.0, not the rest of the /8.
#   - 100.64.0.0/10  RFC 6598 CGNAT shared address space — `is_global` not
#                    consistently False across older Python minors.
#   - 64:ff9b::/96   NAT64 — embeds an IPv4 address in the low 32 bits;
#                    classified via the embedded address (see `_classify_address`).
#   - 2002::/16      6to4 — embeds an IPv4 address in bits 17-48; same shape.
#   - 169.254.169.254 / 255.255.255.255 / fd00:ec2::254 — explicit auditability
#                    markers for cloud-metadata / broadcast even though the
#                    primary gate already catches them.
_IPV4_DENY_NETWORKS: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
)
_IPV6_DENY_NETWORKS: Final[tuple[ipaddress.IPv6Network, ...]] = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("2002::/16"),
)
_IPV4_DENY_ADDRESSES: Final[frozenset[ipaddress.IPv4Address]] = frozenset(
    {
        ipaddress.IPv4Address("169.254.169.254"),
        ipaddress.IPv4Address("255.255.255.255"),
    }
)
_IPV6_DENY_ADDRESSES: Final[frozenset[ipaddress.IPv6Address]] = frozenset(
    {
        ipaddress.IPv6Address("fd00:ec2::254"),
    }
)


_dns_executor: concurrent.futures.ThreadPoolExecutor | None = None
_validation_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_dns_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the module-level DNS executor, creating it on first use."""
    global _dns_executor
    if _dns_executor is None:
        with _executor_lock:
            if _dns_executor is None:
                _dns_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_DNS_EXECUTOR_WORKERS,
                    thread_name_prefix="aizk-egress-dns",
                )
    return _dns_executor


def _get_validation_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the module-level validation executor for async callers."""
    global _validation_executor
    if _validation_executor is None:
        with _executor_lock:
            if _validation_executor is None:
                _validation_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_VALIDATION_EXECUTOR_WORKERS,
                    thread_name_prefix="aizk-egress-validate",
                )
    return _validation_executor


@dataclass(frozen=True)
class ValidatedDestination:
    """A destination that passed the egress policy.

    `ip` is the address that classification approved and that the connection
    layer SHALL pin for the socket connect; DNS-rebinding TOCTOU is closed by
    using this exact IP rather than re-resolving.
    """

    ip: str
    port: int
    host: str
    scheme: str


def _classify_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True iff the address is in the deny set.

    Primary gate is `not is_global`; explicit category checks are layered on
    top for ranges that stdlib classification has historically missed (e.g.
    RFC 6598 shared address space, NAT64, 6to4, the cloud-metadata IPs).
    """
    # Normalize IPv4-mapped IPv6 (::ffff:a.b.c.d) so an attacker cannot smuggle
    # a private IPv4 inside an IPv6 envelope past classification.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return _classify_address(mapped)

    # Multicast is_global is True on Python 3.12 — gate it explicitly.
    if ip.is_multicast:
        return True

    if not ip.is_global:
        return True

    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _IPV4_DENY_ADDRESSES:
            return True
        for network in _IPV4_DENY_NETWORKS:
            if ip in network:
                return True
        return False

    if ip in _IPV6_DENY_ADDRESSES:
        return True
    for network in _IPV6_DENY_NETWORKS:
        if ip in network:
            # NAT64 / 6to4: classify the embedded IPv4 address.
            packed = ip.packed
            if network.prefixlen == 96:  # NAT64: low 32 bits carry IPv4
                embedded = ipaddress.IPv4Address(packed[12:16])
                return _classify_address(embedded)
            if network.prefixlen == 16:  # 6to4: bits 17-48 carry IPv4
                embedded = ipaddress.IPv4Address(packed[2:6])
                return _classify_address(embedded)
            return True
    return False


def _resolve_with_deadline(
    host: str,
    port: int,
    *,
    timeout: float | None = None,
) -> list[tuple[str, int]]:
    """Resolve `host`:`port` via getaddrinfo with a hard wall-clock deadline.

    Returns the list of `(ip_string, family)` pairs that getaddrinfo produced.
    Raises `DnsTimeout` if resolution exceeds the deadline; the calling thread
    in the bounded executor is then released back to the pool but the
    underlying getaddrinfo call may continue to run on its own — this is
    acceptable because the deny-list semantics are enforced before any
    socket is opened.
    """
    deadline = _DNS_TIMEOUT_SECONDS if timeout is None else timeout
    executor = _get_dns_executor()
    future = executor.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    try:
        results = future.result(timeout=deadline)
    except concurrent.futures.TimeoutError as exc:
        logger.warning(
            "Egress denied: DNS resolution exceeded deadline",
            extra={"host": host, "deadline_seconds": deadline},
        )
        raise DnsTimeout(f"DNS resolution exceeded {deadline}s deadline") from exc
    addresses: list[tuple[str, int]] = []
    for family, _socktype, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        addresses.append((ip_str, family))
    return addresses


def assert_egress_allowed(url: str) -> ValidatedDestination:
    """Validate `url` against the egress policy synchronously.

    Resolves DNS with a 2-second deadline, classifies every resolved address,
    and returns a `ValidatedDestination` carrying the first address from the
    `getaddrinfo` result set. The first address is the one the connection-pinned
    transport will dial; classifying ALL addresses prevents an attacker from
    smuggling a private IP behind a public one in mixed-resolution responses.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        logger.warning(
            "Egress denied: disallowed scheme",
            extra={"scheme": scheme, "url": url},
        )
        raise DisallowedScheme(f"Scheme {scheme!r} is not in the egress allowlist")

    host = parsed.hostname
    if not host:
        logger.warning("Egress denied: URL missing hostname", extra={"url": url})
        raise DisallowedScheme("URL is missing a hostname")

    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80

    addresses = _resolve_with_deadline(host, port)
    if not addresses:
        logger.warning(
            "Egress denied: no addresses resolved",
            extra={"host": host, "url": url},
        )
        raise DenyListDestination(f"No addresses resolved for host {host!r}")

    for ip_str, _family in addresses:
        ip = ipaddress.ip_address(ip_str)
        if _classify_address(ip):
            logger.warning(
                "Egress denied: resolved address in deny set",
                extra={"host": host, "ip": ip_str, "url": url},
            )
            raise DenyListDestination(f"Resolved address for host {host!r} is in the egress deny set")

    first_ip = addresses[0][0]
    return ValidatedDestination(ip=first_ip, port=port, host=host, scheme=scheme)


async def async_assert_egress_allowed(url: str) -> ValidatedDestination:
    """Async wrapper around `assert_egress_allowed`.

    Submits the sync validation to a dedicated bounded executor (separate
    from `asyncio.to_thread`'s default executor and from the DNS executor)
    so concurrent fan-out (e.g. the 50-image pre-fetch phase) cannot stall
    the event loop.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_validation_executor(),
        assert_egress_allowed,
        url,
    )


__all__ = [
    "ValidatedDestination",
    "assert_egress_allowed",
    "async_assert_egress_allowed",
]
