"""Tests for Orchestrator: fetch dispatch, resolver recursion, depth cap."""

from __future__ import annotations

import importlib
import sys
from typing import ClassVar

import pytest

from aizk.conversion.core.errors import FetcherDepthExceeded
from aizk.conversion.core.orchestrator import Orchestrator
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from aizk.conversion.core.source_ref import UrlRef, ArxivRef, KarakeepBookmarkRef


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeContentFetcher:
    def __init__(self, content_type: ContentType = ContentType.HTML) -> None:
        self._content_type = content_type

    def fetch(self, ref):  # noqa: ARG002
        return ConversionInput(content=b"bytes", content_type=self._content_type)


class _FakeResolver:
    # Declared on the class so ``isinstance(impl, RefResolver)`` succeeds — the
    # runtime_checkable protocol requires the ``resolves_to`` ClassVar.
    resolves_to: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, target_ref) -> None:
        self._target_ref = target_ref

    def resolve(self, ref):  # noqa: ARG002
        return self._target_ref


class _FakeConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML, ContentType.PDF})
    requires_gpu: ClassVar[bool] = False

    def convert(self, conversion_input):  # noqa: ARG002
        return ConversionArtifacts(markdown="# result")

    def config_snapshot(self):
        return {}


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_orchestrator(registry: dict, converter_registry: dict, depth_cap: int = 3) -> Orchestrator:
    def resolve_fetcher(kind):
        return registry[kind]

    def resolve_converter(content_type, name):
        return converter_registry[(content_type, name)]

    return Orchestrator(resolve_fetcher, resolve_converter, depth_cap=depth_cap)


# ---------------------------------------------------------------------------
# Tests: single-hop content fetcher
# ---------------------------------------------------------------------------

def test_single_hop_content_fetcher():
    """A ref whose kind maps to a ContentFetcher returns ConversionInput directly."""
    fetcher = _FakeContentFetcher(ContentType.HTML)
    registry = {"url": fetcher}
    converter = _FakeConverter()
    converter_reg = {(ContentType.HTML, "fake"): converter}

    orch = _make_orchestrator(registry, converter_reg)
    ref = UrlRef(url="https://example.com")
    result = orch.process(ref, "fake")

    assert isinstance(result, ConversionArtifacts)
    assert result.markdown == "# result"


def test_single_hop_fetch_returns_conversion_input():
    """_fetch with a direct content fetcher returns the ConversionInput from the fetcher."""
    fetcher = _FakeContentFetcher(ContentType.PDF)
    registry = {"url": fetcher}

    orch = Orchestrator(
        resolve_fetcher=lambda kind: registry[kind],
        resolve_converter=lambda ct, name: None,
    )
    ref = UrlRef(url="https://example.com/paper.pdf")
    conv_input = orch._fetch(ref, depth=0)

    assert isinstance(conv_input, ConversionInput)
    assert conv_input.content_type is ContentType.PDF
    assert conv_input.content == b"bytes"


# ---------------------------------------------------------------------------
# Tests: two-hop resolver → content fetcher
# ---------------------------------------------------------------------------

def test_two_hop_resolver_then_fetcher():
    """A resolver ref is resolved once, then fetched by a content fetcher."""
    arxiv_ref = ArxivRef(arxiv_id="2301.12345")
    resolver = _FakeResolver(target_ref=arxiv_ref)
    fetcher = _FakeContentFetcher(ContentType.PDF)

    registry = {
        "karakeep_bookmark": resolver,
        "arxiv": fetcher,
    }
    converter = _FakeConverter()
    converter_reg = {(ContentType.PDF, "fake"): converter}

    orch = _make_orchestrator(registry, converter_reg)
    ref = KarakeepBookmarkRef(bookmark_id="bk-001")
    result = orch.process(ref, "fake")

    assert isinstance(result, ConversionArtifacts)


def test_two_hop_fetch_result_uses_resolved_ref():
    """_fetch at depth=0 for a resolver recurses and returns the fetcher's ConversionInput."""
    arxiv_ref = ArxivRef(arxiv_id="2301.00001")
    resolver = _FakeResolver(target_ref=arxiv_ref)
    fetcher = _FakeContentFetcher(ContentType.PDF)

    registry = {
        "karakeep_bookmark": resolver,
        "arxiv": fetcher,
    }
    orch = Orchestrator(
        resolve_fetcher=lambda kind: registry[kind],
        resolve_converter=lambda ct, name: None,
    )
    ref = KarakeepBookmarkRef(bookmark_id="bk-002")
    conv_input = orch._fetch(ref, depth=0)

    assert conv_input.content_type is ContentType.PDF


