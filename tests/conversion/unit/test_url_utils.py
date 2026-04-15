"""Unit tests for conversion-specific URL helpers.

Pure url_utils tests (normalize_url, extract_domain, extract_urls,
deduplication) live in `tests/utilities/test_url_utils.py` to match
the url-utils spec's declared test location.
"""

import pytest

from aizk.conversion.utilities.bookmark_utils import detect_source_type


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
