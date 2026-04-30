"""Configuration management for the conversion service."""

from __future__ import annotations

import os
import re
from typing import ClassVar, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.conversion.core.errors import ConfigurationError

_UNRESOLVED_ENV_PATTERN = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


class DoclingConverterConfig(BaseSettings):
    """Per-adapter config for the Docling converter."""

    model_config = SettingsConfigDict(
        env_prefix="AIZK_CONVERTER__DOCLING__",
        env_file=None,
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


class AuthSettings(BaseSettings):
    """Deployment trust-mode configuration for the conversion API.

    The `auth_mode` field reserves names for future modes (`token`,
    `proxy_headers`, `oidc`) at the type level so that exhaustive `match`
    statements in the principal resolver can be type-checked against the full
    eventual surface, while a runtime validator rejects any value that lacks
    an implemented resolver branch.
    """

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

    auth_mode: Literal["trust_network", "token", "proxy_headers", "oidc"] = "trust_network"
    default_principal: str = "self"

    _IMPLEMENTED_MODES: ClassVar[frozenset[str]] = frozenset({"trust_network"})

    @field_validator("auth_mode")
    @classmethod
    def _reject_unimplemented_modes(cls, value: str) -> str:
        """Reject auth modes whose resolver branch is not implemented in this build."""
        if value not in cls._IMPLEMENTED_MODES:
            raise ConfigurationError(
                f"auth mode '{value}' is reserved for a future build but not implemented at this cutover"
            )
        return value


class KarakeepFetcherConfig(BaseSettings):
    """Per-adapter config for KaraKeep bookmark fetching."""

    model_config = SettingsConfigDict(
        env_prefix="AIZK_FETCHER__KARAKEEP__",
        env_file=None,
        extra="ignore",
    )

    base_url: str = ""
    api_key: str = ""


class ConversionConfig(BaseSettings):
    """Environment-driven configuration for the conversion service."""

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

    database_url: str = "sqlite:///./data/conversion_service.db"
    s3_endpoint_url: str = ""
    s3_bucket_name: str = "aizk"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "us-east-1"

    queue_max_depth: int = 1000
    queue_retry_after_seconds: int = 30
    worker_concurrency: int = 4
    worker_gpu_concurrency: int = 1
    fetch_timeout_seconds: int = 30
    retry_max_attempts: int = 3
    retry_base_delay_seconds: int = 60
    worker_stale_job_minutes: int = 30
    worker_stale_job_check_seconds: float = 60.0
    fetch_max_response_bytes: int = 25 * 1024 * 1024
    worker_job_timeout_seconds: float = 7200
    worker_drain_timeout_seconds: int = 300
    worker_converter_name: str = "docling"

    mlflow_tracing_enabled: bool = False
    mlflow_tracking_uri: str = ""
    mlflow_experiment_name: str = ""

    log_level: str = "INFO"
    log_format: str = "json"

    litestream_enabled: bool = True
    litestream_start_role: str = "api"
    litestream_binary: str = "litestream"
    litestream_config_path: str = "./data/litestream.yaml"
    litestream_s3_bucket_name: str = ""
    litestream_s3_prefix: str = "db"
    litestream_s3_force_path_style: bool = True
    litestream_s3_sign_payload: bool = True
    litestream_restore_on_startup: bool = True
    litestream_allow_empty_restore: bool = True

    api_host: str = "0.0.0.0"  # noqa: S104
    api_port: int = 8000
    api_reload: bool = False

    trusted_hosts: list[str] = Field(
        default=["localhost", "127.0.0.1"],
        description=(
            "Allowlist enforced by Starlette TrustedHostMiddleware against the "
            "inbound Host header. Operators deploying behind a reverse proxy MUST "
            "override this and ensure the proxy rewrites Host AND strips client-supplied "
            "X-Forwarded-Host."
        ),
    )

    prefetch_per_image_max_bytes: int = 10 * 1024 * 1024
    prefetch_max_images_per_doc: int = 50
    prefetch_max_total_bytes_per_doc: int = 100 * 1024 * 1024
    prefetch_phase_deadline_seconds: float = 60.0
    prefetch_max_images_per_host: int = Field(
        default=10,
        description="Per-hostname cap on prefetched images per HTML document (outbound-amplification defence).",
    )

    egress_dns_workers: int = Field(
        default=4,
        description="Thread-pool size for synchronous DNS resolution in the egress validator.",
    )
    egress_validation_workers: int = Field(
        default=4,
        description="Thread-pool size for the async-egress validation executor.",
    )

    def prefetch_policy(self) -> "PrefetchPolicy":
        """Build a :class:`PrefetchPolicy` from the four ``PREFETCH_*`` config fields."""
        # Local import: keeps the dependency edge config -> html_prefetch one-directional
        # at module load time and avoids a cycle if html_prefetch ever needs config.
        from aizk.conversion.utilities.html_prefetch import PrefetchPolicy

        return PrefetchPolicy(
            per_image_max_bytes=self.prefetch_per_image_max_bytes,
            max_images=self.prefetch_max_images_per_doc,
            max_total_bytes=self.prefetch_max_total_bytes_per_doc,
            phase_deadline_seconds=self.prefetch_phase_deadline_seconds,
            max_images_per_host=self.prefetch_max_images_per_host,
        )