# ---------------------------------------------------------------------------
# Tests: depth cap
# ---------------------------------------------------------------------------

def test_depth_limit_raises_fetcher_depth_exceeded():
    """A chain of resolvers that reaches depth_cap raises FetcherDepthExceeded."""
    # Build a cyclic-ish chain: bookmark -> url -> url -> ... (hits cap before terminating)
    url_ref = UrlRef(url="https://example.com")
    # resolver always returns another UrlRef (infinite chain)
    registry = {
        "karakeep_bookmark": _FakeResolver(target_ref=url_ref),
        "url": _FakeResolver(target_ref=url_ref),
    }
    orch = Orchestrator(
        resolve_fetcher=lambda kind: registry[kind],
        resolve_converter=lambda ct, name: None,
        depth_cap=3,
    )
    ref = KarakeepBookmarkRef(bookmark_id="bk-deep")

    with pytest.raises(FetcherDepthExceeded) as exc:
        orch._fetch(ref, depth=0)

    assert exc.value.depth == 3
    assert exc.value.kind == "url"


def test_depth_cap_one_allows_single_resolver_hop():
    """With depth_cap=1, a single resolver hop (depth 0) succeeds; depth 1 raises."""
    url_ref = UrlRef(url="https://a.com")
    registry = {
        "url": _FakeResolver(target_ref=url_ref),
    }
    orch = Orchestrator(
        resolve_fetcher=lambda kind: registry[kind],
        resolve_converter=lambda ct, name: None,
        depth_cap=1,
    )
    # First call at depth=0 succeeds (resolves), second recursive call at depth=1 raises.
    with pytest.raises(FetcherDepthExceeded) as exc:
        orch._fetch(url_ref, depth=0)

    assert exc.value.depth == 1


def test_depth_cap_not_exceeded_for_valid_chain():
    """A chain shorter than depth_cap completes without raising."""
    arxiv_ref = ArxivRef(arxiv_id="2301.00002")
    fetcher = _FakeContentFetcher(ContentType.PDF)
    registry = {
        "karakeep_bookmark": _FakeResolver(target_ref=arxiv_ref),
        "arxiv": fetcher,
    }
    orch = Orchestrator(
        resolve_fetcher=lambda kind: registry[kind],
        resolve_converter=lambda ct, name: None,
        depth_cap=3,
    )
    result = orch._fetch(KarakeepBookmarkRef(bookmark_id="bk-ok"), depth=0)
    assert result.content_type is ContentType.PDF


# ---------------------------------------------------------------------------
# Tests: no adapter imports
# ---------------------------------------------------------------------------

def test_orchestrator_has_no_adapter_imports():
    """orchestrator.py must not transitively import adapters or wiring modules.

    We purge any previously-cached adapter/wiring entries from sys.modules, then
    reload the orchestrator module in isolation and assert that none of those
    namespaces appear in sys.modules afterwards.
    """
    import importlib
    import importlib.util

    # Remove any previously loaded adapter/wiring modules so the check is clean.
    _forbidden_prefixes = ("aizk.conversion.adapters", "aizk.conversion.wiring")
    _cached = [k for k in list(sys.modules) if any(k.startswith(p) for p in _forbidden_prefixes)]
    for key in _cached:
        del sys.modules[key]

    # Also evict the orchestrator itself so the import runs fresh.
    sys.modules.pop("aizk.conversion.core.orchestrator", None)

    importlib.import_module("aizk.conversion.core.orchestrator")

    loaded = [k for k in sys.modules if any(k.startswith(p) for p in _forbidden_prefixes)]
    assert loaded == [], (
        f"orchestrator.py transitively imported forbidden modules: {loaded}"
    )


# ---------------------------------------------------------------------------
# Tests: full fetch-convert cycle with injected fakes
# ---------------------------------------------------------------------------

def test_full_cycle_with_fakes_no_real_adapters():
    """Orchestrator completes a full fetch-convert cycle using only injected fakes."""
    fetcher = _FakeContentFetcher(ContentType.HTML)
    converter = _FakeConverter()

    registry = {"url": fetcher}
    converter_reg = {(ContentType.HTML, "fake_converter"): converter}

    orch = _make_orchestrator(registry, converter_reg)
    ref = UrlRef(url="https://example.com/page")
    artifacts = orch.process(ref, "fake_converter")

    assert artifacts.markdown == "# result"
    assert isinstance(artifacts.figures, list)
    assert isinstance(artifacts.metadata, dict)
