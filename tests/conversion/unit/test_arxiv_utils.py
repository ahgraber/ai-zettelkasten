"""Unit tests for arXiv utility functions."""

from pydantic import ValidationError as PydanticValidationError
import pytest
from validators import ValidationError as URLValidatorValidationError

from aizk.conversion.utilities.arxiv_utils import (
    arxiv_abs_url,
    arxiv_html_url,
    arxiv_pdf_url,
    get_arxiv_id,
    is_arxiv_url,
    validate_arxiv_id,
)


class TestGetArxivId:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://arxiv.org/abs/1706.03762", "1706.03762"),
            ("https://arxiv.org/pdf/1706.03762v2", "1706.03762v2"),
            ("https://export.arxiv.org/html/2401.12345", "2401.12345"),
        ],
    )
    def test_get_arxiv_id(self, url, expected):
        assert get_arxiv_id(url) == expected

    def test_get_arxiv_id_rejects_non_arxiv_url(self):
        with pytest.raises(ValueError, match="URL must be from arxiv.org"):
            get_arxiv_id("https://example.com/abs/1706.03762")


class TestValidateArxivId:
    def test_valid_base_id(self):
        assert validate_arxiv_id("1706.03762") == "1706.03762"

    def test_version_and_whitespace(self):
        assert validate_arxiv_id(" 2101.00001v2 ") == "2101.00001v2"

    def test_uppercase_version(self):
        assert validate_arxiv_id("1706.03762V3") == "1706.03762V3"

    @pytest.mark.parametrize(
        "invalid_id",
        ["", "   ", "1706.037", "abcd", "2299.12345", "1706.03762vv1", "1706.03762v"],
    )
    def test_invalid_ids(self, invalid_id: str):
        with pytest.raises(ValueError):
            validate_arxiv_id(invalid_id)


class TestArxivAbsUrl:
    def test_export_url_default(self):
        """Test arxiv_abs_url with use_export_url=True (default)."""
        assert arxiv_abs_url("1706.03762") == "http://export.arxiv.org/abs/1706.03762"

    def test_export_url_true(self):
        """Test arxiv_abs_url with use_export_url=True explicitly."""
        assert arxiv_abs_url("1706.03762", use_export_url=True) == "http://export.arxiv.org/abs/1706.03762"

    def test_export_url_false(self):
        """Test arxiv_abs_url with use_export_url=False."""
        assert arxiv_abs_url("1706.03762", use_export_url=False) == "https://arxiv.org/abs/1706.03762"

    def test_with_version(self):
        """Test arxiv_abs_url with versioned arXiv ID."""
        assert arxiv_abs_url("1706.03762v1") == "http://export.arxiv.org/abs/1706.03762v1"
        assert arxiv_abs_url("1706.03762v1", use_export_url=False) == "https://arxiv.org/abs/1706.03762v1"

    def test_new_format_id(self):
        """Test arxiv_abs_url with new format arXiv ID."""
        assert arxiv_abs_url("2101.00001") == "http://export.arxiv.org/abs/2101.00001"
        assert arxiv_abs_url("2101.00001", use_export_url=False) == "https://arxiv.org/abs/2101.00001"

    def test_invalid_id(self):
        with pytest.raises(ValueError):
            arxiv_abs_url("not-an-id")


class TestArxivPdfUrl:
    def test_export_url_default(self):
        """Test arxiv_pdf_url with use_export_url=True (default)."""
        assert arxiv_pdf_url("1706.03762") == "http://export.arxiv.org/pdf/1706.03762"

    def test_export_url_true(self):
        """Test arxiv_pdf_url with use_export_url=True explicitly."""
        assert arxiv_pdf_url("1706.03762", use_export_url=True) == "http://export.arxiv.org/pdf/1706.03762"

    def test_export_url_false(self):
        """Test arxiv_pdf_url with use_export_url=False."""
        assert arxiv_pdf_url("1706.03762", use_export_url=False) == "https://arxiv.org/pdf/1706.03762"

    def test_with_version(self):
        """Test arxiv_pdf_url with versioned arXiv ID."""
        assert arxiv_pdf_url("1706.03762v1") == "http://export.arxiv.org/pdf/1706.03762v1"
        assert arxiv_pdf_url("1706.03762v1", use_export_url=False) == "https://arxiv.org/pdf/1706.03762v1"

    def test_new_format_id(self):
        """Test arxiv_pdf_url with new format arXiv ID."""
        assert arxiv_pdf_url("2101.00001") == "http://export.arxiv.org/pdf/2101.00001"
        assert arxiv_pdf_url("2101.00001", use_export_url=False) == "https://arxiv.org/pdf/2101.00001"

    def test_invalid_id(self):
        with pytest.raises(ValueError):
            arxiv_pdf_url("invalid")


class TestArxivHtmlUrl:
    def test_export_url_default(self):
        """Test arxiv_html_url with use_export_url=True (default)."""
        assert arxiv_html_url("1706.03762") == "http://export.arxiv.org/html/1706.03762"

    def test_export_url_true(self):
        """Test arxiv_html_url with use_export_url=True explicitly."""
        assert arxiv_html_url("1706.03762", use_export_url=True) == "http://export.arxiv.org/html/1706.03762"

    def test_export_url_false(self):
        """Test arxiv_html_url with use_export_url=False."""
        assert arxiv_html_url("1706.03762", use_export_url=False) == "https://arxiv.org/html/1706.03762"

    def test_with_version(self):
        """Test arxiv_html_url with versioned arXiv ID."""
        assert arxiv_html_url("1706.03762v1") == "http://export.arxiv.org/html/1706.03762v1"
        assert arxiv_html_url("1706.03762v1", use_export_url=False) == "https://arxiv.org/html/1706.03762v1"

    def test_new_format_id(self):
        """Test arxiv_html_url with new format arXiv ID."""
        assert arxiv_html_url("2101.00001") == "http://export.arxiv.org/html/2101.00001"
        assert arxiv_html_url("2101.00001", use_export_url=False) == "https://arxiv.org/html/2101.00001"

    def test_invalid_id(self):
        with pytest.raises(ValueError):
            arxiv_html_url("invalid")


class TestIsArxivUrl:
    """Test the is_arxiv_url function for detecting arxiv URLs."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://arxiv.org/abs/2000.56789",
            "https://arxiv.org/pdf/2000.56789.pdf",
            "https://export.arxiv.org/abs/2000.56789",
            "https://export.arxiv.org/pdf/2000.56789.pdf",
        ],
    )
    def test_arxiv_urls_exact_domain(self, url):
        """Test that exact arxiv domain URLs are detected correctly."""
        assert is_arxiv_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.arxiv.org/abs/2000.56789",
            "https://subdomain.arxiv.org/abs/2000.56789",
            "https://www.export.arxiv.org/abs/2000.56789",
        ],
    )
    def test_arxiv_urls_with_subdomains(self, url):
        """Test that arxiv URLs with subdomains are detected correctly."""
        assert is_arxiv_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/abs/2000.56789",
            "https://github.com/user/repo",
            "https://linkedin.com/in/someone",
            "https://arxiv.co.uk/abs/2000.56789",  # Different TLD
        ],
    )
    def test_non_arxiv_urls(self, url):
        """Test that non-arxiv URLs are not detected as arxiv."""
        assert is_arxiv_url(url) is False

    def test_invalid_url(self):
        """Test that invalid URLs raise appropriate errors."""
        with pytest.raises((PydanticValidationError, URLValidatorValidationError, ValueError)):
            is_arxiv_url("not-a-url")
