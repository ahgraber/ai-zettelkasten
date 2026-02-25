"""Unit tests for conversion configuration loading."""

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
    config = ConversionConfig(_env_file=None)
    assert config.database_url == database_url
    assert config.s3_bucket_name == s3_bucket_name
    assert config.worker_concurrency == int(worker_concurrency)
    assert config.mlflow_tracing_enabled is True
    assert config.mlflow_tracking_uri == mlflow_tracking_uri
    assert config.mlflow_experiment_name == mlflow_experiment_name
