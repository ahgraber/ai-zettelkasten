#!/usr/bin/env python3
"""Demo script for KarakeepClient usage.

This script demonstrates how to use the KarakeepClient to interact with the Karakeep API.
Make sure to set KARAKEEP_API_KEY and KARAKEEP_BASEURL environment variables.
"""

# %%
import asyncio
import logging
import os
from pathlib import Path
import sys

# %%
# Add the src directory to the path so we can import treadmill
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aizk.karakeep import APIError, AuthenticationError, KarakeepClient, get_all_urls

# %%
# Set up logging
logging.basicConfig(level=logging.INFO)

aizk_logger = logging.getLogger("aizk")
aizk_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# %%
# Initialize client
client = KarakeepClient(
    # disable_response_validation=True,
    verbose=True,
)

# %%
# Get all bookmarks (or limited subset)
logger.info("Fetching first page of bookmarks...")
bookmarks_page = await client.get_bookmarks_paged(limit=5)

if client.disable_response_validation:
    logger.info("Retrieved %d bookmarks", len(bookmarks_page.get("bookmarks", [])))
else:
    logger.info("Retrieved %d bookmarks", len(bookmarks_page.bookmarks))

# %%
# Get all URLs
logger.info("Fetching all bookmark URLs...")
all_urls = await get_all_urls()
logger.info("Found %d URLs total", len(all_urls))

# Show first few URLs
for i, url in enumerate(all_urls[:5]):
    logger.info("URL %d: %s", i + 1, url)


# %%
# Search bookmarks
logger.info("Searching bookmarks for 'example'...")
search_results = await client.search_bookmarks(q="python uv", limit=3)

if client.disable_response_validation:
    search_count = len(search_results.get("bookmarks", []))
else:
    search_count = len(search_results.bookmarks)
logger.info("Found %d bookmarks matching 'example'", search_count)

# %%
# Get bookmark ID by URL
logger.info("Getting bookmark ID by URL...")
bookmark_id = await client.get_bookmark_id_by_url(url="https://www.bitecode.dev/p/a-year-of-uv-pros-cons-and-should")

if bookmark_id:
    logger.info("Found bookmark ID: %s", bookmark_id)
else:
    logger.info("No bookmark found for the given URL")


# %%
async def demo_bookmark_creation():
    """Demonstrate bookmark creation."""
    try:
        client = KarakeepClient(verbose=True)

        # Create a link bookmark
        logger.info("Creating a link bookmark...")
        link_bookmark = await client.create_bookmark(
            bookmark_type="link",
            url="https://example.com",
            title="Example Website",
            note="This is a demo bookmark created by the Karakeep client",
            favourited=True,
        )

        if client.disable_response_validation:
            bookmark_id = link_bookmark["id"]
            logger.info("Created bookmark with ID: %s", bookmark_id)
        else:
            bookmark_id = link_bookmark.id
            logger.info("Created bookmark with ID: %s", bookmark_id)

        # Create a text bookmark
        logger.info("Creating a text bookmark...")
        text_bookmark = await client.create_bookmark(
            bookmark_type="text",
            text="This is a sample text bookmark created by the Karakeep client demo.",
            title="Demo Text Bookmark",
            summary="A demonstration of text bookmark creation",
        )

        if client.disable_response_validation:
            text_bookmark_id = text_bookmark["id"]
            logger.info("Created text bookmark with ID: %s", text_bookmark_id)
        else:
            text_bookmark_id = text_bookmark.id
            logger.info("Created text bookmark with ID: %s", text_bookmark_id)

        # Get the created bookmark
        logger.info("Retrieving the created bookmark...")
        retrieved_bookmark = await client.get_bookmark(bookmark_id)

        if client.disable_response_validation:
            logger.info("Retrieved bookmark title: %s", retrieved_bookmark.get("title"))
        else:
            logger.info("Retrieved bookmark title: %s", retrieved_bookmark.title)

        # Update the bookmark
        logger.info("Updating the bookmark...")
        updated_bookmark = await client.update_bookmark(
            bookmark_id, {"title": "Updated Example Website", "archived": True}
        )
        logger.info("Updated bookmark: %s", updated_bookmark.get("title", "No title"))

        # Clean up - delete the created bookmarks
        logger.info("Cleaning up - deleting created bookmarks...")
        await client.delete_bookmark(bookmark_id)
        await client.delete_bookmark(text_bookmark_id)
        logger.info("Demo bookmarks deleted")

    except APIError:
        logger.exception("API error during bookmark operations")
    except Exception:
        logger.exception("Unexpected error during bookmark operations")


