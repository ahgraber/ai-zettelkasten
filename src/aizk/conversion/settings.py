"""Configuration for the conversion stage."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Final

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_UNRESOLVED_ENV_PATTERN = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")

# Default prefetch policy values. Operator overrides flow through
# ``ConversionConfig`` (env names ``AIZK_PREFETCH_*``) which reads these
# defaults via ``ConversionConfig.prefetch_policy()``. Single source of truth.
_DEFAULT_PER_IMAGE_MAX_BYTES: Final[int] = 10 * 1024 * 1024
_DEFAULT_MAX_IMAGES_PER_DOC: Final[int] = 50
_DEFAULT_MAX_TOTAL_BYTES_PER_DOC: Final[int] = 100 * 1024 * 1024
_DEFAULT_PHASE_DEADLINE_SECONDS: Final[float] = 60.0
_DEFAULT_MAX_IMAGES_PER_HOST: Final[int] = 10


@dataclass(frozen=True)
class PrefetchPolicy:
    """Per-document caps for the ``<img src>`` prefetch phase.

    Centralises the prefetch knobs so callers (the converter, the adapter,
    the egress utility) cannot drift on default values.  ``ConversionConfig``
    holds the env-var operator surface; ``ConversionConfig.prefetch_policy()``
    builds an instance of this dataclass from those fields.

    Attributes:
        per_image_max_bytes: Hard cap on response body bytes per image,
            streaming-enforced inside ``egress_fetch_bytes``.
        max_images: Maximum number of images to prefetch per document.
        max_total_bytes: Maximum cumulative bytes prefetched per document.
        phase_deadline_seconds: Wall-clock budget for the entire prefetch phase.
        max_images_per_host: Per-hostname cap on prefetched images per document.
            Defends against amplification attacks that fan out 50 ``<img>``
            tags pointing at a single victim host.  Counted on attempt, so a
            failed request still consumes the cap — it consumed outbound
            capacity against the host regardless of how it ended.
    """

    per_image_max_bytes: int = _DEFAULT_PER_IMAGE_MAX_BYTES
    max_images: int = _DEFAULT_MAX_IMAGES_PER_DOC
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES_PER_DOC
    phase_deadline_seconds: float = _DEFAULT_PHASE_DEADLINE_SECONDS
    max_images_per_host: int = _DEFAULT_MAX_IMAGES_PER_HOST


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
    picture_description_model: str = "openai/gpt-5.6-luna"
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
        env_file=None,
        extra="ignore",
    )

    base_url: str = ""
    api_key: str = ""


class ConversionConfig(BaseSettings):
    """Environment-driven configuration for the conversion stage."""

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

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

    def prefetch_policy(self) -> PrefetchPolicy:
        """Build a :class:`PrefetchPolicy` from the five ``PREFETCH_*`` config fields."""
        return PrefetchPolicy(
            per_image_max_bytes=self.prefetch_per_image_max_bytes,
            max_images=self.prefetch_max_images_per_doc,
            max_total_bytes=self.prefetch_max_total_bytes_per_doc,
            phase_deadline_seconds=self.prefetch_phase_deadline_seconds,
            max_images_per_host=self.prefetch_max_images_per_host,
        )
