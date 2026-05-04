"""Unit tests for ArxivFetcher adapter.

All tests are hermetic: no .env, no real network calls.
"""

from __future__ import annotations

from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
from aizk.conversion.core.source_ref import ArxivRef
from aizk.conversion.core.types import ContentType, ConversionInput, SourceMetadata
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig

_EMPTY_META = SourceMetadata()


def _config() -> ConversionConfig:
    return ConversionConfig(_env_file=None, fetch_timeout_seconds=30)


def _karakeep_cfg(base_url: str = "") -> KarakeepFetcherConfig:
    return KarakeepFetcherConfig(_env_file=None, base_url=base_url, api_key="")


# ---------------------------------------------------------------------------
# Class-level structural tests
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_produces_pdf_only():
    assert ArxivFetcher.produces == frozenset({ContentType.PDF})


def test_arxiv_fetcher_satisfies_content_fetcher_structurally():
    fetcher = ArxivFetcher(_config(), _karakeep_cfg())
    assert hasattr(fetcher, "fetch")
    assert hasattr(ArxivFetcher, "produces")
    assert callable(fetcher.fetch)
    from aizk.conversion.core.protocols import ContentFetcher

    assert isinstance(fetcher, ContentFetcher)


# ---------------------------------------------------------------------------
# Step 1 — KaraKeep asset URL
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_uses_karakeep_asset_when_arxiv_pdf_url_is_karakeep_url(monkeypatch):
    pdf_bytes = b"%PDF-1.4 karakeep"
    calls = {"fetch_arxiv_pdf": 0}

    async def _fake_karakeep_asset(asset_id: str) -> bytes:
        assert asset_id == "asset-abc"
        return pdf_bytes

    async def _fake_fetch_arxiv_pdf(arxiv_id, config):
        calls["fetch_arxiv_pdf"] += 1
        return b"should not be called"

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_karakeep_asset",
        _fake_karakeep_asset,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config(), _karakeep_cfg("https://karakeep.example.com"))
    ref = ArxivRef(
        arxiv_id="2301.12345",
        arxiv_pdf_url="https://karakeep.example.com/api/v1/assets/asset-abc",
    )
    result = fetcher.fetch(ref, _EMPTY_META)

    assert isinstance(result, ConversionInput)
    assert result.content == pdf_bytes
    assert result.content_type == ContentType.PDF
    assert calls["fetch_arxiv_pdf"] == 0


# ---------------------------------------------------------------------------
# Step 2 — non-KaraKeep arxiv_pdf_url (direct HTTP)
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_uses_arxiv_pdf_url_when_non_karakeep(monkeypatch):
    pdf_bytes = b"%PDF-1.4 arxiv-pdf-url"
    seen_urls: list[str] = []

    async def _fake_egress_fetch(url, **kwargs):
        seen_urls.append(url)
        return pdf_bytes, {"content-type": "application/pdf"}

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.egress_fetch_bytes",
        _fake_egress_fetch,
    )

    fetcher = ArxivFetcher(_config(), _karakeep_cfg())
    ref = ArxivRef(
        arxiv_id="2301.12345",
        arxiv_pdf_url="https://arxiv.org/pdf/2301.12345",
    )
    result = fetcher.fetch(ref, _EMPTY_META)

    assert result.content == pdf_bytes
    assert result.content_type == ContentType.PDF
    assert seen_urls == ["https://arxiv.org/pdf/2301.12345"]


# ---------------------------------------------------------------------------
# Step 3 — no arxiv_pdf_url → arXiv API resolution
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_constructs_url_from_arxiv_id_when_no_pdf_url(monkeypatch):
    pdf_bytes = b"%PDF-1.4 abstract-page"

    async def _fake_fetch_arxiv_pdf(arxiv_id: str, config):
        assert arxiv_id == "2301.12345"
        return pdf_bytes

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config(), _karakeep_cfg())
    ref = ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url=None)
    result = fetcher.fetch(ref, _EMPTY_META)

    assert result.content == pdf_bytes
    assert result.content_type == ContentType.PDF


# ---------------------------------------------------------------------------
# Source-meta merge invariants
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_direct_path_populates_source_meta(monkeypatch):
    """Direct fetch (no resolver hop) populates source_url, document_base_url, normalized_url
    from the arXiv abstract URL."""
    pdf_bytes = b"%PDF-1.4 abstract-page"

    async def _fake_fetch_arxiv_pdf(arxiv_id: str, config):
        return pdf_bytes

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config(), _karakeep_cfg())
    result = fetcher.fetch(ArxivRef(arxiv_id="2301.12345"), _EMPTY_META)

    expected_url = "https://arxiv.org/abs/2301.12345"
    assert result.source_meta.source_url == expected_url
    assert result.source_meta.document_base_url == expected_url
    assert result.source_meta.normalized_url is not None
    assert "arxiv.org" in result.source_meta.normalized_url


def test_arxiv_fetcher_resolved_path_preserves_resolver_source_url(monkeypatch):
    """Resolver-supplied source_url wins over the constructed abstract URL."""
    pdf_bytes = b"%PDF-1.4 abstract-page"

    async def _fake_fetch_arxiv_pdf(arxiv_id: str, config):
        return pdf_bytes

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config(), _karakeep_cfg())
    resolver_meta = SourceMetadata(
        source_url="https://arxiv.org/pdf/2301.12345v2",
        document_base_url="https://arxiv.org/pdf/2301.12345v2",
        resolver_title="My Paper",
    )

    result = fetcher.fetch(ArxivRef(arxiv_id="2301.12345"), resolver_meta)

    # Resolver-supplied URL fields win (merge: earlier non-None wins).
    assert result.source_meta.source_url == "https://arxiv.org/pdf/2301.12345v2"
    assert result.source_meta.document_base_url == "https://arxiv.org/pdf/2301.12345v2"
    assert result.source_meta.resolver_title == "My Paper"


def test_arxiv_fetcher_does_not_backfill_normalized_url_when_resolver_supplied_source_url(monkeypatch):
    """When resolver supplied source_url but left normalized_url None, fetcher MUST NOT
    backfill normalized_url from the constructed abstract URL.

    Conversion-worker spec invariant:
    ``normalized_url == normalize_url(source_url)``.
    Backfilling from a different URL (the abstract page URL) when the resolver took
    ownership of the canonical source_url (e.g. a versioned or PDF arXiv URL) would
    publish a normalized form that does not correspond to source_url, breaking dedup
    and leaking the abstract URL into Source.normalized_url / manifest.normalized_url.
    """
    pdf_bytes = b"%PDF-1.4 abstract-page"

    async def _fake_fetch_arxiv_pdf(arxiv_id: str, config):
        return pdf_bytes

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config(), _karakeep_cfg())
    resolver_meta = SourceMetadata(
        source_url="https://arxiv.org/pdf/2301.12345v2",
        # normalized_url intentionally left None (e.g. resolver's normalize_url raised)
    )

    result = fetcher.fetch(ArxivRef(arxiv_id="2301.12345"), resolver_meta)

    assert result.source_meta.source_url == "https://arxiv.org/pdf/2301.12345v2"
    # normalized_url stays None — fetcher does not derive it from a different URL.
    assert result.source_meta.normalized_url is None
    # document_base_url also not backfilled when resolver owns source_url.
    assert result.source_meta.document_base_url is None
