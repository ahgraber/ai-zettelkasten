"""Unit tests for conversion bookmark utilities."""

import pytest

from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    BookmarkContentKind,
    detect_content_type,
    detect_source_type,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    get_bookmark_text_content,
    is_pdf_asset,
    resolve_bookmark_content_type,
    resolve_bookmark_type,
    validate_bookmark_content,
)
from karakeep_client.models import Bookmark


def test_pdf_bookmark_parsing_extracts_expected_fields(pdf_bookmark):
    bookmark = pdf_bookmark

    assert get_bookmark_source_url(bookmark) == "http://export.arxiv.org/pdf/1706.03762"
    assert get_bookmark_html_content(bookmark) is None
    assert get_bookmark_text_content(bookmark) is None
    assert get_bookmark_asset_id(bookmark) == "1f9093a8-473c-4d2b-a7a5-28067155c28f"
    assert is_pdf_asset(bookmark) is True
    assert detect_content_type(bookmark) == "pdf"
    assert detect_source_type(get_bookmark_source_url(bookmark)) == "arxiv"
    assert validate_bookmark_content(bookmark) is None
    assert resolve_bookmark_type(bookmark) == "asset"
    assert resolve_bookmark_content_type(bookmark) == "asset"


def test_html_bookmark_parsing_extracts_expected_fields(html_bookmark):
    bookmark = html_bookmark

    assert get_bookmark_source_url(bookmark) == (
        "https://aimlbling-about.ninerealmlabs.com/blog/sycophancy-planning-and-the-pepsi-challenge/"
    )
    assert get_bookmark_html_content(bookmark) == (
        '<div class="page" id="readability-page-1"><div><p>HTML content here (truncated for length)</p></div></div>'
    )
    assert get_bookmark_text_content(bookmark) is None
    assert get_bookmark_asset_id(bookmark) is None
    assert is_pdf_asset(bookmark) is False
    assert detect_content_type(bookmark) == "html"
    assert detect_source_type(get_bookmark_source_url(bookmark)) == "other"
    assert validate_bookmark_content(bookmark) is None
    assert resolve_bookmark_type(bookmark) == "link"
    assert resolve_bookmark_content_type(bookmark) == "link"


def test_validate_bookmark_content_rejects_empty_link():
    bookmark = Bookmark.model_validate(
        {
            "id": "empty_link",
            "createdAt": "2025-11-07T23:22:10.000Z",
            "modifiedAt": None,
            "title": "Empty",
            "archived": False,
            "favourited": False,
            "taggingStatus": "success",
            "summarizationStatus": None,
            "note": None,
            "summary": None,
            "tags": [],
            "content": {"type": "link", "url": "https://example.com"},
            "assets": [],
        }
    )
    with pytest.raises(BookmarkContentError, match="missing HTML, text, or PDF content") as exc_info:
        validate_bookmark_content(bookmark)
    assert exc_info.value.error_code == "karakeep_bookmark_missing_contents"
    assert resolve_bookmark_content_type(bookmark) == "link"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"content": {"type": "link", "url": "https://example.com"}},
            "link",
        ),
        (
            {"content": {"type": "text", "text": "Hello", "sourceUrl": "https://example.com"}},
            "text",
        ),
        (
            {"content": {"type": "asset", "assetType": "pdf", "assetId": "asset-1"}},
            "asset",
        ),
        (
            {"content": {"type": "unknown"}},
            "unknown",
        ),
    ],
)
def test_resolve_bookmark_content_type_variants(raw, expected: BookmarkContentKind):
    payload = {
        "id": "variant",
        "createdAt": "2025-11-07T23:22:10.000Z",
        "modifiedAt": None,
        "title": "Variant",
        "archived": False,
        "favourited": False,
        "taggingStatus": "success",
        "summarizationStatus": None,
        "note": None,
        "summary": None,
        "tags": [],
        "assets": [],
        **raw,
    }
    bookmark = Bookmark.model_validate(payload)
    assert resolve_bookmark_content_type(bookmark) == expected
