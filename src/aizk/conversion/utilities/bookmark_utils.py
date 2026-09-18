"""Re-export the bookmark validation and content inspection utilities for importers written against this path."""

from __future__ import annotations

from aizk.conversion.egress.bookmark_utils import (
    BookmarkContentError,
    BookmarkContentKind,
    detect_content_type,
    detect_source_type,
    fetch_karakeep_bookmark,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    get_bookmark_text_content,
    is_pdf_asset,
    is_precrawled_archive_asset,
    resolve_bookmark_content_type,
    resolve_bookmark_type,
    validate_bookmark_content,
)

__all__ = [
    "BookmarkContentKind",
    "BookmarkContentError",
    "detect_content_type",
    "detect_source_type",
    "fetch_karakeep_bookmark",
    "get_bookmark_asset_id",
    "get_bookmark_html_content",
    "get_bookmark_source_url",
    "get_bookmark_text_content",
    "is_pdf_asset",
    "is_precrawled_archive_asset",
    "resolve_bookmark_content_type",
    "resolve_bookmark_type",
    "validate_bookmark_content",
]
