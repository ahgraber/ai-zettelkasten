"""Unit tests for GithubReadmeFetcher source_meta merge behavior.

All tests are hermetic: egress_fetch_bytes is patched at the module boundary.
"""

from __future__ import annotations

import pytest

from aizk.conversion.adapters.fetchers.github import GithubReadmeFetcher
from aizk.conversion.core.errors import GitHubReadmeNotFoundError
from aizk.conversion.core.protocols import ContentFetcher
from aizk.conversion.core.source_ref import GithubReadmeRef
from aizk.conversion.core.types import ContentType, SourceMetadata
from aizk.conversion.utilities.config import ConversionConfig

_README_BYTES = b"# My README\n\nHello world."
_README_URL = "https://raw.githubusercontent.com/owner/repo/main/README.md"


def _make_fetcher() -> GithubReadmeFetcher:
    return GithubReadmeFetcher(ConversionConfig(_env_file=None))


async def _fake_egress_success(url: str, **kwargs):
    """Return README bytes for the first matching URL."""
    if "README.md" in url and "main" in url:
        return _README_BYTES, {"content-type": "text/plain"}
    from aizk.conversion.core.errors import FetchError

    raise FetchError(f"not found: {url}")


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


def test_github_readme_fetcher_satisfies_content_fetcher_protocol() -> None:
    assert isinstance(_make_fetcher(), ContentFetcher)
    assert GithubReadmeFetcher.produces == frozenset({ContentType.HTML})


# ---------------------------------------------------------------------------
# Direct path — no resolver hop → source_meta populated from README URL
# ---------------------------------------------------------------------------


def test_github_readme_fetcher_direct_path_sets_source_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When resolver supplied no source_url, the fetcher fills it from the README URL."""
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.github.egress_fetch_bytes",
        _fake_egress_success,
    )

    result = _make_fetcher().fetch(
        GithubReadmeRef(owner="owner", repo="repo"),
        SourceMetadata(),
    )

    assert result.source_meta.source_url == _README_URL
    assert result.source_meta.document_base_url == _README_URL


def test_github_readme_fetcher_direct_path_sets_normalized_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct fetch populates normalized_url derived from the README URL."""
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.github.egress_fetch_bytes",
        _fake_egress_success,
    )

    result = _make_fetcher().fetch(
        GithubReadmeRef(owner="owner", repo="repo"),
        SourceMetadata(),
    )

    assert result.source_meta.normalized_url is not None
    assert "raw.githubusercontent.com" in result.source_meta.normalized_url


def test_github_readme_fetcher_direct_path_returns_html_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.github.egress_fetch_bytes",
        _fake_egress_success,
    )

    result = _make_fetcher().fetch(
        GithubReadmeRef(owner="owner", repo="repo"),
        SourceMetadata(),
    )

    assert result.content_type is ContentType.HTML
    assert result.content == _README_BYTES


# ---------------------------------------------------------------------------
# Resolved path — resolver-supplied source_url must survive the merge
# ---------------------------------------------------------------------------


def test_github_readme_fetcher_resolved_path_preserves_resolver_source_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver-supplied source_url wins over the constructed README URL."""
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.github.egress_fetch_bytes",
        _fake_egress_success,
    )

    resolver_meta = SourceMetadata(
        source_url="https://github.com/owner/repo",
        document_base_url="https://github.com/owner/repo",
        resolver_title="owner/repo",
    )

    result = _make_fetcher().fetch(
        GithubReadmeRef(owner="owner", repo="repo"),
        resolver_meta,
    )

    # Resolver value wins (merge: earlier non-None wins)
    assert result.source_meta.source_url == "https://github.com/owner/repo"
    assert result.source_meta.document_base_url == "https://github.com/owner/repo"
    assert result.source_meta.resolver_title == "owner/repo"


def test_github_readme_fetcher_does_not_backfill_normalized_url_when_resolver_supplied_source_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When resolver supplied source_url but left normalized_url None, fetcher MUST NOT
    backfill normalized_url from the raw README URL.

    The conversion-worker spec requires
    ``normalized_url == normalize_url(source_url)``; backfilling from a different URL
    (the raw.githubusercontent fetch URL) would violate that invariant. The correct
    behavior is to leave normalized_url None — the resolver's failed normalization
    is authoritative for the canonical source URL.
    """
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.github.egress_fetch_bytes",
        _fake_egress_success,
    )

    resolver_meta = SourceMetadata(
        source_url="https://github.com/owner/repo",
        # normalized_url intentionally left None (e.g. resolver's normalize_url raised)
    )

    result = _make_fetcher().fetch(
        GithubReadmeRef(owner="owner", repo="repo"),
        resolver_meta,
    )

    assert result.source_meta.source_url == "https://github.com/owner/repo"
    # normalized_url stays None — fetcher does not derive it from a different URL
    assert result.source_meta.normalized_url is None
    # document_base_url also not backfilled from raw URL when resolver took ownership of source_url
    assert result.source_meta.document_base_url is None


# ---------------------------------------------------------------------------
# Not-found path
# ---------------------------------------------------------------------------


def test_github_readme_fetcher_raises_when_no_readme_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from aizk.conversion.core.errors import FetchError

    async def _always_404(url: str, **kwargs):
        raise FetchError(f"404 {url}")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.github.egress_fetch_bytes",
        _always_404,
    )

    with pytest.raises(GitHubReadmeNotFoundError):
        _make_fetcher().fetch(
            GithubReadmeRef(owner="owner", repo="missing"),
            SourceMetadata(),
        )
