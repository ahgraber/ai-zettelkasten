from pyleak import no_task_leaks
import pytest

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import fetcher


class _DummyBookmark:
    def __init__(self, bookmark_id: str = "bookmark-1"):
        self.id = bookmark_id


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_karakeep_asset_no_task_leaks(monkeypatch):
    class _DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def get_asset(self, asset_id: str) -> bytes:
            return f"bytes-{asset_id}".encode()

    monkeypatch.setattr(fetcher, "KarakeepClient", lambda **_kwargs: _DummyClient())

    async with no_task_leaks(action="raise"):
        result = await fetcher.fetch_karakeep_asset("asset-1")

    assert result == b"bytes-asset-1"


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_arxiv_pdf_no_task_leaks(monkeypatch):
    class _DummyArxivClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def download_paper_pdf(self, arxiv_id: str, use_export_url: bool = True) -> bytes:
            return f"pdf-{arxiv_id}-{use_export_url}".encode()

    monkeypatch.setattr(fetcher, "ArxivClient", _DummyArxivClient)

    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    async with no_task_leaks(action="raise"):
        result = await fetcher.fetch_arxiv_pdf("1234.5678", config)

    assert result == b"pdf-1234.5678-True"


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_github_readme_pages_no_task_leaks(monkeypatch):
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bookmark: "https://user.github.io/site")
    monkeypatch.setattr(fetcher, "is_github_url", lambda _url: True)
    monkeypatch.setattr(fetcher, "is_github_pages_url", lambda _url: True)

    async with no_task_leaks(action="raise"):
        result = await fetcher.fetch_github_readme(
            bookmark,
            config,
            html_content="<html>ok</html>",
        )

    assert result == b"<html>ok</html>"
