#!/usr/bin/env python3
"""Remove AIZK objects that should be reprocessed or are missing in KaraKeep.

Source of truth:
- KaraKeep bookmark set

Delete scope for selected targets:
- Bookmark rows in AIZK DB
- Related conversion jobs
- Related conversion outputs
- Related S3 prefixes
"""

# %%
import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from uuid import UUID

import boto3
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.utilities.path_utils import get_repo_path
from karakeep_client.karakeep import KarakeepClient
from karakeep_client.models import Bookmark as KKBookmark

# %%
load_dotenv()

# %%
DRY_RUN = True
REMOVE_MISSING_IN_KARAKEEP = True
KARAKEEP_PAGE_SIZE = 100
TABLE_LIMIT = 50

# Add IDs here for manual one-off reprocessing.
REPROCESS_KARAKEEP_IDS: set[str] = set()

BASE_DIR = get_repo_path(__file__)


# %%
@dataclass(frozen=True)
class CleanupRunConfig:
    """Runtime configuration for source cleanup."""

    dry_run: bool
    remove_missing_in_karakeep: bool
    karakeep_page_size: int
    table_limit: int
    reprocess_karakeep_ids: set[str]


def _default_run_config() -> CleanupRunConfig:
    """Build runtime config from module defaults."""
    return CleanupRunConfig(
        dry_run=DRY_RUN,
        remove_missing_in_karakeep=REMOVE_MISSING_IN_KARAKEEP,
        karakeep_page_size=KARAKEEP_PAGE_SIZE,
        table_limit=TABLE_LIMIT,
        reprocess_karakeep_ids=set(REPROCESS_KARAKEEP_IDS),
    )


