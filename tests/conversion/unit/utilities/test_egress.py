"""Unit tests for the conversion egress policy helper."""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any

from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
import pytest

from aizk.conversion.core.errors import (
    DenyListDestination,
    DisallowedScheme,
    DnsTimeout,
    EgressPolicyError,
)
from aizk.conversion.utilities import egress
from aizk.conversion.utilities.egress import (
    ValidatedDestination,
    _classify_address,
    assert_egress_allowed,
    async_assert_egress_allowed,
)


def _stub_getaddrinfo(monkeypatch: pytest.MonkeyPatch, *ips: str) -> None:
    """Replace socket.getaddrinfo so DNS returns the supplied addresses verbatim."""

    def fake(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        results: list[tuple[Any, ...]] = []
        for ip in ips:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError as exc:
                raise socket.gaierror(f"bad test ip: {ip}") from exc
            family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
            sockaddr: tuple[Any, ...] = (ip, port, 0, 0) if addr.version == 6 else (ip, port)
            results.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
        return results

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class TestClassifyAddress:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.5",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.1",
            "100.64.0.1",
            "169.254.1.1",
            "169.254.169.254",
            "0.0.0.0",
            "0.255.255.255",
            "255.255.255.255",
            "224.0.0.1",
        ],
    )
    def test_ipv4_deny_categories_rejected(self, ip: str) -> None:
        assert _classify_address(ipaddress.IPv4Address(ip)) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",
            "::",
            "fc00::1",
            "fd00::1",
            "fe80::1",
            "ff00::1",
            "fd00:ec2::254",
        ],
    )
    def test_ipv6_deny_categories_rejected(self, ip: str) -> None:
        assert _classify_address(ipaddress.IPv6Address(ip)) is True

    def test_nat64_embedding_link_local_rejected(self) -> None:
        # 64:ff9b::169.254.169.254 — NAT64 embedding cloud-metadata IPv4
        addr = ipaddress.IPv6Address("64:ff9b::a9fe:a9fe")
        assert _classify_address(addr) is True

    def test_6to4_embedding_private_rejected(self) -> None:
        # 2002::/16 with embedded 10.0.0.1 in bits 17-48 → 2002:0a00:0001::
        addr = ipaddress.IPv6Address("2002:0a00:0001::")
        assert _classify_address(addr) is True

    def test_ipv4_mapped_ipv6_embedding_loopback_rejected(self) -> None:
        addr = ipaddress.IPv6Address("::ffff:127.0.0.1")
        assert _classify_address(addr) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "192.0.2.1",  # RFC 5737 documentation
            "198.51.100.1",  # RFC 5737 documentation
            "203.0.113.1",  # RFC 5737 documentation
            "198.18.0.1",  # RFC 2544 benchmarking
            "198.19.255.254",
        ],
    )
    def test_ipv4_documentation_test_ranges_rejected(self, ip: str) -> None:
        # is_global excludes these on the pinned Python version.
        assert _classify_address(ipaddress.IPv4Address(ip)) is True

    def test_ipv6_documentation_range_rejected(self) -> None:
        assert _classify_address(ipaddress.IPv6Address("2001:db8::1")) is True

    @pytest.mark.parametrize(
        "ip",
        ["8.8.8.8", "1.1.1.1", "93.184.216.34"],
    )
    def test_public_ipv4_accepted(self, ip: str) -> None:
        assert _classify_address(ipaddress.IPv4Address(ip)) is False

    @pytest.mark.parametrize(
        "ip",
        ["2606:4700:4700::1111", "2001:4860:4860::8888"],
    )
    def test_public_ipv6_accepted(self, ip: str) -> None:
        assert _classify_address(ipaddress.IPv6Address(ip)) is False


