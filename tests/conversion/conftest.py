"""Shared fixtures for conversion service tests."""

from __future__ import annotations

from collections.abc import MutableMapping
import os
from pathlib import Path
import subprocess
import sys
from typing import AbstractSet, Iterator

import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.source import Source
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from karakeep_client.models import Bookmark


def make_source(karakeep_id: str, **overrides) -> Source:
    """Construct a Source row for a KaraKeep bookmark id with synthesized source_ref."""
    ref = KarakeepBookmarkRef(bookmark_id=karakeep_id)
    defaults = {
        "karakeep_id": karakeep_id,
        "source_ref": ref.model_dump(),
        "source_ref_hash": compute_source_ref_hash(ref),
    }
    defaults.update(overrides)
    return Source(**defaults)

# Env-var aliases the harness intentionally owns — kept in sync with `set_test_env` below.
# Aliases in this set survive the session-start cleanup so `set_test_env` can set them per test.
_HARNESS_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_REGION",
        "S3_BUCKET_NAME",
        "S3_ENDPOINT_URL",
        "RETRY_BASE_DELAY_SECONDS",
    }
)


def _conversion_config_aliases() -> frozenset[str]:
    return frozenset(
        field.validation_alias
        for field in ConversionConfig.model_fields.values()
        if isinstance(field.validation_alias, str)
    )


def _strip_unclaimed_aliases(
    environ: MutableMapping[str, str],
    aliases: AbstractSet[str],
    allowlist: AbstractSet[str],
) -> dict[str, str]:
    """Remove every alias (and its nested descendants) from `environ` that is not in `allowlist`.

    For nested-container aliases like `AIZK_CONVERTER`, also strips any key matching
    `AIZK_CONVERTER__*` since pydantic-settings uses `__` as a nested delimiter.
    """
    stripped: dict[str, str] = {}
    for alias in aliases - allowlist:
        if alias in environ:
            stripped[alias] = environ.pop(alias)
        prefix = f"{alias}__"
        for key in [k for k in environ if k.startswith(prefix)]:
            stripped[key] = environ.pop(key)
    return stripped


@pytest.fixture(autouse=True, scope="session")
def _hermetic_conversion_config() -> Iterator[None]:
    """Enforce the `testing` capability's hermeticity contract for every `ConversionConfig`.

    Blocks the two pydantic-settings configuration sources that would otherwise leak workstation
    state into test runs: (1) `.env` parsing is disabled for the session via `model_config`, and
    (2) shell-exported variables matching any `ConversionConfig` alias not in the harness
    allowlist are removed from `os.environ` before any test runs and restored at session end.
    """
    original_env_file = ConversionConfig.model_config.get("env_file")
    ConversionConfig.model_config["env_file"] = None

    stripped = _strip_unclaimed_aliases(os.environ, _conversion_config_aliases(), _HARNESS_ENV_ALLOWLIST)

    try:
        yield
    finally:
        ConversionConfig.model_config["env_file"] = original_env_file
        for alias, value in stripped.items():
            os.environ[alias] = value


def _resolve_repo_root() -> Path:
    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent,
    ).stdout.strip()
    return Path(repo_root)


@pytest.fixture(autouse=True)
def _ensure_repo_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _resolve_repo_root()
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        monkeypatch.setenv("PYTHONPATH", f"{repo_root}{os.pathsep}{existing}")
    else:
        monkeypatch.setenv("PYTHONPATH", str(repo_root))
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


@pytest.fixture()
def test_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a temp SQLite path for test database storage."""
    return tmp_path_factory.mktemp("conversion_db") / "conversion_service.db"


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch: pytest.MonkeyPatch, test_db_path: Path) -> None:
    """Ensure tests use a temp SQLite database and predictable settings.

    Keep every `ConversionConfig`-aliased variable set here listed in `_HARNESS_ENV_ALLOWLIST`;
    aliases absent from that set are stripped from the environment before tests run.
    """
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
    """Create and initialize a SQLite engine for tests via Alembic migrations."""
    from aizk.conversion.migrations import run_migrations

    db_url = f"sqlite:///{test_db_path}"
    run_migrations(db_url)
    return get_engine(db_url)


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
