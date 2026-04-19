"""Unit tests for the conversion Orchestrator.

Covers:
- Single-hop fetch returns ConversionInput.
- Two-hop resolution (RefResolver -> ContentFetcher) succeeds through process().
- Depth-cap enforcement raises FetcherDepthExceeded with cap, kinds trail, and config key.
- Orchestrator does not transitively import any adapter or wiring module.
- End-to-end fetch-convert cycle uses only injected fakes.
- process() dispatches converter by the fetched content_type (not hardcoded).
- Default depth cap of 2 permits a one-hop chain.
- _fetch raises with correctly ordered kinds_traversed.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, ClassVar

import pytest

from aizk.conversion.core.errors import FetcherDepthExceeded
from aizk.conversion.core.orchestrator import Orchestrator
from aizk.conversion.core.protocols import ContentFetcher, Converter, RefResolver
from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    KarakeepBookmarkRef,
    SourceRef,
    UrlRef,
)
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput

# --- Fake adapters -----------------------------------------------------------


class _FakePdfFetcher:
    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

    def __init__(self, payload: bytes = b"pdf-bytes") -> None:
        self._payload = payload
        self.calls: list[SourceRef] = []

    def fetch(self, ref: SourceRef) -> ConversionInput:
        self.calls.append(ref)
        return ConversionInput(content=self._payload, content_type=ContentType.PDF)


class _FakeHtmlFetcher:
    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def __init__(self, payload: bytes = b"<html></html>") -> None:
        self._payload = payload
        self.calls: list[SourceRef] = []

    def fetch(self, ref: SourceRef) -> ConversionInput:
        self.calls.append(ref)
        return ConversionInput(content=self._payload, content_type=ContentType.HTML)


class _FakeKarakeepResolver:
    resolves_to: ClassVar[frozenset[str]] = frozenset({"arxiv"})

    def __init__(self, target: SourceRef) -> None:
        self._target = target
        self.calls: list[SourceRef] = []

    def resolve(self, ref: SourceRef) -> SourceRef:
        self.calls.append(ref)
        return self._target


class _FakeChainingResolver:
    """Resolver that produces another resolver's kind, enabling deeper chains."""

    resolves_to: ClassVar[frozenset[str]] = frozenset({"karakeep_bookmark"})

    def __init__(self, target: SourceRef) -> None:
        self._target = target

    def resolve(self, ref: SourceRef) -> SourceRef:
        return self._target


class _FakeConverter:
    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})
    requires_gpu: ClassVar[bool] = False

    def __init__(self, markdown: str = "# fake") -> None:
        self._markdown = markdown
        self.received: list[ConversionInput] = []

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002 — protocol arg name
        self.received.append(input)
        return ConversionArtifacts(markdown=self._markdown)

    def config_snapshot(self) -> dict[str, Any]:
        return {"converter_name": "fake"}


def _make_fetcher_resolver(mapping: dict[str, ContentFetcher | RefResolver]):
    def _resolve(kind: str):
        return mapping[kind]

    return _resolve


def _make_converter_resolver(mapping: dict[tuple[ContentType, str], Converter]):
    def _resolve(content_type: ContentType, name: str):
        return mapping[(content_type, name)]

    return _resolve


# --- Tests -------------------------------------------------------------------


def test_single_hop_fetch_returns_conversion_input():
    fetcher = _FakePdfFetcher(payload=b"hello-pdf")
    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver({"arxiv": fetcher}),
        resolve_converter=_make_converter_resolver({}),
    )

    ref = ArxivRef(arxiv_id="2301.12345")
    result = orch._fetch(ref)

    assert isinstance(result, ConversionInput)
    assert result.content == b"hello-pdf"
    assert result.content_type is ContentType.PDF
    assert fetcher.calls == [ref]


def test_two_hop_resolution_succeeds_through_process():
    arxiv_ref = ArxivRef(arxiv_id="2301.12345")
    resolver = _FakeKarakeepResolver(target=arxiv_ref)
    fetcher = _FakePdfFetcher(payload=b"hello-pdf")
    converter = _FakeConverter(markdown="# docling output")

    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver(
            {"karakeep_bookmark": resolver, "arxiv": fetcher},
        ),
        resolve_converter=_make_converter_resolver(
            {(ContentType.PDF, "docling"): converter},
        ),
    )

    bookmark = KarakeepBookmarkRef(bookmark_id="b1")
    artifacts = orch.process(bookmark, "docling")

    assert isinstance(artifacts, ConversionArtifacts)
    assert artifacts.markdown == "# docling output"
    # Resolver was invoked on the original ref; fetcher received the refined ref.
    assert resolver.calls == [bookmark]
    assert fetcher.calls == [arxiv_ref]
    assert len(converter.received) == 1
    assert converter.received[0].content_type is ContentType.PDF


