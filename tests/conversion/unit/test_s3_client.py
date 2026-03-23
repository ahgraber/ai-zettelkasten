"""Unit tests for S3Client.get_object_bytes."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
import pytest

from aizk.conversion.storage.s3_client import S3Client, S3Error, S3NotFoundError
from aizk.conversion.utilities.config import ConversionConfig


def _make_client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test error"}}, "GetObject")


@pytest.fixture()
def s3_client(monkeypatch: pytest.MonkeyPatch) -> S3Client:
    """Return an S3Client with a mocked boto3 client."""
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    config = ConversionConfig()
    client = S3Client.__new__(S3Client)
    client.config = config
    client.bucket = config.s3_bucket_name
    client.client = MagicMock()
    return client


def test_get_object_bytes_returns_content(s3_client: S3Client) -> None:
    content = b"# Hello\n\nWorld"
    s3_client.client.get_object.return_value = {"Body": BytesIO(content)}

    result = s3_client.get_object_bytes("some/key.md")

    assert result == content
    s3_client.client.get_object.assert_called_once_with(Bucket="test-bucket", Key="some/key.md")


def test_get_object_bytes_raises_not_found_on_no_such_key(s3_client: S3Client) -> None:
    s3_client.client.get_object.side_effect = _make_client_error("NoSuchKey")

    with pytest.raises(S3NotFoundError) as exc_info:
        s3_client.get_object_bytes("missing/key.md")

    assert exc_info.value.error_code == "s3_not_found"
    assert not S3NotFoundError.retryable


def test_get_object_bytes_raises_not_found_on_404(s3_client: S3Client) -> None:
    s3_client.client.get_object.side_effect = _make_client_error("404")

    with pytest.raises(S3NotFoundError):
        s3_client.get_object_bytes("missing/key.md")


def test_get_object_bytes_raises_s3_error_on_other_client_error(s3_client: S3Client) -> None:
    s3_client.client.get_object.side_effect = _make_client_error("AccessDenied")

    with pytest.raises(S3Error) as exc_info:
        s3_client.get_object_bytes("some/key.md")

    assert not isinstance(exc_info.value, S3NotFoundError)
    assert S3Error.retryable


def test_get_object_bytes_raises_s3_error_on_unexpected_exception(s3_client: S3Client) -> None:
    s3_client.client.get_object.side_effect = RuntimeError("connection reset")

    with pytest.raises(S3Error):
        s3_client.get_object_bytes("some/key.md")
