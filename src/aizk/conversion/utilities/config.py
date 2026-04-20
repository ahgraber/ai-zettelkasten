"""Configuration management for the conversion service."""

from __future__ import annotations

import os
import re

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_UNRESOLVED_ENV_PATTERN = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


class DoclingConverterConfig(BaseSettings):
    """Per-adapter config for the Docling converter."""

    model_config = SettingsConfigDict(
        env_prefix="AIZK_CONVERTER__DOCLING__",
        env_file=".env",
        extra="ignore",
    )

    pdf_max_pages: int = 250
    ocr_enabled: bool = True
    table_structure_enabled: bool = True
    picture_description_model: str = "openai/gpt-5.4-nano"
    picture_timeout: float = 180.0
    picture_classification_enabled: bool = True
    picture_description_base_url: str = ""
    picture_description_api_key: str = ""

    @model_validator(mode="after")
    def validate_picture_description_fields(self) -> "DoclingConverterConfig":
        """Expand env placeholders once, then fail fast if any remain unresolved."""
        for field_name in ("picture_description_base_url", "picture_description_api_key"):
            value = getattr(self, field_name).strip()
            if value:
                value = os.path.expandvars(value).strip()
                setattr(self, field_name, value)
            if value and _UNRESOLVED_ENV_PATTERN.search(value):
                raise ValueError(
                    f"{field_name} contains unresolved env placeholder syntax: {value!r}. "
                    "Set a concrete value before constructing DoclingConverterConfig."
                )
        return self

    def is_picture_description_enabled(self) -> bool:
        """Return whether upstream picture-description chat calls are enabled."""
        return bool(self.picture_description_base_url.rstrip("/") and self.picture_description_api_key)


class KarakeepFetcherConfig(BaseSettings):
    """Per-adapter config for KaraKeep bookmark fetching."""

    model_config = SettingsConfigDict(
        env_prefix="AIZK_FETCHER__KARAKEEP__",
        env_file=".env",
        extra="ignore",
    )

    base_url: str = ""
    api_key: str = ""


class ConversionConfig(BaseSettings):
    """Environment-driven configuration for the conversion service."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./data/conversion_service.db",
        validation_alias="DATABASE_URL",
    )
    s3_endpoint_url: str = Field(default="", validation_alias="S3_ENDPOINT_URL")
    s3_bucket_name: str = Field(default="aizk", validation_alias="S3_BUCKET_NAME")
    s3_access_key_id: str = Field(default="", validation_alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(default="", validation_alias="S3_SECRET_ACCESS_KEY")
    s3_region: str = Field(default="us-east-1", validation_alias="S3_REGION")

    queue_max_depth: int = Field(default=1000, validation_alias="QUEUE_MAX_DEPTH")
    queue_retry_after_seconds: int = Field(default=30, validation_alias="QUEUE_RETRY_AFTER_SECONDS")
    worker_concurrency: int = Field(default=4, validation_alias="WORKER_CONCURRENCY")
    worker_gpu_concurrency: int = Field(default=1, validation_alias="WORKER_GPU_CONCURRENCY")
    fetch_timeout_seconds: int = Field(default=30, validation_alias="FETCH_TIMEOUT_SECONDS")
    retry_max_attempts: int = Field(default=3, validation_alias="RETRY_MAX_ATTEMPTS")
    retry_base_delay_seconds: int = Field(default=60, validation_alias="RETRY_BASE_DELAY_SECONDS")
    worker_stale_job_minutes: int = Field(default=30, validation_alias="WORKER_STALE_JOB_MINUTES")
    worker_stale_job_check_seconds: float = Field(
        default=60.0,
        validation_alias="WORKER_STALE_JOB_CHECK_SECONDS",
    )
    worker_job_timeout_seconds: float = Field(
        default=7200,
        validation_alias="WORKER_JOB_TIMEOUT_SECONDS",
    )
    worker_drain_timeout_seconds: int = Field(
        default=300,
        validation_alias="WORKER_DRAIN_TIMEOUT_SECONDS",
    )
    worker_converter_name: str = Field(default="docling", validation_alias="WORKER_CONVERTER_NAME")

    mlflow_tracing_enabled: bool = Field(default=False, validation_alias="MLFLOW_TRACING_ENABLED")
    mlflow_tracking_uri: str = Field(default="", validation_alias="MLFLOW_TRACKING_URI")
    mlflow_experiment_name: str = Field(default="", validation_alias="MLFLOW_EXPERIMENT_NAME")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field(default="json", validation_alias="LOG_FORMAT")

    litestream_enabled: bool = Field(default=True, validation_alias="LITESTREAM_ENABLED")
    litestream_start_role: str = Field(default="api", validation_alias="LITESTREAM_START_ROLE")
    litestream_binary: str = Field(default="litestream", validation_alias="LITESTREAM_BINARY")
    litestream_config_path: str = Field(
        default="./data/litestream.yaml",
        validation_alias="LITESTREAM_CONFIG_PATH",
    )
    litestream_s3_bucket_name: str = Field(
        default="",
        validation_alias="LITESTREAM_S3_BUCKET_NAME",
    )
    litestream_s3_prefix: str = Field(default="db", validation_alias="LITESTREAM_S3_PREFIX")
    litestream_s3_force_path_style: bool = Field(
        default=True,
        validation_alias="LITESTREAM_S3_FORCE_PATH_STYLE",
    )
    litestream_s3_sign_payload: bool = Field(
        default=True,
        validation_alias="LITESTREAM_S3_SIGN_PAYLOAD",
    )
    litestream_restore_on_startup: bool = Field(
        default=True,
        validation_alias="LITESTREAM_RESTORE_ON_STARTUP",
    )
    litestream_allow_empty_restore: bool = Field(
        default=True,
        validation_alias="LITESTREAM_ALLOW_EMPTY_RESTORE",
    )

    api_host: str = Field(default="0.0.0.0", validation_alias="API_HOST")  # NOQA: S104
    api_port: int = Field(default=8000, validation_alias="API_PORT")
    api_reload: bool = Field(default=False, validation_alias="API_RELOAD")
