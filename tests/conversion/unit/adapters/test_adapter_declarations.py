"""Matrix tests for adapter-declared wiring metadata.

Per the consolidation refactor ("adapters declare, wiring reads"), each
adapter carries its own ``produces`` and ``api_submittable`` declarations
instead of those values being duplicated in ``wiring/``. These tests lock
the current declarations so any future adapter change is explicit.

If a new adapter is added, add a row here — the assertion intentionally
covers every registered kind in the default wiring.
"""

from __future__ import annotations

import pytest

from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
from aizk.conversion.adapters.fetchers.github import GithubReadmeFetcher
from aizk.conversion.adapters.fetchers.inline import InlineContentFetcher
from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver
from aizk.conversion.adapters.fetchers.singlefile import SingleFileFetcher
from aizk.conversion.adapters.fetchers.url import UrlFetcher
from aizk.conversion.core.types import ContentType


# ---------------------------------------------------------------------------
# api_submittable matrix: one row per adapter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_cls", "expected"),
    [
        # Resolvers: KaraKeep is the only submittable ingress today.
        (KarakeepBookmarkResolver, True),
        # Content fetchers: all worker-internal. External clients submit a
        # higher-level kind (e.g. karakeep_bookmark) and the resolver chain
        # refines to one of these.
        (ArxivFetcher, False),
        (GithubReadmeFetcher, False),
        (UrlFetcher, False),
        (InlineContentFetcher, False),
        # Skeleton — not registered; declaration exists for protocol hygiene.
        (SingleFileFetcher, False),
    ],
)
def test_adapter_api_submittable_declarations(adapter_cls: type, expected: bool) -> None:
    assert adapter_cls.api_submittable is expected


# ---------------------------------------------------------------------------
# produces matrix: one row per ContentFetcher adapter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fetcher_cls", "expected"),
    [
        (ArxivFetcher, frozenset({ContentType.PDF})),
        (GithubReadmeFetcher, frozenset({ContentType.HTML})),
        (UrlFetcher, frozenset({ContentType.PDF, ContentType.HTML})),
        (InlineContentFetcher, frozenset({ContentType.HTML})),
        (SingleFileFetcher, frozenset({ContentType.HTML})),
    ],
)
def test_content_fetcher_produces_declarations(
    fetcher_cls: type, expected: frozenset[ContentType]
) -> None:
    assert fetcher_cls.produces == expected
