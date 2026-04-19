"""Unit tests for InlineContentFetcher adapter.

All tests are hermetic: no .env, no network I/O.
"""

from __future__ import annotations

from aizk.conversion.adapters.fetchers.inline import InlineContentFetcher
from aizk.conversion.core.source_ref import InlineHtmlRef
from aizk.conversion.core.types import ContentType, ConversionInput

# ---------------------------------------------------------------------------
# Class-level structural tests
# ---------------------------------------------------------------------------


def test_inline_fetcher_produces_html_only():
    assert InlineContentFetcher.produces == frozenset({ContentType.HTML})


def test_inline_fetcher_satisfies_content_fetcher_structurally():
    fetcher = InlineContentFetcher()
    assert hasattr(fetcher, "fetch")
    assert hasattr(InlineContentFetcher, "produces")
    assert callable(fetcher.fetch)
    from aizk.conversion.core.protocols import ContentFetcher

    assert isinstance(fetcher, ContentFetcher)


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------


def test_inline_fetcher_returns_embedded_bytes_as_conversion_input():
    body = b"<html>hello</html>"
    ref = InlineHtmlRef(body=body)

    fetcher = InlineContentFetcher()
    result = fetcher.fetch(ref)

    assert isinstance(result, ConversionInput)
    assert result.content == b"<html>hello</html>"
    assert result.content_type == ContentType.HTML
    assert result.metadata == {}


def test_inline_fetcher_preserves_body_bytes_exactly():
    body = b"<html><body><pre>some text &amp; entities</pre></body></html>"
    ref = InlineHtmlRef(body=body)

    fetcher = InlineContentFetcher()
    result = fetcher.fetch(ref)

    assert result.content is body or result.content == body
