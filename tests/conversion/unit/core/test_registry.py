"""Tests for FetcherRegistry and ConverterRegistry."""

from __future__ import annotations

from typing import ClassVar

import pytest

from aizk.conversion.core.errors import FetcherNotRegistered, NoConverterForFormat
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import (
    ContentType,
    ConversionArtifacts,
    ConversionInput,
)


class _FakeContentFetcher:
    def fetch(self, ref):  # noqa: ARG002
        return ConversionInput(content=b"", content_type=ContentType.HTML)


class _FakeResolver:
    resolves_to: ClassVar[frozenset[str]] = frozenset({"url"})

    def resolve(self, ref):  # noqa: ARG002
        return ref


class _MultiFormatConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset(
        {ContentType.PDF, ContentType.HTML}
    )
    requires_gpu: ClassVar[bool] = True

    def convert(self, conversion_input):  # noqa: ARG002
        return ConversionArtifacts(markdown="")

    def config_snapshot(self):
        return {}


# --- FetcherRegistry --------------------------------------------------------


def test_register_content_fetcher_and_resolve():
    reg = FetcherRegistry()
    impl = _FakeContentFetcher()
    reg.register_content_fetcher("arxiv", impl)
    role, resolved = reg.resolve("arxiv")
    assert role == "content_fetcher"
    assert resolved is impl


def test_register_resolver_and_resolve():
    reg = FetcherRegistry()
    impl = _FakeResolver()
    reg.register_resolver("karakeep_bookmark", impl)
    role, resolved = reg.resolve("karakeep_bookmark")
    assert role == "resolver"
    assert resolved is impl


def test_duplicate_kind_across_roles_rejected():
    reg = FetcherRegistry()
    reg.register_content_fetcher("url", _FakeContentFetcher())
    with pytest.raises(ValueError, match="already registered"):
        reg.register_resolver("url", _FakeResolver())


def test_duplicate_kind_same_role_rejected():
    reg = FetcherRegistry()
    reg.register_content_fetcher("url", _FakeContentFetcher())
    with pytest.raises(ValueError, match="already registered"):
        reg.register_content_fetcher("url", _FakeContentFetcher())


def test_unregistered_kind_raises_typed_error():
    reg = FetcherRegistry()
    with pytest.raises(FetcherNotRegistered) as exc:
        reg.resolve("does_not_exist")
    assert exc.value.kind == "does_not_exist"


def test_registered_kinds_returns_union_across_roles():
    reg = FetcherRegistry()
    reg.register_content_fetcher("url", _FakeContentFetcher())
    reg.register_resolver("karakeep_bookmark", _FakeResolver())
    assert reg.registered_kinds() == frozenset({"url", "karakeep_bookmark"})


# --- ConverterRegistry ------------------------------------------------------


def test_converter_registered_for_each_supported_format():
    reg = ConverterRegistry()
    impl = _MultiFormatConverter()
    reg.register("docling", impl)
    assert reg.resolve(ContentType.PDF, "docling") is impl
    assert reg.resolve(ContentType.HTML, "docling") is impl


def test_missing_converter_raises_typed_error():
    reg = ConverterRegistry()
    reg.register("docling", _MultiFormatConverter())
    with pytest.raises(NoConverterForFormat):
        reg.resolve(ContentType.IMAGE, "docling")
    with pytest.raises(NoConverterForFormat):
        reg.resolve(ContentType.PDF, "marker")


def test_converter_with_no_supported_formats_rejected():
    class _Empty:
        supported_formats: ClassVar[frozenset[ContentType]] = frozenset()
        requires_gpu: ClassVar[bool] = False

        def convert(self, conversion_input):  # noqa: ARG002
            return ConversionArtifacts(markdown="")

        def config_snapshot(self):
            return {}

    reg = ConverterRegistry()
    with pytest.raises(ValueError, match="supported_formats"):
        reg.register("empty", _Empty())


def test_converter_missing_supported_formats_attr_rejected():
    class _NoAttr:
        requires_gpu: ClassVar[bool] = False

        def convert(self, conversion_input):  # noqa: ARG002
            return ConversionArtifacts(markdown="")

        def config_snapshot(self):
            return {}

    reg = ConverterRegistry()
    with pytest.raises(ValueError, match="supported_formats"):
        reg.register("no_attr", _NoAttr())
