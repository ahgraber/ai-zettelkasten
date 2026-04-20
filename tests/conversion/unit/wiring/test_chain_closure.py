"""Unit tests for validate_chain_closure and register_ready_adapters chain behaviour.

Covers:
- validate_chain_closure passes for a well-formed resolver DAG.
- validate_chain_closure raises ChainNotTerminated when a resolves_to target is missing.
- validate_chain_closure raises ChainNotTerminated when resolvers form a cycle.
- validate_chain_closure raises ChainNotTerminated when the declared DAG exceeds depth_cap.
- validate_chain_closure operates on registered_kinds, not accepted_submission_kinds.
- "singlefile" is NOT in registered_kinds because register_ready_adapters omits SingleFileFetcher.
- Default wiring passes chain closure validation.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import patch

import pytest

from aizk.conversion.core.errors import ChainNotTerminated
from aizk.conversion.core.protocols import ContentFetcher, RefResolver
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.source_ref import SourceRef, UrlRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.wiring.registrations import validate_chain_closure

# ---------------------------------------------------------------------------
# Minimal fake adapters — used to build synthetic DAGs without real adapters
# ---------------------------------------------------------------------------


class _FakePdfFetcher:
    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

    def fetch(self, ref: SourceRef) -> ConversionInput:  # pragma: no cover
        return ConversionInput(content=b"", content_type=ContentType.PDF)


class _FakeHtmlFetcher:
    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def fetch(self, ref: SourceRef) -> ConversionInput:  # pragma: no cover
        return ConversionInput(content=b"", content_type=ContentType.HTML)


def _make_resolver(resolves_to: frozenset[str]) -> RefResolver:
    """Factory: return a one-off RefResolver subclass with the given resolves_to set."""

    class _R:
        pass

    _R.resolves_to = resolves_to  # type: ignore[attr-defined]

    def _resolve(self, ref: SourceRef) -> SourceRef:  # pragma: no cover
        return UrlRef(url="https://example.com/")

    _R.resolve = _resolve  # type: ignore[attr-defined]
    return _R()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Happy-path: well-formed single-hop DAG
# ---------------------------------------------------------------------------


def test_validate_chain_closure_passes_for_valid_dag():
    fr = FetcherRegistry()
    # Resolver that emits "url"; terminal fetcher registered for "url".
    fr.register_resolver("karakeep_bookmark", _make_resolver(frozenset({"url"})))
    fr.register_content_fetcher("url", _FakePdfFetcher())
    # Should not raise.
    validate_chain_closure(fr, depth_cap=2)


def test_validate_chain_closure_passes_with_no_resolvers():
    fr = FetcherRegistry()
    fr.register_content_fetcher("url", _FakePdfFetcher())
    validate_chain_closure(fr, depth_cap=2)


# ---------------------------------------------------------------------------
# Missing registration
# ---------------------------------------------------------------------------


def test_validate_chain_closure_raises_when_resolves_to_target_is_not_registered():
    fr = FetcherRegistry()
    # Resolver declares "arxiv" but "arxiv" is not registered.
    fr.register_resolver("karakeep_bookmark", _make_resolver(frozenset({"arxiv"})))
    with pytest.raises(ChainNotTerminated, match="arxiv"):
        validate_chain_closure(fr, depth_cap=2)


def test_validate_chain_closure_raises_on_partial_missing_target():
    """Two resolves_to targets — one registered, one not — still raises."""
    fr = FetcherRegistry()
    fr.register_resolver("karakeep_bookmark", _make_resolver(frozenset({"url", "arxiv"})))
    fr.register_content_fetcher("url", _FakePdfFetcher())
    # "arxiv" is not registered → must still raise.
    with pytest.raises(ChainNotTerminated, match="arxiv"):
        validate_chain_closure(fr, depth_cap=2)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_validate_chain_closure_raises_on_direct_cycle():
    """a → b, b → a forms a two-node cycle."""
    fr = FetcherRegistry()
    fr.register_resolver("a", _make_resolver(frozenset({"b"})))
    fr.register_resolver("b", _make_resolver(frozenset({"a"})))
    with pytest.raises(ChainNotTerminated):
        validate_chain_closure(fr, depth_cap=5)


def test_validate_chain_closure_raises_on_self_referential_cycle():
    """a → a is a direct self-cycle."""
    fr = FetcherRegistry()
    fr.register_resolver("a", _make_resolver(frozenset({"a"})))
    with pytest.raises(ChainNotTerminated):
        validate_chain_closure(fr, depth_cap=5)


# ---------------------------------------------------------------------------
# Depth cap
# ---------------------------------------------------------------------------


def test_validate_chain_closure_raises_on_declared_path_exceeding_depth_cap():
    """Three-hop resolver chain: a → b → c → url (terminal). cap=1 must fail."""
    fr = FetcherRegistry()
    fr.register_resolver("a", _make_resolver(frozenset({"b"})))
    fr.register_resolver("b", _make_resolver(frozenset({"c"})))
    fr.register_resolver("c", _make_resolver(frozenset({"url"})))
    fr.register_content_fetcher("url", _FakePdfFetcher())
    with pytest.raises(ChainNotTerminated):
        validate_chain_closure(fr, depth_cap=1)


def test_validate_chain_closure_passes_at_exact_depth_cap():
    """One-hop resolver chain (depth=1) should pass with depth_cap=2."""
    fr = FetcherRegistry()
    fr.register_resolver("karakeep_bookmark", _make_resolver(frozenset({"url"})))
    fr.register_content_fetcher("url", _FakePdfFetcher())
    validate_chain_closure(fr, depth_cap=2)


# ---------------------------------------------------------------------------
# Operates on registered_kinds, NOT on accepted_submission_kinds
# ---------------------------------------------------------------------------


def test_validate_chain_closure_considers_registered_kinds_not_ingress_policy():
    """Chain validation must pass even for kinds that are not in IngressPolicy.

    The closure check walks registered_kinds; IngressPolicy.accepted_submission_kinds
    is irrelevant. Here "url" and "arxiv" are registered but would not be publicly
    submittable under the default IngressPolicy — chain closure should still pass.
    """
    fr = FetcherRegistry()
    # karakeep_bookmark resolves to "arxiv" and "url" — both registered.
    fr.register_resolver("karakeep_bookmark", _make_resolver(frozenset({"arxiv", "url"})))
    fr.register_content_fetcher("arxiv", _FakePdfFetcher())
    fr.register_content_fetcher("url", _FakeHtmlFetcher())
    # Should not raise — "arxiv" and "url" ARE registered even if not publicly submittable.
    validate_chain_closure(fr, depth_cap=2)


# ---------------------------------------------------------------------------
# "singlefile" is absent from registered_kinds
# ---------------------------------------------------------------------------


def test_singlefile_not_in_registered_kinds_after_register_ready_adapters():
    """SingleFileFetcher is a skeleton — register_ready_adapters must NOT register it."""
    from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
    from aizk.conversion.wiring.registrations import register_ready_adapters

    cfg = ConversionConfig(_env_file=None)
    docling_cfg = DoclingConverterConfig(_env_file=None)
    karakeep_cfg = KarakeepFetcherConfig(_env_file=None)
    fr = FetcherRegistry()
    cr = ConverterRegistry()
    register_ready_adapters(fr, cr, cfg, docling_cfg=docling_cfg, karakeep_cfg=karakeep_cfg)

    assert "singlefile" not in fr.registered_kinds(), (
        "register_ready_adapters should not wire SingleFileFetcher (skeleton)"
    )


# ---------------------------------------------------------------------------
# Default wiring passes chain closure
# ---------------------------------------------------------------------------


def test_default_wiring_passes_chain_closure():
    """register_ready_adapters completes without raising ChainNotTerminated."""
    from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
    from aizk.conversion.wiring.registrations import register_ready_adapters

    cfg = ConversionConfig(_env_file=None)
    docling_cfg = DoclingConverterConfig(_env_file=None)
    karakeep_cfg = KarakeepFetcherConfig(_env_file=None)
    fr = FetcherRegistry()
    cr = ConverterRegistry()
    # Should not raise — KarakeepBookmarkResolver's resolves_to targets are all registered.
    register_ready_adapters(fr, cr, cfg, docling_cfg=docling_cfg, karakeep_cfg=karakeep_cfg)