class TestAssertEgressAllowed:
    @pytest.mark.parametrize(
        "scheme",
        ["file", "data", "javascript", "gopher", "ftp"],
    )
    def test_disallowed_scheme(self, scheme: str) -> None:
        with pytest.raises(DisallowedScheme):
            assert_egress_allowed(f"{scheme}://example.com/")

    def test_missing_host(self) -> None:
        with pytest.raises(DisallowedScheme):
            assert_egress_allowed("http:///")

    def test_loopback_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "127.0.0.1")
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://localhost/")

    def test_cloud_metadata_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "169.254.169.254")
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://metadata.local/latest/meta-data/")

    def test_mixed_resolution_any_private_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "8.8.8.8", "10.0.0.5")
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://attacker.example/")

    def test_public_ipv4_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "8.8.8.8")
        result = assert_egress_allowed("http://example.com/")
        assert isinstance(result, ValidatedDestination)
        assert result.ip == "8.8.8.8"
        assert result.host == "example.com"
        assert result.scheme == "http"
        assert result.port == 80

    def test_public_ipv6_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "2606:4700:4700::1111")
        result = assert_egress_allowed("https://example.com/")
        assert result.ip == "2606:4700:4700::1111"
        assert result.scheme == "https"
        assert result.port == 443

    def test_explicit_port_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "8.8.8.8")
        result = assert_egress_allowed("https://example.com:8443/foo")
        assert result.port == 8443

    def test_dns_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
            time.sleep(5.0)
            return []

        monkeypatch.setattr(socket, "getaddrinfo", slow)
        # Override the deadline so we don't actually wait 2 s in the test suite.
        monkeypatch.setattr(egress, "_DNS_TIMEOUT_SECONDS", 0.05)
        start = time.monotonic()
        with pytest.raises(DnsTimeout):
            assert_egress_allowed("http://example.com/")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    def test_egress_policy_error_is_base_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "10.0.0.1")
        with pytest.raises(EgressPolicyError):
            assert_egress_allowed("http://internal.example/")

    def test_empty_getaddrinfo_result_raises_deny_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `getaddrinfo` returning zero records is an unusual but legal outcome
        # (e.g., A-only AAAA query against a broken resolver). The egress
        # validator must treat it as a deny rather than approving an empty
        # destination set.
        def empty(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
            return []

        monkeypatch.setattr(socket, "getaddrinfo", empty)
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://example.com/")

    def test_ipv6_only_resolution_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Host resolves to a single IPv6 address with no IPv4 record.
        _stub_getaddrinfo(monkeypatch, "2606:4700:4700::1111")
        result = assert_egress_allowed("https://example.com/")
        assert result.ip == "2606:4700:4700::1111"

    def test_ipv6_only_resolution_to_link_local_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An IPv6-only host resolving to fe80::1 (link-local) must reject
        # at the same layer the IPv4 link-local case rejects.
        _stub_getaddrinfo(monkeypatch, "fe80::1")
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://internal.example/")

    def test_ipv6_only_cloud_metadata_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # AWS IPv6 metadata address must reject in IPv6-only resolution.
        _stub_getaddrinfo(monkeypatch, "fd00:ec2::254")
        with pytest.raises(DenyListDestination):
            assert_egress_allowed("http://metadata.example/")


class TestAsyncAssertEgressAllowed:
    @pytest.mark.asyncio
    async def test_async_wrapper_returns_validated_destination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "8.8.8.8")
        result = await async_assert_egress_allowed("http://example.com/")
        assert result.ip == "8.8.8.8"

    @pytest.mark.asyncio
    async def test_async_wrapper_propagates_egress_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_getaddrinfo(monkeypatch, "169.254.169.254")
        with pytest.raises(DenyListDestination):
            await async_assert_egress_allowed("http://metadata.local/")


# ---------------------------------------------------------------------------
# Property-based tests for `_classify_address`
#
# `_classify_address` is the central security primitive: a returns-False bug
# anywhere in the cross-product of `is_global` × the augmentation networks ×
# IPv4-mapped IPv6 normalization × NAT64 / 6to4 embedding becomes a silent
# SSRF primitive across every fetcher and the prefetch path. Example-based
# tests cover the canonical attack shapes; the property-based tests below
# probe the rest of the address space.
# ---------------------------------------------------------------------------


# `_classify_address` consults `ip.is_global` as the primary gate and
# augments it with the explicit-deny networks/addresses listed in the
# module's `_IPV4_DENY_*` / `_IPV6_DENY_*` constants. The contract this test
# pins: any address whose `is_global=True` AND that does not fall into any
# augmentation deny range is APPROVED (returns False); any address whose
# `is_global=True` but that DOES fall into an augmentation range is REJECTED
# (returns True). Multicast is rejected explicitly even though Python 3.12
# returns is_global=True for it.


def _is_in_ipv4_augmentation_deny(ip: ipaddress.IPv4Address) -> bool:
    """Replicate the augmentation-deny set from the egress module for IPv4."""
    if ip in egress._IPV4_DENY_ADDRESSES:
        return True
    return any(ip in net for net in egress._IPV4_DENY_NETWORKS)


