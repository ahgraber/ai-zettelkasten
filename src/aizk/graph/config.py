"""Define graph-stage settings loaded from ``AIZK_GRAPH__*`` environment variables.

The settings cover contextualization, extraction, admission, worker lease and
retry behavior, the operator API, and the local GLiNER2 model revision. A model
endpoint must be complete before contextualization starts; model weights load
from the configured local directory.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextualizationConfig(BaseSettings):
    """Contextualization model endpoint and worker settings.

    Read from ``AIZK_GRAPH__CONTEXTUALIZATION__*``. The ``operator_api_*`` fields
    configure the separate operator API listener.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__CONTEXTUALIZATION__",
        env_file=None,
        extra="ignore",
    )

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    operator_api_host: str = "0.0.0.0"  # noqa: S104
    operator_api_port: int = 8001
    operator_api_reload: bool = False

    worker_concurrency: int = 1
    worker_timeout_seconds: float = 600.0
    worker_stale_minutes: float = 30.0
    worker_poll_interval_seconds: float = 2.0
    worker_drain_timeout_seconds: float = 30.0
    worker_stale_recovery_interval_seconds: float = 60.0

    retry_base_delay_seconds: float = 2.0
    retry_max_attempts: int = 3

    @property
    def llm_enabled(self) -> bool:
        """Return ``True`` only when the model endpoint values are configured.

        Unresolved ``${...}`` placeholders count as unconfigured.
        """
        values = (self.llm_base_url, self.llm_api_key, self.llm_model)
        if any(not value for value in values):
            return False
        return not any("${" in value for value in values)


class NerConfig(BaseSettings):
    """Local GLiNER2 weight location and pinned revision.

    The worker loads weights from ``gliner2_model_dir`` and does not fetch them at
    runtime.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__NER__",
        env_file=None,
        extra="ignore",
    )

    gliner2_model_dir: str = "data/models/gliner2-base-v1"
    gliner2_revision: str = "f5b2ecedebe4381b088c1cf276f5bf72a52cac54"


class ExtractionConfig(BaseSettings):
    """Extraction policy and worker settings.

    Read from ``AIZK_GRAPH__EXTRACTION__*``. ``extractor`` and ``input_policy``
    select the run derivation; changing either does not re-enqueue terminal units.
    The ``worker_*`` and ``retry_*`` fields configure execution.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__EXTRACTION__",
        env_file=None,
        extra="ignore",
    )

    extractor: Literal["spacy", "gliner2"] = "spacy"
    input_policy: Literal["contextualized", "raw"] = "contextualized"

    worker_concurrency: int = 1
    worker_timeout_seconds: float = 600.0
    worker_stale_minutes: float = 30.0
    worker_poll_interval_seconds: float = 2.0
    worker_drain_timeout_seconds: float = 30.0
    worker_stale_recovery_interval_seconds: float = 60.0

    retry_base_delay_seconds: float = 2.0
    retry_max_attempts: int = 3


class AdmissionConfig(BaseSettings):
    """Admission and queue-capacity settings for graph stages.

    Read from ``AIZK_GRAPH__*``. Each stage has its own queue limit and admission
    flag; a non-positive queue limit disables the limit. The
    ``queue_retry_after_seconds`` field sets the intake ``Retry-After`` value.
    Numeric settings reject invalid values at construction.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__",
        env_file=None,
        extra="ignore",
    )

    contextualization_queue_max_depth: int = Field(default=0, ge=0)
    extraction_queue_max_depth: int = Field(default=0, ge=0)
    queue_retry_after_seconds: int = Field(default=30, ge=1)

    admission_contextualization_enabled: bool = False
    admission_extraction_enabled: bool = False
    admission_interval_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
