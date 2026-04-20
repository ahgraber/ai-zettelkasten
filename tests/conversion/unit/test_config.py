"""Unit tests for conversion configuration loading."""

from pydantic import ValidationError
import pytest

from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig


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
    config = ConversionConfig(_env_file=None)
    assert config.database_url == database_url
    assert config.s3_bucket_name == s3_bucket_name
    assert config.worker_concurrency == int(worker_concurrency)
    assert config.mlflow_tracing_enabled is True
    assert config.mlflow_tracking_uri == mlflow_tracking_uri
    assert config.mlflow_experiment_name == mlflow_experiment_name


def test_api_reload_defaults_to_false(monkeypatch):
    monkeypatch.delenv("API_RELOAD", raising=False)
    config = ConversionConfig(_env_file=None)
    assert config.api_reload is False


def test_docling_config_reads_new_env_vars(monkeypatch):
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__OCR_ENABLED", "false")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES", "100")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")

    config = DoclingConverterConfig(_env_file=None)

    assert config.ocr_enabled is False
    assert config.pdf_max_pages == 100


def test_docling_old_env_var_has_no_effect(monkeypatch):
    """Old DOCLING_* env vars must have no effect — no compatibility shim."""
    monkeypatch.setenv("DOCLING_ENABLE_OCR", "false")  # opposite of default True
    monkeypatch.delenv("AIZK_CONVERTER__DOCLING__OCR_ENABLED", raising=False)
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")

    config = DoclingConverterConfig(_env_file=None)

    assert config.ocr_enabled is True  # default, old var ignored


def test_docling_config_placeholder_expansion(monkeypatch):
    monkeypatch.setenv("_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "${_OPENROUTER_BASE_URL}")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "$OPENROUTER_API_KEY")

    config = DoclingConverterConfig(_env_file=None)

    assert config.picture_description_base_url == "https://openrouter.ai/api/v1"
    assert config.picture_description_api_key == "test-key"
    assert config.is_picture_description_enabled() is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "${AIZK_TEST_MISSING_BASE_URL}"),
        ("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "$AIZK_TEST_MISSING_API_KEY"),
    ],
)
def test_docling_config_rejects_unresolved_placeholders(monkeypatch, field, value):
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")
    monkeypatch.delenv("AIZK_TEST_MISSING_BASE_URL", raising=False)
    monkeypatch.delenv("AIZK_TEST_MISSING_API_KEY", raising=False)
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError, match="contains unresolved env placeholder syntax"):
        DoclingConverterConfig(_env_file=None)


def test_karakeep_config_reads_new_env_vars(monkeypatch):
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__BASE_URL", "http://kk:3000")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__API_KEY", "mytoken")

    config = KarakeepFetcherConfig(_env_file=None)

    assert config.base_url == "http://kk:3000"
    assert config.api_key == "mytoken"