def _is_in_ipv6_augmentation_deny(ip: ipaddress.IPv6Address) -> bool:
    """Replicate the augmentation-deny set from the egress module for IPv6."""
    if ip in egress._IPV6_DENY_ADDRESSES:
        return True
    return any(ip in net for net in egress._IPV6_DENY_NETWORKS)


@given(addr_int=st.integers(min_value=0, max_value=2**32 - 1))
@hyp_settings(max_examples=400, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_classify_address_property_ipv4(addr_int: int) -> None:
    """For every IPv4 address, classification matches the deny-set contract."""
    ip = ipaddress.IPv4Address(addr_int)
    expected_deny = (not ip.is_global) or ip.is_multicast or _is_in_ipv4_augmentation_deny(ip)
    assert _classify_address(ip) is expected_deny, (
        f"IPv4 {ip} (is_global={ip.is_global}, is_multicast={ip.is_multicast}) — expected deny={expected_deny}"
    )


@given(addr_int=st.integers(min_value=0, max_value=2**128 - 1))
@hyp_settings(max_examples=400, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_classify_address_property_ipv6(addr_int: int) -> None:
    """For every IPv6 address, classification respects mapping/embedding semantics.

    Three cases the function handles before consulting `is_global`:

    1. IPv4-mapped IPv6 (`::ffff:0:0/96`) is normalized via `ipv4_mapped` and
       classified as the embedded IPv4. The IPv6 envelope itself does not
       influence the verdict.
    2. NAT64 (`64:ff9b::/96`) and 6to4 (`2002::/16`) embed an IPv4 address;
       the function recurses on the embedded address.
    3. Multicast IPv6 (`ff00::/8`) is rejected explicitly even though Python
       returns `is_global=True` for several multicast scopes.
    """
    ip = ipaddress.IPv6Address(addr_int)

    # IPv4-mapped IPv6: behavior is determined by the embedded IPv4.
    mapped = ip.ipv4_mapped
    if mapped is not None:
        expected_deny = (not mapped.is_global) or mapped.is_multicast or _is_in_ipv4_augmentation_deny(mapped)
        assert _classify_address(ip) is expected_deny, f"IPv4-mapped IPv6 {ip} (embedded {mapped}) misclassified"
        return

    # NAT64 and 6to4: behavior is determined by the embedded IPv4.
    if ip in egress._IPV6_DENY_NETWORKS[0]:  # NAT64 64:ff9b::/96
        embedded = ipaddress.IPv4Address(ip.packed[12:16])
        embedded_deny = (not embedded.is_global) or embedded.is_multicast or _is_in_ipv4_augmentation_deny(embedded)
        assert _classify_address(ip) is embedded_deny, f"NAT64 IPv6 {ip} (embedded IPv4 {embedded}) misclassified"
        return
    if ip in egress._IPV6_DENY_NETWORKS[1]:  # 6to4 2002::/16
        embedded = ipaddress.IPv4Address(ip.packed[2:6])
        embedded_deny = (not embedded.is_global) or embedded.is_multicast or _is_in_ipv4_augmentation_deny(embedded)
        assert _classify_address(ip) is embedded_deny, f"6to4 IPv6 {ip} (embedded IPv4 {embedded}) misclassified"
        return

    # Plain IPv6: deny iff not is_global, multicast, or in IPv6 augmentation deny set.
    expected_deny = (not ip.is_global) or ip.is_multicast or _is_in_ipv6_augmentation_deny(ip)
    assert _classify_address(ip) is expected_deny, (
        f"IPv6 {ip} (is_global={ip.is_global}, is_multicast={ip.is_multicast}) — expected deny={expected_deny}"
    )


@given(addr_int=st.integers(min_value=0, max_value=2**32 - 1))
@hyp_settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_classify_address_ipv4_mapped_recursion_property(addr_int: int) -> None:
    """`::ffff:<v4>` always classifies the same as the embedded IPv4.

    Pins the smuggling-defeat invariant: an attacker cannot tunnel a
    private IPv4 inside an IPv6 envelope past classification.
    """
    v4 = ipaddress.IPv4Address(addr_int)
    mapped = ipaddress.IPv6Address(int(ipaddress.IPv6Address("::ffff:0:0")) | int(v4))
    assert _classify_address(mapped) is _classify_address(v4), (
        f"IPv4-mapped IPv6 {mapped} disagrees with embedded IPv4 {v4} classification"
    )
