#!/usr/bin/env python3
"""Remove bookmarks from AIZK DB and S3 artifacts.

After improving scraping with SingleFile + Karakeep, some bookmarks need to be completely reprocessed (remove & re-add).
"""

# %%
import asyncio
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import boto3
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.bookmark_utils import get_bookmark_asset_id, get_bookmark_source_url
from aizk.conversion.utilities.config import ConversionConfig
from aizk.utilities.path_utils import get_repo_path
from karakeep_client.karakeep import KarakeepClient, get_all_urls
from karakeep_client.models import Bookmark as KKBookmark

# %%
load_dotenv()

# %%
DRY_RUN = True  # "True" will preview deletions without making changes

# %%
BASE_DIR = get_repo_path(__file__)
QUERY = ""
LIMIT = 25
EXPLICIT_IDS: list[str] = []
URL_DOMAIN_FILTERS: list[str] = []
URL_CONTAINS: list[str] = []


# %%
async def query_karakeep_bookmarks(query: str) -> list[KKBookmark]:
    """Search KaraKeep bookmarks by query and collect all bookmarks.

    Args:
        query: Search query string.

    Returns:
        List of bookmarks matching the query across all paginated results.
    """
    kk_client = KarakeepClient()
    bookmarks: list[KKBookmark] = []

    results = await kk_client.search_bookmarks(q=query)
    while results:
        bookmarks.extend(b for b in results.bookmarks)
        if not results.next_cursor:
            break
        results = await kk_client.search_bookmarks(q=query, cursor=results.next_cursor)

    return bookmarks


# %%
# query karakeep for bookmarks to remove by url pattern
to_remove_ids: set[tuple[str, str]] = set()
for query in [
    "url:huggingface.co/spaces",
    "url:transformer-circuits.pub",
]:
    bookmarks = await query_karakeep_bookmarks(query=query)
    to_remove_ids.update({(b.id, get_bookmark_source_url(b)) for b in bookmarks})

# %%
# review the set to remove
print(f"Found {len(to_remove_ids)} bookmarks to remove:")
for t in to_remove_ids:
    print(t)

# %%
karakeep_ids: list[str] = [id_ for id_, url in to_remove_ids]


# %%
def _resolve_sqlite_url(database_url: str, base_dir: Path) -> str:
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
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _delete_s3_prefix(client, bucket: str, prefix: str, dry_run: bool) -> int:
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


# %%
config = ConversionConfig()
database_url = _resolve_sqlite_url(config.database_url, BASE_DIR)
s3_client = boto3.client(
    "s3",
    endpoint_url=config.s3_endpoint_url or None,
    aws_access_key_id=config.s3_access_key_id,
    aws_secret_access_key=config.s3_secret_access_key,
    region_name=config.s3_region,
)


# %%
engine = get_engine(database_url)

with Session(engine) as session:
    bookmarks = session.exec(select(Bookmark).where(Bookmark.karakeep_id.in_(karakeep_ids))).all()
    if not bookmarks:
        print("No matching bookmarks found in the AIZK database.")
    else:
        s3_prefixes: set[str] = set()
        job_ids: list[int] = []
        for bookmark in bookmarks:
            outputs = session.exec(
                select(ConversionOutput).where(ConversionOutput.aizk_uuid == bookmark.aizk_uuid)
            ).all()
            for output in outputs:
                if output.s3_prefix:
                    s3_prefixes.add(output.s3_prefix)
            jobs = session.exec(select(ConversionJob).where(ConversionJob.aizk_uuid == bookmark.aizk_uuid)).all()
            job_ids.extend([job.id for job in jobs])

        print(f"Matched {len(bookmarks)} bookmarks and {len(job_ids)} jobs")
        for s3_prefix in sorted(s3_prefixes):
            bucket, prefix = _parse_s3_uri(s3_prefix)
            deleted = _delete_s3_prefix(s3_client, bucket, prefix, DRY_RUN)
            action = "Would delete" if DRY_RUN else "Deleted"
            print(f"{action} {deleted} objects under {s3_prefix}")

        if DRY_RUN:
            print("Dry run enabled; no database rows deleted.")
        else:
            for bookmark in bookmarks:
                outputs = session.exec(
                    select(ConversionOutput).where(ConversionOutput.aizk_uuid == bookmark.aizk_uuid)
                ).all()
                for output in outputs:
                    session.delete(output)

                jobs = session.exec(select(ConversionJob).where(ConversionJob.aizk_uuid == bookmark.aizk_uuid)).all()
                for job in jobs:
                    session.delete(job)

                session.delete(bookmark)

            session.commit()
            print("Deleted bookmarks, jobs, and outputs from the AIZK database.")

# %%
