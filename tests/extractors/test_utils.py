import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
import responses  # For mocking HTTP requests

from aizk.extractors.utils import atomic_write, download_file, validate_file


class TestAtomicWrite:
    def test_write_str(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        with atomic_write(tmp_path / name, binary_mode=False) as f:
            f.write(content)

        assert (tmp_path / name).read_text() == content
        assert len(list(tmp_path.iterdir())) == 1

    def test_write_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, binary_mode should be True
        with atomic_write(tmp_path / name, binary_mode=True) as f:
            f.write(content.encode("utf-8"))

        assert (tmp_path / name).read_text() == content
        assert len(list(tmp_path.iterdir())) == 1

    def test_write_needs_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is encoded, binary_mode should be True
        with (
            pytest.raises(TypeError),
            atomic_write(tmp_path / name, binary_mode=False) as f,
        ):
            f.write(content.encode("utf-8"))

    def test_write_extra_binary(self, tmp_path):
        name = "test.txt"
        content = "this is only a test"

        # If text is string, binary_mode should be False
        with (
            pytest.raises(TypeError),
            atomic_write(tmp_path / name, binary_mode=True) as f,
        ):
            f.write(content)


class TestDownloadFile:
    @pytest.fixture
    def sample_file_content(self):
        return b"Sample file content"

    @pytest.fixture
    def mock_successful_response(self, responses, sample_file_content):
        responses.add(
            responses.HEAD,
            "https://example.com/file.txt",
            headers={"content-length": str(len(sample_file_content))},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://example.com/file.txt",
            body=sample_file_content,
            headers={"content-length": str(len(sample_file_content))},
            status=200,
        )

    @pytest.fixture
    def mock_head_failure(self, responses, sample_file_content):
        responses.add(
            responses.HEAD,
            "https://example.com/file.txt",
            status=404,
        )

    @pytest.fixture
    def mock_download_failure(self, responses, sample_file_content):
        responses.add(
            responses.HEAD,
            "https://example.com/file.txt",
            headers={"content-length": str(len(sample_file_content))},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://example.com/file.txt",
            status=500,
        )

    def test_successful_download(self, tmp_path, mock_successful_response):
        """Test successful file download with valid URL and path."""
        output_path = tmp_path / "downloaded_file.txt"
        download_file("https://example.com/file.txt", output_path)

        assert output_path.exists()
        assert output_path.read_bytes() == b"Sample file content"

    @responses.activate
    def test_head_request_failure(self, tmp_path, mock_head_failure):
        """Test handling of HEAD request failure."""
        # responses.add(responses.HEAD, "https://example.com/file.txt", status=404)

        with pytest.raises(requests.exceptions.RequestException):
            download_file("https://example.com/file.txt", tmp_path / "file.txt")

    @responses.activate
    def test_download_request_failure(self, tmp_path, mock_download_failure):
        """Test handling of GET request failure."""
        # Mock successful HEAD request but failed GET request
        # responses.add(responses.HEAD, "https://example.com/file.txt", headers={"content-length": "100"}, status=200)
        # responses.add(responses.GET, "https://example.com/file.txt", status=500)

        with pytest.raises(requests.exceptions.RequestException):
            download_file("https://example.com/file.txt", tmp_path / "file.txt")

    def test_timeout_handling(self, tmp_path):
        """Test handling of timeout during download."""
        with patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout

            with pytest.raises(requests.exceptions.RequestException):
                download_file("https://example.com/file.txt", tmp_path / "file.txt", timeout=1)

    @responses.activate
    def test_missing_content_length(self, tmp_path):
        """Test download with missing content-length header."""
        responses.add(responses.HEAD, "https://example.com/file.txt", status=200)
        responses.add(responses.GET, "https://example.com/file.txt", body=b"content", status=200)

        output_path = tmp_path / "downloaded_file.txt"
        download_file("https://example.com/file.txt", output_path)

        assert output_path.exists()
        assert output_path.read_bytes() == b"content"

    def test_invalid_url(self, tmp_path):
        """Test handling of invalid URL."""
        with pytest.raises(requests.exceptions.RequestException):
            download_file("invalid-url", tmp_path / "file.txt")


class TestValidateFile:
    @pytest.fixture
    def create_test_file(self, tmp_path):
        """Fixture to create a test file with specific content."""

        def _create_file(content: bytes) -> Path:
            test_file = tmp_path / "test_file"
            test_file.write_bytes(content)
            return test_file

        return _create_file

    def test_valid_file_path_pathlib(self, create_test_file):
        """Test validation with Path object and valid file size."""
        test_file = create_test_file(b"test")
        assert validate_file(test_file) is True

    def test_valid_file_path_str(self, create_test_file):
        """Test validation with string path and valid file size."""
        test_file = create_test_file(b"test")
        assert validate_file(str(test_file)) is True

    def test_file_exact_min_size(self, create_test_file):
        """Test file with exactly minimum size requirement."""
        test_file = create_test_file(b"x")
        assert validate_file(test_file, min_bytes=1) is True

    def test_file_below_min_size(self, create_test_file):
        """Test file smaller than minimum size requirement."""
        test_file = create_test_file(b"x")
        assert validate_file(test_file, min_bytes=2) is False

    def test_empty_file(self, create_test_file):
        """Test empty file validation."""
        test_file = create_test_file(b"")
        assert validate_file(test_file) is False

    def test_large_file(self, create_test_file):
        """Test large file validation."""
        large_content = b"x" * 1024 * 1024  # 1MB
        test_file = create_test_file(large_content)
        assert validate_file(test_file, min_bytes=1024 * 1024) is True

    def test_nonexistent_file(self, tmp_path):
        """Test validation of non-existent file."""
        nonexistent_file = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError):
            validate_file(nonexistent_file)

    def test_directory_instead_of_file(self, tmp_path):
        """Test validation when path points to a directory."""
        with pytest.raises(FileNotFoundError):
            validate_file(tmp_path)

    @pytest.mark.skipif(os.name == "nt", reason="Permission tests unreliable on Windows")
    def test_no_read_permission(self, create_test_file):
        """Test file with no read permissions."""
        test_file = create_test_file(b"test")
        test_file.chmod(0o000)

        with pytest.raises(PermissionError):
            validate_file(test_file)

        # Cleanup: restore permissions for proper test cleanup
        test_file.chmod(0o644)

    def test_min_bytes_validation(self, create_test_file):
        """Test with negative min_bytes parameter."""
        test_file = create_test_file(b"test")
        with pytest.raises(ValueError):
            validate_file(test_file, min_bytes=-1)

        with pytest.raises(ValueError):
            validate_file(test_file, min_bytes=0)

    @pytest.mark.parametrize(
        "min_bytes,content,expected",
        [
            (1, b"x", True),
            (2, b"x", False),
            (5, b"hello", True),
            (10, b"hello", False),
        ],
    )
    def test_various_sizes(self, create_test_file, min_bytes, content, expected):
        """Test various combinations of file sizes and minimum requirements."""
        test_file = create_test_file(content)
        assert validate_file(test_file, min_bytes=min_bytes) is expected

    def test_symlink_file(self, create_test_file, tmp_path):
        """Test validation of symlink to file."""
        original_file = create_test_file(b"test")
        symlink_path = tmp_path / "symlink"
        symlink_path.symlink_to(original_file)
        assert validate_file(symlink_path) is True

    def test_special_characters_in_path(self, tmp_path):
        """Test validation with special characters in filename."""
        special_file = tmp_path / "test!@#$%^&*.txt"
        special_file.write_bytes(b"test")
        assert validate_file(special_file) is True
