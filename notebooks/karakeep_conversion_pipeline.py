#!/usr/bin/env python3
"""Submit KaraKeep bookmarks to the conversion pipeline."""

# %% [markdown]
# # KaraKeep → conversion pipeline
#
# This notebook pages through all KaraKeep bookmarks and submits each bookmark ID
# to the conversion service. It expects `KARAKEEP_API_KEY` and `KARAKEEP_BASE_URL`
# to be set (or available via `.env`/direnv).
#
# Optional environment variables:
# - `CONVERSION_API_BASE_URL` (default: `http://localhost:8000`)
# - `KARAKEEP_PAGE_LIMIT` (default: `100`, max: `100`)
# - `KARAKEEP_DRY_RUN` (default: `false`; set to `true` to log IDs without submitting)

# %% [markdown]
# ## Start the API + worker in background processes
#
# Run these in a terminal before executing the cells below:
#
# ```bash
# mkdir -p data/logs
# uv run python -m aizk.conversion.cli db-init
# KARAKEEP_API_KEY="$KARAKEEP_API_KEY" KARAKEEP_BASE_URL="$KARAKEEP_BASE_URL" uv run python -m aizk.conversion.cli serve > data/logs/conversion-api.log 2>&1 &
# KARAKEEP_API_KEY="$KARAKEEP_API_KEY" KARAKEEP_BASE_URL="$KARAKEEP_BASE_URL" uv run python -m aizk.conversion.cli worker > data/logs/conversion-worker.log 2>&1 &
# ```
#
# The server listens on `http://localhost:8000` by default. Set
# `CONVERSION_API_BASE_URL` if you use a different host/port.

# %% [markdown]
# ## Liveness check
#
# Verify the API is up before submitting jobs:
#
# ```bash
# curl -sS http://localhost:8000/v1/jobs | head -c 200
# ```

# %%
import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv
import httpx

import tenacity

from aizk.utilities.limiters import retry
from karakeep_client.karakeep import KarakeepClient

# %%
_ = load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# %%
DEFAULT_CONVERSION_API_BASE_URL = "http://localhost:8000"
DEFAULT_PAGE_LIMIT = 100


def resolve_conversion_api_base_url() -> str:
    """Return the conversion API base URL."""
    return os.environ.get("CONVERSION_API_BASE_URL", DEFAULT_CONVERSION_API_BASE_URL)


@retry()
async def submit_bookmark(
    http_client: httpx.AsyncClient,
    karakeep_id: str,
) -> dict[str, Any]:
    """Submit a single bookmark ID to the conversion API.

    Args:
        http_client: HTTP client configured with the conversion API base URL.
        karakeep_id: KaraKeep bookmark ID to submit.

    Returns:
        The conversion job response payload.
    """
    payload = {
        "karakeep_id": karakeep_id,
        "idempotency_key": f"karakeep:{karakeep_id}",
    }
    response = await http_client.post("/v1/jobs", json=payload)
    response.raise_for_status()
    return response.json()


async def submit_all_bookmarks(
    page_size: int = DEFAULT_PAGE_LIMIT,
    n_pages: int | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Page through KaraKeep bookmarks and submit each one.

    Args:
        page_size: Page size for KaraKeep pagination (max 100).
        n_pages: Limit submissions to this many pages (None = all pages).
        dry_run: When true, only log bookmark IDs without submitting.

    Returns:
        Tuple of (submitted_count, failed_count).
    """
    karakeep_client = KarakeepClient()
    base_url = resolve_conversion_api_base_url()
    submitted = 0
    failed = 0
    cursor: str | None = None

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as http_client:
        pages_processed = 0
        while True:
            if n_pages is not None and pages_processed >= n_pages:
                break
            page = await karakeep_client.get_bookmarks_paged(
                limit=page_size,
                cursor=cursor,
                include_content=False,
            )
            logger.info("Loaded %d bookmarks (cursor=%s)", len(page.bookmarks), cursor)

            for bookmark in page.bookmarks:
                if dry_run:
                    logger.info("Dry run: would submit bookmark %s", bookmark.id)
                    submitted += 1
                    continue
                try:
                    await submit_bookmark(http_client, bookmark.id)
                except httpx.HTTPError:
                    failed += 1
                    logger.exception("Submission failed for bookmark %s", bookmark.id)
                else:
                    submitted += 1

            pages_processed += 1
            if not page.next_cursor:
                break
            cursor = page.next_cursor

    return submitted, failed


async def list_jobs(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Fetch jobs from the conversion API."""
    base_url = resolve_conversion_api_base_url()
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as http_client:
        response = await http_client.get("/v1/jobs", params=params)
        response.raise_for_status()
        return response.json()


async def summarize_job_statuses(limit: int = 200) -> dict[str, int]:
    """Summarize job statuses from the latest jobs."""
    payload = await list_jobs(limit=limit, offset=0)
    counts: dict[str, int] = {}
    for job in payload.get("jobs", []):
        status = str(job.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


# %%
submitted_count, failed_count = await submit_all_bookmarks(
    page_size=10,
    n_pages=2,
    dry_run=False,
)
logger.info(
    "Submitted %d bookmarks (%d failed).",
    submitted_count,
    failed_count,
)


# %%
recent_jobs = await list_jobs(limit=50)
recent_jobs.get("jobs", [])[:5]


# %%
status_summary = await summarize_job_statuses(limit=200)
status_summary

# %% [markdown]
# ## Stop the API + worker
#
# ```sh
# pkill -f "aizk.conversion.cli serve"
# pkill -f "aizk.conversion.cli worker"
# ```
