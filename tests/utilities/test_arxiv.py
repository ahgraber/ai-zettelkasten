from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

import requests

from aizk.utilities.arxiv_utils import ArxivAccessDeniedError, AsyncArxivClient, get_arxiv_paper_metadata


class TestArxivAccessDeniedError:
    """Test ArxivAccessDeniedError handling."""

    def test_access_denied_error_basic(self):
        """Test basic ArxivAccessDeniedError creation."""
        error = ArxivAccessDeniedError("Test message")
        assert "Access denied to ArXiv API: Test message" in str(error)

    def test_access_denied_error_with_exit_false(self):
        """Test ArxivAccessDeniedError with exit_on_error=False."""
        error = ArxivAccessDeniedError("Test message", exit_on_error=False)
        assert "Access denied to ArXiv API: Test message" in str(error)


class TestAsyncArxivClient403Handling:
    """Test 403 error handling in AsyncArxivClient."""

    @pytest.mark.asyncio
    async def test_get_paper_metadata_403_error(self):
        """Test that 403 errors raise ArxivAccessDeniedError in get_paper_metadata."""
        client = AsyncArxivClient()

        mock_response = Mock()
        mock_response.status_code = 403

        with patch.object(client, "_ensure_client") as mock_ensure_client:
            mock_httpx_client = AsyncMock()
            mock_ensure_client.return_value = mock_httpx_client

            # Create HTTPStatusError with 403 status
            http_error = httpx.HTTPStatusError("403 Forbidden", request=Mock(), response=mock_response)
            mock_httpx_client.get.side_effect = http_error

            with pytest.raises(ArxivAccessDeniedError) as exc_info:
                await client.get_paper_metadata(["2000.56789"])

            assert "Access denied to ArXiv API" in str(exc_info.value)
            assert "HTTP 403" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_download_paper_html_403_error(self):
        """Test that 403 errors raise ArxivAccessDeniedError in download_paper_html."""
        client = AsyncArxivClient()

        mock_response = Mock()
        mock_response.status_code = 403

        with patch.object(client, "_ensure_client") as mock_ensure_client:
            mock_httpx_client = AsyncMock()
            mock_ensure_client.return_value = mock_httpx_client

            # Create HTTPStatusError with 403 status
            http_error = httpx.HTTPStatusError("403 Forbidden", request=Mock(), response=mock_response)
            mock_httpx_client.get.side_effect = http_error

            with pytest.raises(ArxivAccessDeniedError) as exc_info:
                await client.download_paper_html("2000.56789")

            assert "Access denied to ArXiv API" in str(exc_info.value)
            assert "HTTP 403" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_download_paper_pdf_403_error(self):
        """Test that 403 errors raise ArxivAccessDeniedError in download_paper_pdf."""
        client = AsyncArxivClient()

        mock_response = Mock()
        mock_response.status_code = 403

        with patch.object(client, "_ensure_client") as mock_ensure_client:
            mock_httpx_client = AsyncMock()
            mock_ensure_client.return_value = mock_httpx_client

            # Create HTTPStatusError with 403 status
            http_error = httpx.HTTPStatusError("403 Forbidden", request=Mock(), response=mock_response)
            mock_httpx_client.get.side_effect = http_error

            with pytest.raises(ArxivAccessDeniedError) as exc_info:
                await client.download_paper_pdf("2000.56789")

            assert "Access denied to ArXiv API" in str(exc_info.value)
            assert "HTTP 403" in str(exc_info.value)


class TestSyncArxiv403Handling:
    """Test 403 error handling in synchronous arxiv functions."""

    def test_get_arxiv_paper_metadata_403_error(self):
        """Test that 403 errors raise ArxivAccessDeniedError in get_arxiv_paper_metadata."""
        mock_response = Mock()
        mock_response.status_code = 403

        with patch("aizk.utilities.arxiv_utils.requests.get") as mock_get:
            # Create HTTPError that has a response attribute with 403 status
            http_error = requests.HTTPError("403 Forbidden")
            http_error.response = mock_response
            mock_get.side_effect = http_error

            with pytest.raises(ArxivAccessDeniedError) as exc_info:
                get_arxiv_paper_metadata(["2000.56789"])

            assert "Access denied to ArXiv API" in str(exc_info.value)
            assert "HTTP 403" in str(exc_info.value)
