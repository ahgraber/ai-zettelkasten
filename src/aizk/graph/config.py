"""Define graph-stage settings loaded from ``AIZK_GRAPH__*`` environment variables.

The settings cover contextualization, extraction, worker lease and retry
behavior, and the local GLiNER2 model revision; the admission section is
:class:`~aizk.pipeline.admission.AdmissionConfig`, re-exported here. A model
endpoint must be complete before contextualization starts; model weights load
from the configured local directory.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.contextualization.settings import ContextualizationConfig
from aizk.pipeline.admission import AdmissionConfig

__all__ = ["AdmissionConfig", "ContextualizationConfig", "ExtractionConfig", "NerConfig"]


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
