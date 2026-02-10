#!/usr/bin/env python3
"""Clean up and deduplicate KaraKeep bookmarks.

Cleanup criteria:
- Empty content context
- arXiv abstract page URLs
- arXiv HTML page URLs
- Social media URLs

Deduplication winner preference:
1. PDF asset
2. precrawledArchive asset
3. Recency (newer wins)
"""

# %%
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
from typing import Iterable

from dotenv import load_dotenv
from setproctitle import setproctitle

# Add project src directory to import path.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    get_bookmark_source_url,
)
from aizk.utilities.url_utils import is_social_url, normalize_url
from karakeep_client.karakeep import KarakeepClient
from karakeep_client.models import Bookmark as KKBookmark

# %%
setproctitle(Path(__file__).stem)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments controlling cleanup and dedupe behavior."""
    parser = argparse.ArgumentParser(description="Clean up and deduplicate KaraKeep bookmarks.")
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Number of bookmarks to fetch per API page (default: 100).",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--apply",
        dest="execution_mode",
        action="store_const",
        const="apply",
        help="Apply deletions. Default behavior is dry run.",
    )
    mode_group.add_argument(
        "--dry-run",
        dest="execution_mode",
        action="store_const",
        const="dry_run",
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--no-dry-run",
        dest="execution_mode",
        action="store_const",
        const="apply",
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable deletion for cleanup categories (default: enabled).",
    )
    parser.add_argument(
        "--deduplicate",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable deletion of duplicate losers (default: disabled).",
    )
    parser.set_defaults(execution_mode="dry_run")
    args = parser.parse_args()
    args.dry_run = args.execution_mode == "dry_run"
    args.apply = not args.dry_run
    if args.page_size <= 0:
        parser.error("--page-size must be a positive integer")
    return args


def _safe_get_bookmark_source_url(bookmark: KKBookmark) -> str | None:
    """Return source URL when available, else None."""
    try:
        return get_bookmark_source_url(bookmark)
    except BookmarkContentError:
        return None


def is_pdf_asset(bookmark: KKBookmark) -> bool:
    """Return whether bookmark content is a PDF asset."""
    content = getattr(bookmark, "content", None)
    return bool(
        content and getattr(content, "type", None) == "asset" and getattr(content, "asset_type", None) == "pdf"
    )


def is_precrawled_archive_asset(bookmark: KKBookmark) -> bool:
    """Return whether bookmark has a precrawled archive asset."""
    if any(asset.asset_type == "precrawledArchive" for asset in (bookmark.assets or [])):
        return True

    content = getattr(bookmark, "content", None)
    if content and getattr(content, "type", None) == "asset":
        return getattr(content, "asset_type", None) == "precrawledArchive"

    return bool(content and getattr(content, "precrawled_archive_asset_id", None))


def has_protected_asset(bookmark: KKBookmark) -> bool:
    """Return whether bookmark has assets we should preserve during cleanup."""
    return is_pdf_asset(bookmark) or is_precrawled_archive_asset(bookmark)


def _safe_normalize_url(url: str) -> str:
    """Normalize a URL when possible, else return the original input."""
    try:
        return normalize_url(url)
    except ValueError:
        return url


def _safe_is_social_url(url: str) -> bool:
    """Return whether a URL is social media, tolerating invalid URLs."""
    try:
        return is_social_url(url)
    except ValueError:
        return False


def _coerce_datetime(value: object) -> datetime | None:
    """Coerce known datetime representations into ``datetime`` objects."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None
    return None


def _bookmark_recency(bookmark: KKBookmark) -> float:
    """Return a monotonic timestamp proxy for bookmark freshness."""
    modified_at = _coerce_datetime(getattr(bookmark, "modified_at", None))
    updated_at = _coerce_datetime(getattr(bookmark, "updated_at", None))
    created_at = _coerce_datetime(getattr(bookmark, "created_at", None))
    timestamp = modified_at or updated_at or created_at
    if timestamp is None:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


def _bookmark_score(bookmark: KKBookmark) -> tuple[bool, bool, float, str]:
    """Build dedupe winner score tuple for deterministic max() selection."""
    return (
        is_pdf_asset(bookmark),
        is_precrawled_archive_asset(bookmark),
        _bookmark_recency(bookmark),
        str(bookmark.id),
    )


def _has_nonempty_text(value: object) -> bool:
    """Return whether a value contains non-whitespace text."""
    return isinstance(value, str) and bool(value.strip())


def is_context_empty(bookmark: KKBookmark) -> bool:
    """Return whether bookmark content lacks usable context."""
    content = getattr(bookmark, "content", None)
    if content is None:
        return True

    content_type = getattr(content, "type", None)
    if content_type == "asset":
        return False

    if content_type == "text":
        return not _has_nonempty_text(getattr(content, "text", None))

    if content_type == "link":
        if _has_nonempty_text(getattr(content, "html_content", None)):
            return False

        content_asset_id = getattr(content, "content_asset_id", None)
        if _has_nonempty_text(content_asset_id):
            return False

        return not any(getattr(asset, "asset_type", None) == "linkHtmlContent" for asset in (bookmark.assets or []))

    return True