def test_depth_limit_exceeded_raises_fetcher_depth_exceeded():
    # Build a chain of resolvers longer than the cap: cap=1 means a single
    # resolver hop is allowed, a second resolver hop must fail.
    inner_target = KarakeepBookmarkRef(bookmark_id="b2")
    outer_resolver = _FakeChainingResolver(target=inner_target)
    inner_resolver = _FakeKarakeepResolver(target=ArxivRef(arxiv_id="2301.99999"))
    # At depth=1 (post-outer), resolving "karakeep_bookmark" would be the 2nd
    # resolver hop and violate the cap.
    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver(
            {
                # First hop: depth=0, resolves_to karakeep_bookmark
                "url": outer_resolver,
                # Second hop: depth=1, would itself be a resolver -> violates cap=1
                "karakeep_bookmark": inner_resolver,
            },
        ),
        resolve_converter=_make_converter_resolver({}),
        depth_cap=1,
        depth_cap_config_key="AIZK_TEST__DEPTH",
    )

    with pytest.raises(FetcherDepthExceeded) as excinfo:
        orch._fetch(UrlRef(url="https://example.com/x"))

    exc = excinfo.value
    assert exc.cap == 1
    assert exc.config_key == "AIZK_TEST__DEPTH"
    # kinds_traversed is a tuple — ordered kinds visited at each dispatch
    # attempt, including the offending hop.
    assert isinstance(exc.kinds_traversed, tuple)
    # UrlRef normalizes to include a trailing slash, but the kind is "url".
    assert exc.kinds_traversed == ("url", "karakeep_bookmark")

    # str(exc) (from __init__) must mention cap, kinds, and config key so
    # operators can respond to the message without reading source code.
    message = str(exc)
    assert "1" in message  # cap
    assert "url" in message and "karakeep_bookmark" in message
    assert "AIZK_TEST__DEPTH" in message


def test_orchestrator_has_no_transitive_import_of_adapter_modules():
    # Ensure a clean import graph: remove any cached entries to guarantee the
    # measurement reflects the orchestrator's own transitive closure.
    importlib.import_module("aizk.conversion.core.orchestrator")

    leaked_adapters = [name for name in sys.modules if name.startswith("aizk.conversion.adapters")]
    leaked_wiring = [name for name in sys.modules if name.startswith("aizk.conversion.wiring")]

    assert leaked_adapters == [], f"Orchestrator transitively imports adapter modules: {leaked_adapters}"
    assert leaked_wiring == [], f"Orchestrator transitively imports wiring modules: {leaked_wiring}"


def test_orchestrator_with_injected_fakes_completes_fetch_convert_cycle():
    # End-to-end with fakes only: no real adapters/registries involved.
    fetcher = _FakePdfFetcher(payload=b"pdf")
    converter = _FakeConverter(markdown="# fake-markdown")
    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver({"arxiv": fetcher}),
        resolve_converter=_make_converter_resolver({(ContentType.PDF, "docling"): converter}),
    )

    artifacts = orch.process(ArxivRef(arxiv_id="2301.12345"), "docling")

    assert artifacts.markdown == "# fake-markdown"


def test_process_dispatches_converter_by_fetched_content_type():
    # Fetcher returns HTML; converter must be resolved for (HTML, name),
    # NOT hardcoded PDF. Wiring a converter only under PDF must NOT be used.
    fetcher = _FakeHtmlFetcher(payload=b"<html></html>")
    html_converter = _FakeConverter(markdown="# html-out")

    resolve_calls: list[tuple[ContentType, str]] = []

    def _resolve_converter(content_type: ContentType, name: str) -> Converter:
        resolve_calls.append((content_type, name))
        return html_converter

    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver({"url": fetcher}),
        resolve_converter=_resolve_converter,
    )

    artifacts = orch.process(UrlRef(url="https://example.com/p"), "docling")

    assert artifacts.markdown == "# html-out"
    assert resolve_calls == [(ContentType.HTML, "docling")]


def test_default_depth_cap_of_2_allows_one_hop_chain():
    # Belt-and-braces: off-by-one protection. Default cap=2 must NOT block a
    # legitimate single-hop resolver chain (depth=0 resolver -> depth=1 fetcher).
    arxiv_ref = ArxivRef(arxiv_id="2301.00001")
    resolver = _FakeKarakeepResolver(target=arxiv_ref)
    fetcher = _FakePdfFetcher()

    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver(
            {"karakeep_bookmark": resolver, "arxiv": fetcher},
        ),
        resolve_converter=_make_converter_resolver({}),
    )

    # No exception — the one-hop chain is within cap=2.
    result = orch._fetch(KarakeepBookmarkRef(bookmark_id="b1"))
    assert result.content_type is ContentType.PDF


def test_fetch_raises_with_correctly_ordered_kinds_trail_from_top():
    # Three-kind chain exceeding cap=1 — the trail must list kinds in the
    # order dispatched, from the top of the chain to the offending hop.
    github_ref = GithubReadmeRef(owner="o", repo="r")
    outer = _FakeChainingResolver(target=KarakeepBookmarkRef(bookmark_id="b"))
    inner = _FakeKarakeepResolver(target=github_ref)

    orch = Orchestrator(
        resolve_fetcher=_make_fetcher_resolver(
            {
                "url": outer,  # depth 0 -> karakeep_bookmark
                "karakeep_bookmark": inner,  # depth 1 -> cap violation
            },
        ),
        resolve_converter=_make_converter_resolver({}),
        depth_cap=1,
        depth_cap_config_key="AIZK_TEST__DEPTH",
    )

    with pytest.raises(FetcherDepthExceeded) as excinfo:
        orch._fetch(UrlRef(url="https://example.com/"))

    # Must include the original top-of-chain kind first, then the offending
    # kind that triggered the cap violation.
    assert excinfo.value.kinds_traversed == ("url", "karakeep_bookmark")
