"""Unit tests for startup validation."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
import httpx
import pytest

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.startup import (
    StartupValidationError,
    log_feature_summary,
    probe_karakeep,
    probe_picture_description,
    probe_s3,
    validate_startup,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config(monkeypatch: pytest.MonkeyPatch) -> ConversionConfig:
    """Return a ConversionConfig with minimal valid settings."""
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__BASE_URL", "http://karakeep.local")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__API_KEY", "test-key")
    return ConversionConfig(_env_file=None)


def _config_with_karakeep(*, base_url: str, api_key: str) -> ConversionConfig:
    return ConversionConfig(
        _env_file=None,
        fetcher={"karakeep": {"base_url": base_url, "api_key": api_key}},
    )


# ---------------------------------------------------------------------------
# probe_s3
# ---------------------------------------------------------------------------


def test_probe_s3_succeeds_when_bucket_reachable(config: ConversionConfig) -> None:
    mock_boto = MagicMock()
    with patch(
        "aizk.conversion.utilities.startup.S3Client",
        return_value=MagicMock(client=mock_boto, config=config),
    ):
        probe_s3(config)

    mock_boto.head_bucket.assert_called_once_with(Bucket="test-bucket")


def test_probe_s3_raises_on_client_error(config: ConversionConfig) -> None:
    error = ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}},
        "HeadBucket",
    )
    mock_s3 = MagicMock()
    mock_s3.client.head_bucket.side_effect = error
    with (
        patch("aizk.conversion.utilities.startup.S3Client", return_value=mock_s3),
        pytest.raises(StartupValidationError, match="test-bucket.*unreachable"),
    ):
        probe_s3(config)


def test_probe_s3_raises_on_connection_error(config: ConversionConfig) -> None:
    mock_s3 = MagicMock()
    mock_s3.client.head_bucket.side_effect = ConnectionError("refused")
    with (
        patch("aizk.conversion.utilities.startup.S3Client", return_value=mock_s3),
        pytest.raises(StartupValidationError, match="unreachable"),
    ):
        probe_s3(config)


# ---------------------------------------------------------------------------
# probe_karakeep
# ---------------------------------------------------------------------------


def test_probe_karakeep_succeeds_when_reachable(config: ConversionConfig) -> None:
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with patch("aizk.conversion.utilities.startup.httpx.get", return_value=mock_response) as mock_get:
        probe_karakeep(config)

    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args
    assert "bookmarks" in call_kwargs.args[0]
    assert call_kwargs.kwargs["params"] == {"limit": 1}
    assert "Bearer test-key" in call_kwargs.kwargs["headers"]["Authorization"]


def test_probe_karakeep_raises_on_missing_env_vars() -> None:
    config = _config_with_karakeep(base_url="", api_key="")

    with pytest.raises(StartupValidationError, match="Missing required environment variables"):
        probe_karakeep(config)


def test_probe_karakeep_raises_on_missing_base_url() -> None:
    config = _config_with_karakeep(base_url="", api_key="test-key")

    with pytest.raises(StartupValidationError, match="AIZK_FETCHER__KARAKEEP__BASE_URL"):
        probe_karakeep(config)


def test_probe_karakeep_raises_on_http_error(config: ConversionConfig) -> None:
    mock_response = httpx.Response(status_code=401, request=httpx.Request("GET", "http://test"))
    with (
        patch(
            "aizk.conversion.utilities.startup.httpx.get",
            side_effect=httpx.HTTPStatusError("401", request=mock_response.request, response=mock_response),
        ),
        pytest.raises(StartupValidationError, match="HTTP 401"),
    ):
        probe_karakeep(config)


def test_probe_karakeep_raises_on_connection_error(config: ConversionConfig) -> None:
    with (
        patch("aizk.conversion.utilities.startup.httpx.get", side_effect=httpx.ConnectError("refused")),
        pytest.raises(StartupValidationError, match="unreachable"),
    ):
        probe_karakeep(config)


# ---------------------------------------------------------------------------
# log_feature_summary
# ---------------------------------------------------------------------------


def test_log_feature_summary_all_enabled(config: ConversionConfig, caplog: pytest.LogCaptureFixture) -> None:
    config.mlflow_tracing_enabled = True
    config.converter.docling.picture_description_base_url = "http://llm.local/v1"
    config.converter.docling.picture_description_api_key = "key"
    config.litestream_enabled = True
    config.litestream_s3_bucket_name = "backup-bucket"

    with caplog.at_level(logging.INFO):
        log_feature_summary(config, "worker")

    assert "startup feature summary" in caplog.text


def test_log_feature_summary_all_disabled(config: ConversionConfig, caplog: pytest.LogCaptureFixture) -> None:
    config.mlflow_tracing_enabled = False
    config.converter.docling.picture_description_base_url = ""
    config.converter.docling.picture_description_api_key = ""
    config.litestream_enabled = False

    with caplog.at_level(logging.INFO):
        log_feature_summary(config, "api")

    assert "startup feature summary" in caplog.text


@pytest.mark.parametrize(
    ("base_url", "api_key", "mlflow", "litestream_enabled", "litestream_bucket", "expected_disabled"),
    [
        (
            "",
            "",
            False,
            False,
            "",
            {"picture_descriptions", "picture_classification", "mlflow_tracing", "litestream_replication"},
        ),
        ("http://llm", "key", True, True, "bucket", set()),
        (
            "http://llm",
            "",
            False,
            True,
            "",
            {"picture_descriptions", "picture_classification", "mlflow_tracing", "litestream_replication"},
        ),
        ("", "key", True, True, "bucket", {"picture_descriptions", "picture_classification"}),
    ],
    ids=["all-disabled", "all-enabled", "mixed-disabled", "only-picture-disabled"],
)
def test_log_feature_summary_combinations(
    config: ConversionConfig,
    base_url: str,
    api_key: str,
    mlflow: bool,
    litestream_enabled: bool,
    litestream_bucket: str,
    expected_disabled: set[str],
) -> None:
    config.converter.docling.picture_description_base_url = base_url
    config.converter.docling.picture_description_api_key = api_key
    config.mlflow_tracing_enabled = mlflow
    config.litestream_enabled = litestream_enabled
    config.litestream_s3_bucket_name = litestream_bucket

    with patch("aizk.conversion.utilities.startup.logger") as mock_logger:
        log_feature_summary(config, "worker")

    call_kwargs = mock_logger.info.call_args
    features = call_kwargs.kwargs["extra"]["features"]
    disabled_features = {name for name, state in features.items() if state["status"] == "disabled"}
    assert disabled_features == expected_disabled


# ---------------------------------------------------------------------------
# validate_startup
# ---------------------------------------------------------------------------


def test_validate_startup_succeeds_when_all_probes_pass(config: ConversionConfig) -> None:
    with (
        patch("aizk.conversion.utilities.startup.probe_s3") as mock_s3,
        patch("aizk.conversion.utilities.startup.probe_karakeep") as mock_kk,
        patch("aizk.conversion.utilities.startup.log_feature_summary") as mock_log,
    ):
        validate_startup(config, "worker")

    mock_s3.assert_called_once_with(config)
    mock_kk.assert_called_once_with(config)
    mock_log.assert_called_once_with(config, "worker")


def test_validate_startup_raises_on_s3_failure(config: ConversionConfig) -> None:
    with (
        patch("aizk.conversion.utilities.startup.probe_s3", side_effect=StartupValidationError("s3 down")),
        patch("aizk.conversion.utilities.startup.probe_karakeep") as mock_kk,
        pytest.raises(StartupValidationError, match="s3 down"),
    ):
        validate_startup(config, "worker")

    mock_kk.assert_not_called()


def test_validate_startup_raises_on_karakeep_failure(config: ConversionConfig) -> None:
    with (
        patch("aizk.conversion.utilities.startup.probe_s3"),
        patch("aizk.conversion.utilities.startup.probe_karakeep", side_effect=StartupValidationError("kk down")),
        pytest.raises(StartupValidationError, match="kk down"),
    ):
        validate_startup(config, "api")


# ---------------------------------------------------------------------------
# probe_picture_description
# ---------------------------------------------------------------------------


def test_probe_picture_description_noop_when_not_configured(config: ConversionConfig) -> None:
    config.converter.docling.picture_description_base_url = ""
    config.converter.docling.picture_description_api_key = ""
    # Should complete without making any HTTP calls
    probe_picture_description(config)


def test_probe_picture_description_noop_when_only_url_set(config: ConversionConfig) -> None:
    config.converter.docling.picture_description_base_url = "http://vllm.local/v1"
    config.converter.docling.picture_description_api_key = ""
    probe_picture_description(config)


def test_probe_picture_description_succeeds_on_200(config: ConversionConfig) -> None:
    config.converter.docling.picture_description_base_url = "http://vllm.local/v1"
    config.converter.docling.picture_description_api_key = "test-key"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    with patch("aizk.conversion.utilities.startup.httpx.get", return_value=mock_response):
        probe_picture_description(config)


def test_probe_picture_description_raises_on_non_2xx(config: ConversionConfig) -> None:
    config.converter.docling.picture_description_base_url = "http://vllm.local/v1"
    config.converter.docling.picture_description_api_key = "test-key"
    with (
        patch(
            "aizk.conversion.utilities.startup.httpx.get",
            side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock(status_code=401)),
        ),
        pytest.raises(StartupValidationError, match="401"),
    ):
        probe_picture_description(config)


def test_probe_picture_description_raises_on_connection_error(config: ConversionConfig) -> None:
    config.converter.docling.picture_description_base_url = "http://vllm.local/v1"
    config.converter.docling.picture_description_api_key = "test-key"
    with (
        patch(
            "aizk.conversion.utilities.startup.httpx.get",
            side_effect=httpx.ConnectError("refused"),
        ),
        pytest.raises(StartupValidationError, match="unreachable"),
    ):
        probe_picture_description(config)
