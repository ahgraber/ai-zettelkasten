"""Unit tests for FetcherRegistry and ConverterRegistry."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from aizk.conversion.core.errors import (
    FetcherNotRegistered,
    NoConverterForFormat,
    RegistrationRoleMismatch,
)
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.source_ref import ArxivRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput

# --- Fake adapters -----------------------------------------------------------


class _FakeContentFetcher:
    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

    def fetch(self, ref: SourceRef) -> ConversionInput:
        return ConversionInput(content=b"x", content_type=ContentType.PDF)


class _FakeRefResolver:
    resolves_to: ClassVar[frozenset[str]] = frozenset({"arxiv"})

    def resolve(self, ref: SourceRef) -> SourceRef:
        return ArxivRef(arxiv_id="2301.12345")


class _NotAFetcher:
    """Satisfies neither ContentFetcher nor RefResolver."""

    def something_else(self) -> None: ...


class _MultiFormatConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})
    requires_gpu: ClassVar[bool] = True

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002 — protocol arg name
        return ConversionArtifacts(markdown="# out")

    def config_snapshot(self) -> dict[str, Any]:
        return {"converter_name": "multi"}


# --- FetcherRegistry: happy path --------------------------------------------


def test_register_content_fetcher_stores_impl_and_resolves_it():
    reg = FetcherRegistry()
    impl = _FakeContentFetcher()
    reg.register_content_fetcher("arxiv", impl)
    assert reg.resolve("arxiv") is impl


def test_register_resolver_stores_impl_and_resolves_it():
    reg = FetcherRegistry()
    impl = _FakeRefResolver()
    reg.register_resolver("karakeep_bookmark", impl)
    assert reg.resolve("karakeep_bookmark") is impl


def test_registered_kinds_returns_union_across_roles():
    reg = FetcherRegistry()
    reg.register_content_fetcher("arxiv", _FakeContentFetcher())
    reg.register_resolver("karakeep_bookmark", _FakeRefResolver())
    assert reg.registered_kinds() == frozenset({"arxiv", "karakeep_bookmark"})


def test_registered_kinds_is_frozenset():
    reg = FetcherRegistry()
    reg.register_content_fetcher("arxiv", _FakeContentFetcher())
    assert isinstance(reg.registered_kinds(), frozenset)


# --- FetcherRegistry: unknown kind ------------------------------------------


def test_resolve_unknown_kind_raises_fetcher_not_registered():
    reg = FetcherRegistry()
    with pytest.raises(FetcherNotRegistered):
        reg.resolve("nope")


# --- FetcherRegistry: duplicate kind across both roles ----------------------


def test_duplicate_content_fetcher_kind_rejected():
    reg = FetcherRegistry()
    reg.register_content_fetcher("arxiv", _FakeContentFetcher())
    with pytest.raises(ValueError):
        reg.register_content_fetcher("arxiv", _FakeContentFetcher())


def test_duplicate_resolver_kind_rejected():
    reg = FetcherRegistry()
    reg.register_resolver("karakeep_bookmark", _FakeRefResolver())
    with pytest.raises(ValueError):
        reg.register_resolver("karakeep_bookmark", _FakeRefResolver())


def test_resolver_kind_cannot_also_be_content_fetcher():
    reg = FetcherRegistry()
    reg.register_resolver("arxiv", _FakeRefResolver())
    with pytest.raises(ValueError):
        reg.register_content_fetcher("arxiv", _FakeContentFetcher())


def test_content_fetcher_kind_cannot_also_be_resolver():
    reg = FetcherRegistry()
    reg.register_content_fetcher("arxiv", _FakeContentFetcher())
    with pytest.raises(ValueError):
        reg.register_resolver("arxiv", _FakeRefResolver())


# --- FetcherRegistry: role-mismatch rejection -------------------------------


def test_register_content_fetcher_rejects_resolver_impl_with_state_unchanged():
    reg = FetcherRegistry()
    with pytest.raises(RegistrationRoleMismatch):
        reg.register_content_fetcher("karakeep_bookmark", _FakeRefResolver())
    assert reg.registered_kinds() == frozenset()


def test_register_content_fetcher_rejects_impl_that_is_not_a_content_fetcher():
    reg = FetcherRegistry()
    with pytest.raises(RegistrationRoleMismatch):
        reg.register_content_fetcher("bogus", _NotAFetcher())
    assert reg.registered_kinds() == frozenset()


def test_register_resolver_rejects_non_resolver_impl_with_state_unchanged():
    reg = FetcherRegistry()
    with pytest.raises(RegistrationRoleMismatch):
        reg.register_resolver("arxiv", _FakeContentFetcher())
    assert reg.registered_kinds() == frozenset()


def test_register_resolver_rejects_junk_impl():
    reg = FetcherRegistry()
    with pytest.raises(RegistrationRoleMismatch):
        reg.register_resolver("bogus", _NotAFetcher())
    assert reg.registered_kinds() == frozenset()


def test_registry_has_no_submittable_kinds_method():
    """Public-ingress is NOT a registry concern."""
    reg = FetcherRegistry()
    assert not hasattr(reg, "submittable_kinds")


# --- ConverterRegistry ------------------------------------------------------


def test_converter_registry_multi_format_registration_resolvable_per_type():
    reg = ConverterRegistry()
    conv = _MultiFormatConverter()
    reg.register(conv, name="multi")
    assert reg.resolve(ContentType.PDF, "multi") is conv
    assert reg.resolve(ContentType.HTML, "multi") is conv


def test_converter_registry_missing_combo_raises_no_converter_for_format():
    reg = ConverterRegistry()
    reg.register(_MultiFormatConverter(), name="multi")
    with pytest.raises(NoConverterForFormat):
        reg.resolve(ContentType.CSV, "multi")
    with pytest.raises(NoConverterForFormat):
        reg.resolve(ContentType.PDF, "other-name")
