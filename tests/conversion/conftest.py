"""Shared fixtures for conversion service tests."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from sqlmodel import Session

from aizk.db import create_db_and_tables, get_engine
from karakeep_client.models import Bookmark


@pytest.fixture(scope="session")
def test_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a temp SQLite path for test database storage."""
    return tmp_path_factory.mktemp("conversion_db") / "conversion_service.db"


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch: pytest.MonkeyPatch, test_db_path: Path) -> None:
    """Ensure tests use a temp SQLite database and predictable settings."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db_path}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "0")


@pytest.fixture()
def db_engine(test_db_path: Path):
    """Create and initialize a SQLite engine for tests."""
    engine = get_engine(f"sqlite:///{test_db_path}")
    create_db_and_tables(engine)
    return engine


@pytest.fixture()
def db_session(db_engine) -> Iterator[Session]:
    """Provide a SQLModel session tied to the test database."""
    with Session(db_engine) as session:
        yield session


_PDF_BOOKMARK = {
    "id": "kbleumlsp93mtgx4r8dc6ext",
    "createdAt": "2025-11-07T23:22:10.000Z",
    "modifiedAt": "2025-11-19T19:07:24.000Z",
    "title": "Attention Is All You Need",
    "archived": False,
    "favourited": False,
    "taggingStatus": "success",
    "summarizationStatus": "success",
    "note": None,
    "summary": None,
    "tags": [{"id": "hxnan6kdps1g58myyfv59g3t", "name": "Self-Attention", "attachedBy": "ai"}],
    "content": {
        "type": "asset",
        "assetType": "pdf",
        "assetId": "1f9093a8-473c-4d2b-a7a5-28067155c28f",
        "fileName": "1706.03762",
        "sourceUrl": "http://export.arxiv.org/pdf/1706.03762",
        "size": 2215244.0,
        "content": "PDF content here (truncated for length)",
    },
    "assets": [{"id": "1f9093a8-473c-4d2b-a7a5-28067155c28f", "assetType": "bookmarkAsset"}],
}

_HTML_BOOKMARK = {
    "id": "rpnt3mzc96g5uhovbv2runu4",
    "createdAt": "2025-07-08T01:00:00.000Z",
    "modifiedAt": "2025-07-08T01:00:07.000Z",
    "title": None,
    "archived": False,
    "favourited": False,
    "taggingStatus": "success",
    "summarizationStatus": "success",
    "note": None,
    "summary": None,
    "tags": [{"id": "b4bk2x53i0wwxwhx1ubqib2d", "name": "Chatbot Arena", "attachedBy": "ai"}],
    "content": {
        "type": "link",
        "url": "https://aimlbling-about.ninerealmlabs.com/blog/sycophancy-planning-and-the-pepsi-challenge/",
        "title": "Sycophancy, Planning, and the Pepsi Challenge",
        "description": (
            "Sycophancy On April 25th, we [OpenAI] rolled out an update to GPT-4o in ChatGPT "
            "that made the model noticeably more sycophantic."
        ),
        "imageUrl": "https://github.com/ahgraber.png",
        "imageAssetId": "ac6ac94c-a265-46fa-814a-7430c207fbf3",
        "screenshotAssetId": "a6b18e96-80a1-4f15-a702-8a630dba0386",
        "fullPageArchiveAssetId": None,
        "precrawledArchiveAssetId": None,
        "videoAssetId": None,
        "favicon": "https://aimlbling-about.ninerealmlabs.com/apple-touch-icon.png",
        "htmlContent": '<div class="page" id="readability-page-1"><div><p>HTML content here (truncated for length)</p></div></div>',
        "contentAssetId": None,
        "crawledAt": "2025-07-08T01:00:04.000Z",
        "author": None,
        "publisher": None,
        "datePublished": "2025-07-07T04:00:00.000Z",
        "dateModified": "2025-07-07T23:19:41.000Z",
    },
    "assets": [
        {"id": "a6b18e96-80a1-4f15-a702-8a630dba0386", "assetType": "screenshot"},
        {"id": "ac6ac94c-a265-46fa-814a-7430c207fbf3", "assetType": "bannerImage"},
    ],
}


@pytest.fixture()
def pdf_bookmark() -> Bookmark:
    """Return a parsed KaraKeep PDF bookmark fixture."""
    return Bookmark.model_validate(_PDF_BOOKMARK)


@pytest.fixture()
def html_bookmark() -> Bookmark:
    """Return a parsed KaraKeep HTML bookmark fixture."""
    return Bookmark.model_validate(_HTML_BOOKMARK)
