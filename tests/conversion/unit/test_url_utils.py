"""Unit tests for conversion URL utilities."""

import pytest

from aizk.conversion.utilities.arxiv_utils import get_arxiv_id
from aizk.conversion.utilities.bookmark_utils import detect_content_type, detect_source_type
from aizk.utilities.url_utils import normalize_url, standardize_github


def test_normalize_url_sorts_query_and_drops_fragment():
    url = "https://Example.com/path?b=2&a=1#section"
    assert normalize_url(url) == "https://example.com/path?a=1&b=2"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/1706.03762", "arxiv"),
        ("https://github.com/org/repo", "github"),
        ("https://example.com/file", "other"),
    ],
)
def test_detect_source_type(url, expected):
    assert detect_source_type(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/1706.03762", "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762v2", "1706.03762v2"),
        ("https://export.arxiv.org/html/2401.12345", "2401.12345"),
    ],
)
def test_get_arxiv_id(url, expected):
    assert get_arxiv_id(url) == expected


def test_get_arxiv_id_rejects_non_arxiv_url():
    with pytest.raises(ValueError, match="URL must be from arxiv.org"):
        get_arxiv_id("https://example.com/abs/1706.03762")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/org/repo/blob/main/README.md", "https://github.com/org/repo/tree/main/README.md"),
        ("https://github.com/org/repo", "https://github.com/org/repo"),
        ("https://example.com/path", "https://example.com/path"),
    ],
)
def test_standardize_github(url, expected):
    assert standardize_github(url) == expected