# %%
async def create_pdf_bookmark_from_url():
    """Create a PDF bookmark from a URL."""
    client = KarakeepClient()

    # Create a link bookmark for a PDF URL
    bookmark = await client.create_bookmark(
        bookmark_type="link",
        url="https://example.com/document.pdf",
        title="My Important PDF Document",  # Optional
        note="Research paper on AI safety",  # Optional
        favourited=True,  # Optional
    )

    print(f"Created bookmark: {bookmark.id}")
    return bookmark


# %%
async def create_pdf_bookmark_from_local_file():
    """Create a PDF bookmark from a local file."""
    client = KarakeepClient()

    # Step 1: Upload the local PDF file
    pdf_path = ...  # path/to/local.pdf
    asset = await client.upload_new_asset(pdf_path)

    print(f"Uploaded asset: {asset.asset_id}")

    # Step 2: Create an asset bookmark using the uploaded asset ID
    bookmark = await client.create_bookmark(
        bookmark_type="asset",
        asset_type="pdf",
        asset_id=asset.asset_id,
        title="My Local PDF Document",  # Optional
        file_name="research_paper.pdf",  # Optional
        note="Important research findings",  # Optional
        source_url=...,  # https://original-source.com/file.pdf # Optional - if you know the original source
    )

    print(f"Created bookmark: {bookmark.id}")
    return bookmark


# %%
async def demo_asset_operations():
    """Demonstrate asset upload and management."""
    try:
        client = KarakeepClient(verbose=True)

        # Create a simple text file to upload
        demo_file = Path("/tmp/karakeep_demo.txt")
        demo_file.write_text("This is a demo file for Karakeep asset upload testing.")

        logger.info("Uploading demo asset...")
        asset = await client.upload_new_asset(str(demo_file))

        if client.disable_response_validation:
            asset_id = asset["assetId"]
            logger.info("Uploaded asset with ID: %s", asset_id)
        else:
            asset_id = asset.asset_id
            logger.info("Uploaded asset with ID: %s", asset_id)

        # Create an asset bookmark
        logger.info("Creating asset bookmark...")
        asset_bookmark = await client.create_bookmark(
            bookmark_type="asset",
            asset_type="pdf",  # This might not match the actual file type, but it's just a demo
            asset_id=asset_id,
            title="Demo Asset Bookmark",
            file_name="demo.txt",
        )

        if client.disable_response_validation:
            asset_bookmark_id = asset_bookmark["id"]
            logger.info("Created asset bookmark with ID: %s", asset_bookmark_id)
        else:
            asset_bookmark_id = asset_bookmark.id
            logger.info("Created asset bookmark with ID: %s", asset_bookmark_id)

        # Retrieve the asset content
        logger.info("Retrieving asset content...")
        asset_content = await client.get_asset(asset_id)
        logger.info("Retrieved asset content: %d bytes", len(asset_content))
        logger.info("Asset content preview: %s", asset_content[:50].decode("utf-8", errors="ignore"))

        # Clean up
        logger.info("Cleaning up...")
        await client.delete_bookmark(asset_bookmark_id)
        demo_file.unlink()  # Delete the temporary file
        logger.info("Asset demo completed")

    except FileNotFoundError:
        logger.exception("File error")
    except APIError:
        logger.exception("API error during asset operations")
    except Exception:
        logger.exception("Unexpected error during asset operations")


# %%