def _is_arxiv_abs_url(url: str) -> bool:
    """Return whether the URL points to an arXiv abstract page."""
    return "arxiv.org/abs/" in url.lower()


def _is_arxiv_html_url(url: str) -> bool:
    """Return whether the URL points to an arXiv HTML page."""
    return "arxiv.org/html/" in url.lower()


def _log_id_sample(label: str, bookmark_ids: list[str], limit: int = 25) -> None:
    """Log a bounded sample of bookmark IDs for quick inspection."""
    logger.info("%s: %d", label, len(bookmark_ids))
    for bookmark_id in bookmark_ids[:limit]:
        logger.info("%s id=%s", label, bookmark_id)
    remaining = len(bookmark_ids) - limit
    if remaining > 0:
        logger.info("%s: ... (%d more)", label, remaining)


async def _get_all_bookmarks(client: KarakeepClient, page_size: int) -> list[KKBookmark]:
    """Fetch all bookmarks from KaraKeep.

    Args:
        client: Initialized KaraKeep client.
        page_size: Number of bookmarks per page.

    Returns:
        All bookmarks with content included.
    """
    bookmarks: list[KKBookmark] = []
    cursor: str | None = None

    while True:
        page = await client.get_bookmarks_paged(
            limit=page_size,
            cursor=cursor,
            include_content=True,
        )
        bookmarks.extend(page.bookmarks)
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    return bookmarks


def _collect_cleanup_candidates(bookmarks: list[KKBookmark]) -> dict[str, set[str]]:
    """Collect bookmark IDs that match cleanup categories.

    Args:
        bookmarks: Bookmarks to inspect.

    Returns:
        Mapping from cleanup category to matching bookmark IDs.
    """
    categories: dict[str, set[str]] = {
        "missing_source_url": set(),
        "empty_context": set(),
        "arxiv_abs": set(),
        "arxiv_html": set(),
        "social": set(),
    }

    for bookmark in bookmarks:
        bookmark_id = str(bookmark.id)
        protected = has_protected_asset(bookmark)

        if not protected and is_context_empty(bookmark):
            categories["empty_context"].add(bookmark_id)

        try:
            source_url = get_bookmark_source_url(bookmark)
        except BookmarkContentError:
            categories["missing_source_url"].add(bookmark_id)
            continue

        if protected:
            continue

        if _is_arxiv_abs_url(source_url):
            categories["arxiv_abs"].add(bookmark_id)
        if _is_arxiv_html_url(source_url):
            categories["arxiv_html"].add(bookmark_id)
        if _safe_is_social_url(source_url):
            categories["social"].add(bookmark_id)

    return categories


def _build_cleanup_reason_map(
    cleanup_categories: dict[str, set[str]],
    cleanup_rules: Iterable[str],
) -> dict[str, list[str]]:
    """Map bookmark IDs to cleanup rule names."""
    reason_map: dict[str, list[str]] = defaultdict(list)

    for rule in cleanup_rules:
        for bookmark_id in cleanup_categories.get(rule, set()):
            reason_map[bookmark_id].append(rule)

    for reasons in reason_map.values():
        reasons.sort()

    return dict(reason_map)


def _collect_duplicate_losers(
    bookmarks: list[KKBookmark],
) -> tuple[list[KKBookmark], dict[str, list[KKBookmark]], list[str]]:
    """Compute duplicate losers using URL+quality winner rules.

    Args:
        bookmarks: Bookmarks to deduplicate.

    Returns:
        A tuple containing:
            - loser bookmarks
            - duplicate groups keyed by normalized URL
            - bookmark IDs missing source URLs
    """
    by_url: dict[str, list[KKBookmark]] = defaultdict(list)
    missing_source_url: list[str] = []

    for bookmark in bookmarks:
        try:
            source_url = get_bookmark_source_url(bookmark)
        except BookmarkContentError:
            missing_source_url.append(str(bookmark.id))
            continue
        normalized = _safe_normalize_url(source_url)
        by_url[normalized].append(bookmark)

    duplicate_groups = {url: group for url, group in by_url.items() if len(group) > 1}

    losers: list[KKBookmark] = []
    for url, group in duplicate_groups.items():
        winner = max(group, key=_bookmark_score)
        group_losers = [bookmark for bookmark in group if bookmark.id != winner.id]
        losers.extend(group_losers)
        logger.info(
            "Duplicate URL=%s winner=%s losers=%s",
            url,
            winner.id,
            ",".join(str(bookmark.id) for bookmark in group_losers),
        )

    return losers, duplicate_groups, missing_source_url


