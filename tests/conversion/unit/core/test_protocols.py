"""Tests for the core protocol class-attribute contracts.

These tests assert that the static class-level attributes (`requires_gpu`,
`supported_formats`, `resolves_to`) are inspectable on the class itself,
without instantiation. The wiring layer relies on this for chain-closure
validation and GPU-guard dispatch decisions.
"""

from __future__ import annotations

from typing import ClassVar

from aizk.conversion.core.protocols import (
    ContentFetcher,
    Converter,
    RefResolver,
    ResourceGuard,
)
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput


class _GpuConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})
    requires_gpu: ClassVar[bool] = True

    def convert(self, conversion_input):  # noqa: ARG002
        return ConversionArtifacts(markdown="")

    def config_snapshot(self):
        return {}


class _CpuConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})
    requires_gpu: ClassVar[bool] = False

    def convert(self, conversion_input):  # noqa: ARG002
        return ConversionArtifacts(markdown="")

    def config_snapshot(self):
        return {}


class _Resolver:
    resolves_to: ClassVar[frozenset[str]] = frozenset({"arxiv", "url"})

    def resolve(self, ref):  # noqa: ARG002
        return ref


def test_converter_requires_gpu_inspectable_on_class():
    assert _GpuConverter.requires_gpu is True
    assert _CpuConverter.requires_gpu is False


def test_converter_supported_formats_inspectable_on_class():
    assert _GpuConverter.supported_formats == frozenset({ContentType.PDF})
    assert _CpuConverter.supported_formats == frozenset({ContentType.HTML})


def test_resolver_resolves_to_inspectable_on_class():
    assert _Resolver.resolves_to == frozenset({"arxiv", "url"})
    assert isinstance(_Resolver.resolves_to, frozenset)


def test_protocols_are_runtime_checkable():
    assert isinstance(_GpuConverter(), Converter)
    assert isinstance(_Resolver(), RefResolver)


def test_resource_guard_is_protocol():
    class _Guard:
        def __init__(self):
            self.acquired = False

        def __enter__(self):
            self.acquired = True
            return self

        def __exit__(self, exc_type, exc, tb):
            self.acquired = False
            return False

    guard = _Guard()
    assert isinstance(guard, ResourceGuard)
    with guard:
        assert guard.acquired is True
    assert guard.acquired is False


def test_content_fetcher_protocol_callable():
    class _F:
        def fetch(self, ref):  # noqa: ARG002
            return ConversionInput(content=b"x", content_type=ContentType.HTML)

    assert isinstance(_F(), ContentFetcher)
