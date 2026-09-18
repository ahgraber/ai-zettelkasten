"""Unit tests for S3Client construction and ``get_object_bytes``."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
import pytest

from aizk.storage.s3 import S3Client, S3Error, S3NotFoundError
from aizk.storage.settings import ObjectStorageSettings


def _make_client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test error"}}, "GetObject")


@pytest.fixture()
def _object_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the object-storage environment every settings class over it reads."""
    monkeypatch.setenv("AIZK_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AIZK_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AIZK_S3_REGION", "us-east-1")
    monkeypatch.setenv("AIZK_S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("AIZK_S3_ENDPOINT_URL", "http://localhost:9000")


@pytest.fixture()
def s3_client(_object_storage_env: None) -> S3Client:
    """Return an S3Client with a mocked boto3 client."""
    config = ObjectStorageSettings(_env_file=None)
    client = S3Client.__new__(S3Client)
    client.config = config
    client.bucket = config.s3_bucket_name
    client.client = MagicMock()
    return client


def test_client_reads_its_endpoint_and_bucket_from_object_storage_settings(_object_storage_env: None) -> None:
    """The settings the client is built from decide its boto3 connection and its bucket."""
    settings = ObjectStorageSettings(_env_file=None)

    with patch("aizk.storage.s3.boto3.client") as mock_boto3:
        client = S3Client(settings)

    assert client.config is settings
    assert client.bucket == "test-bucket"
    assert mock_boto3.call_args.kwargs["endpoint_url"] == "http://localhost:9000"
    assert mock_boto3.call_args.kwargs["region_name"] == "us-east-1"


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