def parse_args(argv: list[str] | None = None) -> CleanupRunConfig:
    """Parse CLI arguments into runtime config."""
    parser = argparse.ArgumentParser(description="Clean AIZK objects for reprocessing/missing KaraKeep targets.")
    parser.add_argument(
        "--page-size",
        type=int,
        default=KARAKEEP_PAGE_SIZE,
        help="KaraKeep page size when loading all bookmark IDs (default: 100).",
    )
    parser.add_argument(
        "--remove-missing-in-karakeep",
        action=argparse.BooleanOptionalAction,
        default=REMOVE_MISSING_IN_KARAKEEP,
        help="Remove AIZK bookmarks not present in KaraKeep (default: enabled).",
    )
    parser.add_argument(
        "--table-limit",
        type=int,
        default=TABLE_LIMIT,
        help="Maximum target rows to print in the review table (0 = all).",
    )
    parser.add_argument(
        "--reprocess-karakeep-id",
        action="append",
        default=[],
        help="Add one KaraKeep ID to manual reprocess targets. Repeat flag for multiple IDs.",
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
    parser.set_defaults(execution_mode="dry_run")

    args = parser.parse_args(argv)
    if args.page_size <= 0:
        parser.error("--page-size must be a positive integer")
    if args.table_limit < 0:
        parser.error("--table-limit must be >= 0")

    reprocess_ids = set(REPROCESS_KARAKEEP_IDS)
    reprocess_ids.update(id_.strip() for id_ in args.reprocess_karakeep_id if id_ and id_.strip())

    return CleanupRunConfig(
        dry_run=args.execution_mode == "dry_run",
        remove_missing_in_karakeep=args.remove_missing_in_karakeep,
        karakeep_page_size=args.page_size,
        table_limit=args.table_limit,
        reprocess_karakeep_ids=reprocess_ids,
    )


# %%
@dataclass(frozen=True)
class TargetRow:
    """Review row for a target bookmark."""

    karakeep_id: str
    url: str
    reasons: str
    job_count: int
    output_count: int
    s3_prefix_count: int


# %%
async def query_karakeep_bookmarks(query: str) -> list[KKBookmark]:
    """Search KaraKeep bookmarks by query and collect all pages.

    Args:
        query: KaraKeep query string.

    Returns:
        Matched bookmarks across paginated search results.
    """
    bookmarks: list[KKBookmark] = []
    async with KarakeepClient() as client:
        results = await client.search_bookmarks(q=query)
        while results:
            bookmarks.extend(results.bookmarks)
            if not results.next_cursor:
                break
            results = await client.search_bookmarks(q=query, cursor=results.next_cursor)
    return bookmarks


async def fetch_all_karakeep_ids(page_size: int = KARAKEEP_PAGE_SIZE) -> set[str]:
    """Fetch all KaraKeep bookmark IDs.

    Args:
        page_size: Number of bookmarks to fetch per page.

    Returns:
        Set of KaraKeep bookmark IDs.
    """
    karakeep_ids: set[str] = set()
    cursor: str | None = None
    async with KarakeepClient() as client:
        while True:
            page = await client.get_bookmarks_paged(
                limit=page_size,
                cursor=cursor,
                include_content=False,
            )
            karakeep_ids.update(str(bookmark.id) for bookmark in page.bookmarks)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
    return karakeep_ids


# %% [markdown]
# Example one-off (kept as reference): populate manual reprocess targets from URL pattern queries.
#
# # %%
# pattern_queries = [
#     "url:huggingface.co/spaces",
#     "url:transformer-circuits.pub",
# ]
# query_targets: set[str] = set()
# for query in pattern_queries:
#     bookmarks = await query_karakeep_bookmarks(query=query)
#     query_targets.update({str(bookmark.id) for bookmark in bookmarks})
#
# print(f"Found {len(query_targets)} query-based reprocess targets.")
# for karakeep_id in sorted(query_targets):
#     print(karakeep_id)
#
# REPROCESS_KARAKEEP_IDS.update(query_targets)


# %%
def _resolve_sqlite_url(database_url: str, base_dir: Path) -> str:
    """Resolve relative sqlite paths against the notebook directory."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return database_url
    if not url.database or url.database == ":memory:":
        return database_url
    db_path = Path(url.database)
    if db_path.is_absolute():
        return database_url
    resolved = (base_dir / db_path).resolve()
    return f"sqlite:///{resolved.as_posix()}"


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split an s3:// URI into bucket and key prefix."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _delete_s3_prefix(client, bucket: str, prefix: str, dry_run: bool) -> int:
    """Delete objects under an S3 prefix (or count them in dry run)."""
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", [])
        if not objects:
            continue
        keys = [{"Key": obj["Key"]} for obj in objects]
        if dry_run:
            deleted += len(keys)
            continue
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        deleted += len(response.get("Deleted", []))
    return deleted


def _collect_related_records(
    session: Session,
    aizk_uuids: Iterable[UUID],
) -> tuple[dict[UUID, list[ConversionJob]], dict[UUID, list[ConversionOutput]]]:
    """Load jobs and outputs grouped by bookmark UUID."""
    uuid_list = list(aizk_uuids)
    if not uuid_list:
        return {}, {}

    jobs = session.exec(select(ConversionJob).where(ConversionJob.aizk_uuid.in_(uuid_list))).all()
    outputs = session.exec(select(ConversionOutput).where(ConversionOutput.aizk_uuid.in_(uuid_list))).all()

    jobs_by_uuid: dict[UUID, list[ConversionJob]] = defaultdict(list)
    outputs_by_uuid: dict[UUID, list[ConversionOutput]] = defaultdict(list)

    for job in jobs:
        jobs_by_uuid[job.aizk_uuid].append(job)
    for output in outputs:
        outputs_by_uuid[output.aizk_uuid].append(output)

    return jobs_by_uuid, outputs_by_uuid


def _build_targets_and_reasons(
    bookmarks_by_karakeep_id: dict[str, Bookmark],
    karakeep_ids: set[str],
    *,
    remove_missing_in_karakeep: bool,
    reprocess_karakeep_ids: set[str],
) -> tuple[list[Bookmark], dict[str, list[str]], list[str]]:
    """Build target bookmarks and reason mapping.

    Args:
        bookmarks_by_karakeep_id: AIZK bookmarks keyed by KaraKeep ID.
        karakeep_ids: Current KaraKeep IDs.

    Returns:
        Tuple of:
        - Target bookmarks present in AIZK
        - Reasons by KaraKeep ID
        - Requested manual IDs that are not in AIZK
    """
    reasons_by_id: dict[str, set[str]] = defaultdict(set)

    for karakeep_id in reprocess_karakeep_ids:
        reasons_by_id[karakeep_id].add("manual_reprocess")

    if remove_missing_in_karakeep:
        missing_in_karakeep = sorted(set(bookmarks_by_karakeep_id) - karakeep_ids)
        for karakeep_id in missing_in_karakeep:
            reasons_by_id[karakeep_id].add("missing_in_karakeep")

    missing_in_aizk = sorted(
        karakeep_id for karakeep_id in reasons_by_id if karakeep_id not in bookmarks_by_karakeep_id
    )

    target_ids = sorted(karakeep_id for karakeep_id in reasons_by_id if karakeep_id in bookmarks_by_karakeep_id)
    targets = [bookmarks_by_karakeep_id[karakeep_id] for karakeep_id in target_ids]
    reason_lists = {karakeep_id: sorted(reasons_by_id[karakeep_id]) for karakeep_id in target_ids}
    return targets, reason_lists, missing_in_aizk


def _build_target_rows(
    targets: list[Bookmark],
    reasons_by_id: dict[str, list[str]],
    jobs_by_uuid: dict[UUID, list[ConversionJob]],
    outputs_by_uuid: dict[UUID, list[ConversionOutput]],
) -> list[TargetRow]:
    """Create rows for the targets + reasons review table."""
    rows: list[TargetRow] = []
    for bookmark in targets:
        outputs = outputs_by_uuid.get(bookmark.aizk_uuid, [])
        unique_prefixes = {output.s3_prefix for output in outputs if output.s3_prefix}
        rows.append(
            TargetRow(
                karakeep_id=bookmark.karakeep_id,
                url=bookmark.url or "",
                reasons=",".join(reasons_by_id.get(bookmark.karakeep_id, [])),
                job_count=len(jobs_by_uuid.get(bookmark.aizk_uuid, [])),
                output_count=len(outputs),
                s3_prefix_count=len(unique_prefixes),
            )
        )
    return rows


def _print_reason_summary(reason_lists: dict[str, list[str]]) -> None:
    """Print reason counts across all targets."""
    reason_counts: dict[str, int] = defaultdict(int)
    for reasons in reason_lists.values():
        for reason in reasons:
            reason_counts[reason] += 1

    print("Target reason counts:")
    if not reason_counts:
        print("- <none>")
        return
    for reason in sorted(reason_counts):
        print(f"- {reason}: {reason_counts[reason]}")


def _print_targets_table(rows: list[TargetRow], limit: int) -> None:
    """Print a fixed-width targets + reasons table."""
    if not rows:
        print("No target rows.")
        return

    headers = [
        "karakeep_id",
        "reasons",
        "jobs",
        "outputs",
        "s3_prefixes",
        "url",
    ]

    visible_rows = rows[:limit] if limit > 0 else rows
    table_rows = [
        [
            row.karakeep_id,
            row.reasons,
            str(row.job_count),
            str(row.output_count),
            str(row.s3_prefix_count),
            row.url,
        ]
        for row in visible_rows
    ]

    widths = [len(header) for header in headers]
    for table_row in table_rows:
        for idx, value in enumerate(table_row):
            widths[idx] = max(widths[idx], len(value))

    header_line = " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    divider = "-+-".join("-" * width for width in widths)

    print("Targets + reasons:")
    print(header_line)
    print(divider)
    for table_row in table_rows:
        print(" | ".join(value.ljust(widths[idx]) for idx, value in enumerate(table_row)))

    hidden = len(rows) - len(visible_rows)
    if hidden > 0:
        print(f"... ({hidden} more rows; increase TABLE_LIMIT to show all)")


async def main(run_config: CleanupRunConfig | None = None) -> None:
    """Run target discovery, review, and optional deletion."""
    active_config = run_config or _default_run_config()

    config = ConversionConfig()
    database_url = _resolve_sqlite_url(config.database_url, BASE_DIR)
    s3_client = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint_url or None,
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        region_name=config.s3_region,
    )
    engine = get_engine(database_url)

    with Session(engine) as session:
        bookmarks = session.exec(select(Bookmark)).all()
        if not bookmarks:
            print("No bookmarks found in the AIZK database.")
            return

        bookmarks_by_karakeep_id = {bookmark.karakeep_id: bookmark for bookmark in bookmarks}
        karakeep_ids = (
            await fetch_all_karakeep_ids(page_size=active_config.karakeep_page_size)
            if active_config.remove_missing_in_karakeep
            else set()
        )

        targets, reasons_by_id, missing_in_aizk = _build_targets_and_reasons(
            bookmarks_by_karakeep_id,
            karakeep_ids,
            remove_missing_in_karakeep=active_config.remove_missing_in_karakeep,
            reprocess_karakeep_ids=active_config.reprocess_karakeep_ids,
        )

        if missing_in_aizk:
            print(f"Manual target IDs not found in AIZK: {len(missing_in_aizk)}")
            for karakeep_id in missing_in_aizk[: active_config.table_limit]:
                print(karakeep_id)
            if len(missing_in_aizk) > active_config.table_limit:
                print(f"... ({len(missing_in_aizk) - active_config.table_limit} more)")

        if not targets:
            print("No target bookmarks matched current selection rules.")
            return

        target_uuids = [bookmark.aizk_uuid for bookmark in targets]
        jobs_by_uuid, outputs_by_uuid = _collect_related_records(session, target_uuids)
        rows = _build_target_rows(targets, reasons_by_id, jobs_by_uuid, outputs_by_uuid)

        _print_reason_summary(reasons_by_id)
        _print_targets_table(rows, active_config.table_limit)

        outputs_to_delete = {
            output.id: output
            for uuid in target_uuids
            for output in outputs_by_uuid.get(uuid, [])
            if output.id is not None
        }
        jobs_to_delete = {
            job.id: job for uuid in target_uuids for job in jobs_by_uuid.get(uuid, []) if job.id is not None
        }
        s3_prefixes = {output.s3_prefix for output in outputs_to_delete.values() if output.s3_prefix}

        print(
            "Deletion plan: "
            f"bookmarks={len(targets)} jobs={len(jobs_to_delete)} "
            f"outputs={len(outputs_to_delete)} s3_prefixes={len(s3_prefixes)}"
        )

        s3_object_count = 0
        for s3_prefix in sorted(s3_prefixes):
            bucket, prefix = _parse_s3_uri(s3_prefix)
            deleted = _delete_s3_prefix(s3_client, bucket, prefix, active_config.dry_run)
            action = "Would delete" if active_config.dry_run else "Deleted"
            print(f"{action} {deleted} objects under {s3_prefix}")
            s3_object_count += deleted
        print(f"S3 objects {'to delete' if active_config.dry_run else 'deleted'}: {s3_object_count}")

        if active_config.dry_run:
            print("Dry run enabled; no database rows deleted.")
            return

        for output in outputs_to_delete.values():
            session.delete(output)
        for job in jobs_to_delete.values():
            session.delete(job)
        for bookmark in targets:
            session.delete(bookmark)
        session.commit()

        print(
            "Deleted AIZK objects for selected targets: "
            f"bookmarks={len(targets)} jobs={len(jobs_to_delete)} outputs={len(outputs_to_delete)}"
        )


# %%
if __name__ == "__main__":
    asyncio.run(main(parse_args()))