async def _delete_bookmarks(
    client: KarakeepClient,
    bookmark_ids: list[str],
    *,
    enabled: bool,
    dry_run: bool,
    label: str,
    bookmark_urls: dict[str, str | None] | None = None,
    bookmark_reasons: dict[str, list[str]] | None = None,
) -> int:
    """Delete bookmarks, optionally in dry-run mode.

    Args:
        client: Initialized KaraKeep client.
        bookmark_ids: Bookmark IDs to delete.
        enabled: Whether deletion is enabled for this category.
        dry_run: Whether to log deletion without performing it.
        label: Log label for this deletion phase.
        bookmark_urls: Optional mapping from bookmark ID to source URL.
        bookmark_reasons: Optional mapping from bookmark ID to cleanup reasons.

    Returns:
        Number of bookmarks processed for deletion.
    """
    if not bookmark_ids:
        logger.info("No bookmarks to delete for %s", label)
        return 0

    if not enabled:
        logger.info("Deletion disabled for %s (%d bookmarks)", label, len(bookmark_ids))
        return 0

    deleted_count = 0
    for bookmark_id in bookmark_ids:
        bookmark_url = bookmark_urls.get(bookmark_id) if bookmark_urls else None
        reasons = bookmark_reasons.get(bookmark_id) if bookmark_reasons else None
        reason_text = ",".join(reasons) if reasons else "<unknown>"

        if dry_run:
            logger.info(
                "Dry run: would delete %s bookmark id=%s url=%s reasons=%s",
                label,
                bookmark_id,
                bookmark_url or "<unknown>",
                reason_text,
            )
            deleted_count += 1
            continue

        try:
            await client.delete_bookmark(bookmark_id)
        except Exception as exc:
            logger.warning(
                "Failed deleting %s bookmark id=%s url=%s reasons=%s: %s",
                label,
                bookmark_id,
                bookmark_url or "<unknown>",
                reason_text,
                exc,
            )
            continue

        logger.info(
            "Deleted %s bookmark id=%s url=%s reasons=%s",
            label,
            bookmark_id,
            bookmark_url or "<unknown>",
            reason_text,
        )
        deleted_count += 1

    return deleted_count


async def main() -> None:
    """Run combined bookmark cleanup and deduplication workflow."""
    args = parse_args()
    _ = load_dotenv()

    async with KarakeepClient() as client:
        bookmarks = await _get_all_bookmarks(client, page_size=args.page_size)
        bookmark_urls = {str(bookmark.id): _safe_get_bookmark_source_url(bookmark) for bookmark in bookmarks}

        logger.info("Loaded %d bookmarks", len(bookmarks))

        cleanup_categories = _collect_cleanup_candidates(bookmarks)
        cleanup_rules = ("empty_context", "arxiv_abs", "arxiv_html", "social")
        cleanup_delete_ids = set().union(*(cleanup_categories[rule] for rule in cleanup_rules))
        cleanup_reason_map = _build_cleanup_reason_map(cleanup_categories, cleanup_rules)

        logger.info("Cleanup candidates: empty_context=%d", len(cleanup_categories["empty_context"]))
        logger.info("Cleanup candidates: arxiv_abs=%d", len(cleanup_categories["arxiv_abs"]))
        logger.info("Cleanup candidates: arxiv_html=%d", len(cleanup_categories["arxiv_html"]))
        logger.info("Cleanup candidates: social=%d", len(cleanup_categories["social"]))
        logger.info("Cleanup candidates total: %d", len(cleanup_delete_ids))

        missing_source_cleanup = sorted(cleanup_categories["missing_source_url"])
        _log_id_sample("Missing source_url (all bookmarks)", missing_source_cleanup)

        remaining_bookmarks = [bookmark for bookmark in bookmarks if str(bookmark.id) not in cleanup_delete_ids]
        logger.info(
            "Bookmarks kept for dedupe after cleanup filtering: %d",
            len(remaining_bookmarks),
        )

        duplicate_losers, duplicate_groups, missing_source_dedupe = _collect_duplicate_losers(remaining_bookmarks)
        logger.info("Found %d duplicate URL groups", len(duplicate_groups))
        logger.info("Duplicate losers total: %d", len(duplicate_losers))
        _log_id_sample("Missing source_url (dedupe set)", sorted(missing_source_dedupe))

        cleanup_deleted = await _delete_bookmarks(
            client,
            sorted(cleanup_delete_ids),
            enabled=args.cleanup,
            dry_run=args.dry_run,
            label="cleanup",
            bookmark_urls=bookmark_urls,
            bookmark_reasons=cleanup_reason_map,
        )
        duplicate_deleted = await _delete_bookmarks(
            client,
            [str(bookmark.id) for bookmark in duplicate_losers],
            enabled=args.deduplicate,
            dry_run=args.dry_run,
            label="dedupe",
            bookmark_urls=bookmark_urls,
        )

    logger.info("Processed cleanup deletions: %d", cleanup_deleted)
    logger.info("Processed dedupe deletions: %d", duplicate_deleted)


# %%
if __name__ == "__main__":
    asyncio.run(main())
