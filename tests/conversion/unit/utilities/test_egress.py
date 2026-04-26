"""Unit tests for the conversion egress policy helper."""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any

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
