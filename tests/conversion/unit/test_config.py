"""Unit tests for conversion configuration loading."""

from pydantic import ValidationError
import pytest

from aizk.conversion.utilities.config import ConversionConfig


def test_config_reads_env_vars(monkeypatch):
    database_url = "sqlite:///./test.db"
    s3_bucket_name = "aizk-test"
    worker_concurrency = "8"
    mlflow_tracing_enabled = "true"
    mlflow_tracking_uri = "http://mlflow:5000"
    mlflow_experiment_name = "aizk-conversion"

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("S3_BUCKET_NAME", s3_bucket_name)
    monkeypatch.setenv("WORKER_CONCURRENCY", str(worker_concurrency))
    monkeypatch.setenv("MLFLOW_TRACING_ENABLED", mlflow_tracing_enabled)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow_tracking_uri)
    monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", mlflow_experiment_name)
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "")
    config = ConversionConfig(_env_file=None)
    assert config.database_url == database_url
    assert config.s3_bucket_name == s3_bucket_name
    assert config.worker_concurrency == int(worker_concurrency)
    assert config.mlflow_tracing_enabled is True
    assert config.mlflow_tracking_uri == mlflow_tracking_uri
    assert config.mlflow_experiment_name == mlflow_experiment_name


def test_api_reload_defaults_to_false(monkeypatch):
    monkeypatch.delenv("API_RELOAD", raising=False)
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "")
    config = ConversionConfig(_env_file=None)
    assert config.api_reload is False


def test_picture_description_fields_expand_placeholders_from_environment(monkeypatch):
    monkeypatch.setenv("_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "${_OPENROUTER_BASE_URL}")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "$OPENROUTER_API_KEY")

    config = ConversionConfig(_env_file=None)

    assert config.docling_picture_description_base_url == "https://openrouter.ai/api/v1"
    assert config.docling_picture_description_api_key == "test-key"
    assert config.is_picture_description_enabled() is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "${AIZK_TEST_MISSING_BASE_URL}"),
        ("DOCLING_PICTURE_DESCRIPTION_API_KEY", "$AIZK_TEST_MISSING_API_KEY"),
    ],
)
def test_picture_description_fields_reject_unresolved_placeholders(monkeypatch, field, value):
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "")
    monkeypatch.delenv("AIZK_TEST_MISSING_BASE_URL", raising=False)
    monkeypatch.delenv("AIZK_TEST_MISSING_API_KEY", raising=False)
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError, match="contains unresolved env placeholder syntax"):
        ConversionConfig(_env_file=None)
