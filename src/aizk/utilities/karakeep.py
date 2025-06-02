# %%
import json
import logging
import os
from urllib.parse import urljoin

from dotenv import load_dotenv
import httpx

logger = logging.getLogger(__name__)
load_dotenv()

# %%
limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
client = httpx.AsyncClient(limits=limits)


# %%
async def _get_bookmarks_api(next_cursor: str | None = None, limit: int = 100):
    """Retrieve hoarded bookmarks (in chunks)."""
    karakeep_api_key = os.environ["KARAKEEP_API_KEY"]
    karakeep_url = os.environ["KARAKEEP_API_URL"]

    if limit > 100:
        raise ValueError("Max limit is 100.")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {karakeep_api_key}",
    }

    params = {"limit": limit}
    if next_cursor:
        params["cursor"] = next_cursor

    response = await client.get(
        url=urljoin(karakeep_url, "/api/v1/bookmarks"),
        headers=headers,
        params=params,
        timeout=30,
    )
    return response.json()


async def list_bookmarks(cursor: str | None = None) -> list[dict]:
    """Get all bookmarks from Karakeep."""
    bookmarks = await _get_bookmarks_api(next_cursor=cursor)

    # Get current page bookmarks
    current_bookmarks = bookmarks["bookmarks"]

    # Base case: no more pages
    if not bookmarks["nextCursor"]:
        return current_bookmarks

    # Recursive case: get next page and combine
    next_bookmarks = await list_bookmarks(cursor=bookmarks["nextCursor"])
    return current_bookmarks + next_bookmarks


async def get_bookmark(bookmark_id: str, include_content: bool = True) -> dict:
    """Get a bookmark by its ID."""
    karakeep_api_key = os.environ["KARAKEEP_API_KEY"]
    karakeep_url = os.environ["KARAKEEP_API_URL"]

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {karakeep_api_key}",
    }
    params = {}
    if include_content:
        params["includeContent"] = "true"

    response = await client.get(
        url=urljoin(karakeep_url, f"/api/v1/bookmarks/{bookmark_id}"),
        headers=headers,
        params=params,
        timeout=30,
    )
    return response.json()


# %%
# TODO: get GET /api/assets/<asset_id>


async def get_bookmark_asset(bookmark_id: str, asset_id: str):
    """Get a specific asset from a bookmark."""
    karakeep_api_key = os.environ["KARAKEEP_API_KEY"]
    karakeep_url = os.environ["KARAKEEP_API_URL"]

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {karakeep_api_key}",
    }

    response = await client.get(
        url=urljoin(karakeep_url, f"/api/v1/bookmarks/{bookmark_id}/assets/{asset_id}"),
        headers=headers,
        timeout=30,
    )
    # TODO: what about files?
    return response.json()
