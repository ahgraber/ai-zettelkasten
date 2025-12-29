"""Unit tests for conversion URL utilities."""

import pytest

from aizk.conversion.utilities.arxiv_utils import get_arxiv_id
from aizk.conversion.utilities.bookmark_utils import detect_content_type, detect_source_type
from aizk.utilities.url_utils import normalize_url, standardize_github


def test_normalize_url_sorts_query_and_drops_fragment():
    url = "https://Example.com/path?b=2&a=1#section"
    assert normalize_url(url) == "https://example.com/path?a=1&b=2"


@pytest.mark.parametrize(
    ("url", "metadata", "expected"),
    [
        ("https://example.com/paper", {"content_type": "application/pdf"}, "pdf"),
        ("https://example.com/paper", {"mime_type": "text/html"}, "html"),
        ("https://example.com/paper.pdf", None, "pdf"),
        ("https://example.com/paper", None, "html"),
    ],
)
def test_detect_content_type(url, metadata, expected):
    assert detect_content_type(url, metadata) == expected


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
