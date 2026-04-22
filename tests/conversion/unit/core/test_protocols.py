"""Unit tests for conversion protocols: class-level declarations and structural conformance."""

from __future__ import annotations

from typing import Any, ClassVar

from aizk.conversion.core.protocols import (
    ContentFetcher,
    Converter,
    RefResolver,
    ResourceGuard,
)
from aizk.conversion.core.source_ref import ArxivRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput


class _FakePdfFetcher:
    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

    def fetch(self, ref: SourceRef) -> ConversionInput:
        return ConversionInput(content=b"x", content_type=ContentType.PDF)


class _FakeResolver:
    resolves_to: ClassVar[frozenset[str]] = frozenset({"arxiv", "url"})

    def resolve(self, ref: SourceRef) -> SourceRef:
        return ArxivRef(arxiv_id="2301.12345")


class _FakeGpuConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})
    requires_gpu: ClassVar[bool] = True

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002 — protocol arg name
        return ConversionArtifacts(markdown="# out")

    def config_snapshot(self) -> dict[str, Any]:
        return {"converter_name": "fake"}


class _FakeCpuConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.CSV})
    requires_gpu: ClassVar[bool] = False

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002 — protocol arg name
        return ConversionArtifacts(markdown="")

    def config_snapshot(self) -> dict[str, Any]:
        return {"converter_name": "fake-cpu"}


class _FakeGuard:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_content_fetcher_isinstance_structural():
    assert isinstance(_FakePdfFetcher(), ContentFetcher)


def test_ref_resolver_isinstance_structural():
    assert isinstance(_FakeResolver(), RefResolver)


def test_content_fetcher_and_ref_resolver_are_distinguishable():
    # A ContentFetcher (fetch + produces) MUST NOT accidentally satisfy RefResolver
    # and vice versa — this is the invariant the registry relies on for
    # structural role determination.
    fetcher = _FakePdfFetcher()
    resolver = _FakeResolver()
    assert isinstance(fetcher, ContentFetcher)
    assert not isinstance(fetcher, RefResolver)
    assert isinstance(resolver, RefResolver)
    assert not isinstance(resolver, ContentFetcher)


def test_converter_requires_gpu_is_class_level_bool_without_instantiation():
    # Spec: requires_gpu must be inspectable without instantiating the class.
    assert _FakeGpuConverter.requires_gpu is True
    assert _FakeCpuConverter.requires_gpu is False


def test_converter_supported_formats_is_class_level_frozenset_without_instantiation():
    assert isinstance(_FakeGpuConverter.supported_formats, frozenset)
    assert ContentType.PDF in _FakeGpuConverter.supported_formats
    assert ContentType.HTML in _FakeGpuConverter.supported_formats


def test_converter_isinstance_structural():
    assert isinstance(_FakeGpuConverter(), Converter)
    assert isinstance(_FakeCpuConverter(), Converter)


def test_ref_resolver_resolves_to_is_class_level_frozenset_without_instantiation():
    assert isinstance(_FakeResolver.resolves_to, frozenset)
    assert _FakeResolver.resolves_to == frozenset({"arxiv", "url"})
    assert all(isinstance(kind, str) for kind in _FakeResolver.resolves_to)


def test_content_fetcher_produces_is_class_level_frozenset_without_instantiation():
    assert isinstance(_FakePdfFetcher.produces, frozenset)
    assert _FakePdfFetcher.produces == frozenset({ContentType.PDF})


def test_resource_guard_isinstance_structural():
    assert isinstance(_FakeGuard(), ResourceGuard)


def test_protocols_do_not_declare_api_submittable_flag():
    # Design decision: public-ingress acceptability is a deployment policy, not
    # an adapter property. No protocol exposes it.
    for proto in (ContentFetcher, RefResolver, Converter):
        assert "api_submittable" not in getattr(proto, "__annotations__", {})
        assert not hasattr(proto, "api_submittable")
