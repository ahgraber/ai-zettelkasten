"""Configuration management for the conversion service."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConversionConfig(BaseSettings):
    """Environment-driven configuration for the conversion service."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./data/conversion_service.db",
        validation_alias="DATABASE_URL",
    )
    s3_endpoint_url: str = Field(default="", validation_alias="S3_ENDPOINT_URL")
    s3_bucket_name: str = Field(default="", validation_alias="S3_BUCKET_NAME")
    s3_access_key_id: str = Field(default="", validation_alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(default="", validation_alias="S3_SECRET_ACCESS_KEY")
    s3_region: str = Field(default="us-east-1", validation_alias="S3_REGION")

    queue_max_depth: int = Field(default=1000, validation_alias="QUEUE_MAX_DEPTH")
    worker_concurrency: int = Field(default=4, validation_alias="WORKER_CONCURRENCY")
    fetch_timeout_seconds: int = Field(default=30, validation_alias="FETCH_TIMEOUT_SECONDS")
    fetch_max_size_html: int = Field(default=52_428_800, validation_alias="FETCH_MAX_SIZE_HTML")
    fetch_max_size_pdf: int = Field(default=104_857_600, validation_alias="FETCH_MAX_SIZE_PDF")
    retry_max_attempts: int = Field(default=3, validation_alias="RETRY_MAX_ATTEMPTS")
    retry_base_delay_seconds: int = Field(default=60, validation_alias="RETRY_BASE_DELAY_SECONDS")

    docling_pdf_max_pages: int = Field(default=100, validation_alias="DOCLING_PDF_MAX_PAGES")
    docling_enable_ocr: bool = Field(default=True, validation_alias="DOCLING_ENABLE_OCR")
    docling_enable_table_structure: bool = Field(
        default=True,
        validation_alias="DOCLING_ENABLE_TABLE_STRUCTURE",
    )
    docling_vlm_model: str = Field(
        default="openai/gpt-5-nano",
        validation_alias="DOCLING_VLM_MODEL",
    )
    docling_picture_timeout: float = Field(
        default=180.0,
        validation_alias="DOCLING_PICTURE_TIMEOUT",
    )

    chat_completions_base_url: str = Field(
        default="",
        validation_alias="CHAT_COMPLETIONS_BASE_URL",
    )
    chat_completions_api_key: str = Field(
        default="",
        validation_alias="CHAT_COMPLETIONS_API_KEY",
    )

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field(default="json", validation_alias="LOG_FORMAT")

    api_host: str = Field(default="0.0.0.0", validation_alias="API_HOST")  # NOQA: S104
    api_port: int = Field(default=8000, validation_alias="API_PORT")
    api_reload: bool = Field(default=True, validation_alias="API_RELOAD")

    temp_workspace_path: Path = Field(
        default=Path("./data/workspace"),
        validation_alias="TEMP_WORKSPACE_PATH",
    )
