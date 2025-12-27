"""Unit tests for conversion bookmark utilities."""

import json

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

_PDF_BOOKMARK = json.loads(
    """
    {
      "id": "kbleumlsp93mtgx4r8dc6ext",
      "createdAt": "2025-11-07T23:22:10.000Z",
      "modifiedAt": "2025-11-19T19:07:24.000Z",
      "title": "Attention Is All You Need",
      "archived": false,
      "favourited": false,
      "taggingStatus": "success",
      "summarizationStatus": "success",
      "note": null,
      "summary": null,
      "tags": [
        {
          "id": "hxnan6kdps1g58myyfv59g3t",
          "name": "Self-Attention",
          "attachedBy": "ai"
        }
      ],
      "content": {
        "type": "asset",
        "assetType": "pdf",
        "assetId": "1f9093a8-473c-4d2b-a7a5-28067155c28f",
        "fileName": "1706.03762",
        "sourceUrl": "http://export.arxiv.org/pdf/1706.03762",
        "size": 2215244.0,
        "content": "PDF content here (truncated for length)"
      },
      "assets": [
        {
          "id": "1f9093a8-473c-4d2b-a7a5-28067155c28f",
          "assetType": "bookmarkAsset"
        }
      ]
    }
    """
)

_HTML_BOOKMARK = json.loads(
    """
    {
      "id": "rpnt3mzc96g5uhovbv2runu4",
      "createdAt": "2025-07-08T01:00:00.000Z",
      "modifiedAt": "2025-07-08T01:00:07.000Z",
      "title": null,
      "archived": false,
      "favourited": false,
      "taggingStatus": "success",
      "summarizationStatus": "success",
      "note": null,
      "summary": null,
      "tags": [
        {
          "id": "b4bk2x53i0wwxwhx1ubqib2d",
          "name": "Chatbot Arena",
          "attachedBy": "ai"
        }
      ],
      "content": {
        "type": "link",
        "url": "https://aimlbling-about.ninerealmlabs.com/blog/sycophancy-planning-and-the-pepsi-challenge/",
        "title": "Sycophancy, Planning, and the Pepsi Challenge",
        "description": "Sycophancy On April 25th, we [OpenAI] rolled out an update to GPT-4o in ChatGPT that made the model noticeably more sycophantic.",
        "imageUrl": "https://github.com/ahgraber.png",
        "imageAssetId": "ac6ac94c-a265-46fa-814a-7430c207fbf3",
        "screenshotAssetId": "a6b18e96-80a1-4f15-a702-8a630dba0386",
        "fullPageArchiveAssetId": null,
        "precrawledArchiveAssetId": null,
        "videoAssetId": null,
        "favicon": "https://aimlbling-about.ninerealmlabs.com/apple-touch-icon.png",
        "htmlContent": "<div class=\\"page\\" id=\\"readability-page-1\\"><div><p>HTML content here (truncated for length)</p></div></div>",
        "contentAssetId": null,
        "crawledAt": "2025-07-08T01:00:04.000Z",
        "author": null,
        "publisher": null,
        "datePublished": "2025-07-07T04:00:00.000Z",
        "dateModified": "2025-07-07T23:19:41.000Z"
      },
      "assets": [
        {
          "id": "a6b18e96-80a1-4f15-a702-8a630dba0386",
          "assetType": "screenshot"
        },
        {
          "id": "ac6ac94c-a265-46fa-814a-7430c207fbf3",
          "assetType": "bannerImage"
        }
      ]
    }
    """
)


def test_pdf_bookmark_parsing_extracts_expected_fields():
    bookmark = Bookmark.model_validate(_PDF_BOOKMARK)

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


def test_html_bookmark_parsing_extracts_expected_fields():
    bookmark = Bookmark.model_validate(_HTML_BOOKMARK)

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
    assert exc_info.value.error_code == "missing_content"
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
