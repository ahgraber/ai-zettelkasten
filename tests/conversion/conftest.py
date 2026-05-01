"""Shared fixtures for conversion service tests."""

from __future__ import annotations

from collections.abc import MutableMapping
import functools
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import AbstractSet, Any, Iterator

import pytest
from sqlmodel import Session

# Install DNS stubs at conftest import time so the egress helper
# (`socket.getaddrinfo`) and Docling's `_validate_url_safety`
# (`socket.gethostbyname`) resolve any host to a public IP without leaving
# the test sandbox. Per-test overrides via `monkeypatch.setattr` still work
# because both functions are read from the `socket` module at call time.
# See `aizk.conversion.utilities.egress._resolve_with_deadline` and
# `docling.backend.html_backend._validate_url_safety`.
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GETHOSTBYNAME = socket.gethostbyname


def _public_addr_stub(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port))]


def _public_hostbyname_stub(_host: str) -> str:
    return "8.8.8.8"


socket.getaddrinfo = _public_addr_stub  # type: ignore[assignment]
socket.gethostbyname = _public_hostbyname_stub  # type: ignore[assignment]

from aizk.conversion.db import get_engine  # noqa: E402
from aizk.conversion.utilities.config import (  # noqa: E402
    ConversionConfig,
    DoclingConverterConfig,
    KarakeepFetcherConfig,
)
from karakeep_client.models import Bookmark  # noqa: E402

# Env-var aliases the harness intentionally owns — kept in sync with `set_test_env` below.
# Aliases in this set survive the session-start cleanup so `set_test_env` can set them per test.
_HARNESS_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "AIZK_DATABASE_URL",
        "AIZK_S3_ACCESS_KEY_ID",
        "AIZK_S3_SECRET_ACCESS_KEY",
        "AIZK_S3_REGION",
        "AIZK_S3_BUCKET_NAME",
        "AIZK_S3_ENDPOINT_URL",
        "AIZK_RETRY_BASE_DELAY_SECONDS",
        # FastAPI TestClient sends `Host: testserver` by default; tests that build
        # the app without overriding `AIZK_TRUSTED_HOSTS` need testserver allowed
        # alongside the loopback defaults so TrustedHostMiddleware doesn't 400 every
        # request. Per-test fixtures may override this with an explicit allowlist.
        "AIZK_TRUSTED_HOSTS",
    }
)


def _conversion_config_aliases() -> frozenset[str]:
    """Return all env-var names read by any conversion config class."""
    aliases: set[str] = set()
    for cls in (ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig):
        prefix = cls.model_config.get("env_prefix", "")
        for field_name in cls.model_fields:
            aliases.add((prefix + field_name).upper())
    return frozenset(aliases)


def _strip_unclaimed_aliases(
    environ: MutableMapping[str, str],
    aliases: AbstractSet[str],
    allowlist: AbstractSet[str],
) -> dict[str, str]:
    """Remove every alias from `environ` that is not in `allowlist`. Return what was removed."""
    stripped: dict[str, str] = {}
    for alias in aliases - allowlist:
        if alias in environ:
            stripped[alias] = environ.pop(alias)
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
    original_docling_env_file = DoclingConverterConfig.model_config.get("env_file")
    original_karakeep_env_file = KarakeepFetcherConfig.model_config.get("env_file")
    ConversionConfig.model_config["env_file"] = None
    DoclingConverterConfig.model_config["env_file"] = None
    KarakeepFetcherConfig.model_config["env_file"] = None

    stripped = _strip_unclaimed_aliases(os.environ, _conversion_config_aliases(), _HARNESS_ENV_ALLOWLIST)

    try:
        yield
    finally:
        ConversionConfig.model_config["env_file"] = original_env_file
        DoclingConverterConfig.model_config["env_file"] = original_docling_env_file
        KarakeepFetcherConfig.model_config["env_file"] = original_karakeep_env_file
        for alias, value in stripped.items():
            os.environ[alias] = value


@pytest.fixture(autouse=True, scope="session")
def _restore_socket_stubs() -> Iterator[None]:
    """Restore ``socket.getaddrinfo`` and ``socket.gethostbyname`` at session end.

    The hermetic stubs are installed at conftest import time (before any test
    fixture runs) so module-level fixtures resolve without leaving the sandbox.
    Without this fixture, the stubs persist in the ``socket`` module's globals
    after pytest exits, which can leak into other Python processes spawned from
    the same interpreter (e.g., REPLs, jupyter kernels) and into other test
    suites that share the same session.
    """
    try:
        yield
    finally:
        socket.getaddrinfo = _REAL_GETADDRINFO  # type: ignore[assignment]
        socket.gethostbyname = _REAL_GETHOSTBYNAME  # type: ignore[assignment]


@functools.lru_cache(maxsize=1)
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
    monkeypatch.setenv("AIZK_DATABASE_URL", f"sqlite:///{test_db_path}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AIZK_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AIZK_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AIZK_S3_REGION", "us-east-1")
    monkeypatch.setenv("AIZK_S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("AIZK_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AIZK_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["testserver", "localhost", "127.0.0.1"]')


@pytest.fixture()
def db_engine(test_db_path: Path):
    """Create and initialize a SQLite engine for tests via Alembic migrations.

    Disposes the engine and drops it from `_ENGINE_CACHE` at teardown so SQLite
    connection-pool file handles (db + WAL + SHM) don't accumulate across the
    session and exhaust the macOS FD soft limit during long suite runs.
    """
    from aizk.conversion.db import _ENGINE_CACHE
    from aizk.conversion.migrations import run_migrations

    db_url = f"sqlite:///{test_db_path}"
    run_migrations(db_url)
    engine = get_engine(db_url)
    try:
        yield engine
    finally:
        engine.dispose()
        _ENGINE_CACHE.pop(db_url, None)


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
