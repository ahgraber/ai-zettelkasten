"""Configuration for the contextualization (graph) worker.

Follows the project's pydantic-settings convention: an ``AIZK_`` prefix with a
``__`` nested delimiter, so this section's variables are
``AIZK_GRAPH__CONTEXTUALIZATION__<FIELD>``. The model is reached as an
OpenAI-compatible chat-completions endpoint configured by the
**base-url / api-key / model** triple, mirroring the Docling picture-description
convention; contextualization is disabled (and the worker refuses to start) when
the endpoint is not fully configured.

The graph stage reuses the conversion service's
:class:`~aizk.conversion.utilities.config.ConversionConfig` for the shared
database URL and S3 settings (the Markdown it reads is a conversion output); this
config carries only the contextualization-specific model endpoint and the
worker's lease/retry knobs.

:class:`NerConfig` follows the same nested convention
(``AIZK_GRAPH__NER__<FIELD>``) for the pinned :class:`~aizk.graph.extraction.Gliner2Extractor`
model's local weight location and pinned revision.

:class:`ExtractionConfig` (``AIZK_GRAPH__EXTRACTION__<FIELD>``) selects the
extraction worker's injected NER extractor and raw-vs-contextualized input
policy, plus its lease/retry knobs, mirroring :class:`ContextualizationConfig`'s
worker settings.

:class:`AdmissionConfig` (``AIZK_GRAPH__<FIELD>``) governs what may enter the
graph stages' queues: the per-stage capacity limits enforced at every enqueue
path, the refusal delay intake reports, and the per-stage switches for automatic
admission. It spans both stages, so it sits at the graph level rather than inside
either stage's section.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextualizationConfig(BaseSettings):
    """Contextualization model endpoint and worker lease/retry settings.

    Read from ``AIZK_GRAPH__CONTEXTUALIZATION__*`` environment variables. The
    ``llm_*`` triple configures the OpenAI-compatible model; the ``worker_*`` and
    ``retry_*`` fields tune the runner's lease, stale recovery, and bounded
    retries; the ``operator_api_*`` fields are the listener for the graph operator
    API (jobs monitor + content explorer) served by ``aizk-graph serve``. It is a
    separate listener from the conversion API, so its default port differs.
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
        """Return ``True`` only when the model endpoint triple is fully configured.

        A value still carrying a ``${...}`` shell-style placeholder counts as
        **unconfigured**: an unresolved interpolation (e.g. a ``.env`` reference the
        runtime never expanded) is non-empty, so it would otherwise pass the startup
        gate and fail only once a job is claimed and the model is called. Rejecting
        it here surfaces the misconfiguration before any work is accepted.
        """
        values = (self.llm_base_url, self.llm_api_key, self.llm_model)
        if any(not value for value in values):
            return False
        return not any("${" in value for value in values)


class NerConfig(BaseSettings):
    """Local weight location and pinned revision for the :class:`~aizk.graph.extraction.Gliner2Extractor`.

    Read from ``AIZK_GRAPH__NER__*`` environment variables. GLiNER2's weights are
    a pinned dependency (design decision ``TwoPinnedExtractorsSpacyAndGliner2``):
    runtime loads ``gliner2_model_dir`` directly and never reaches the network.
    ``gliner2_revision`` pins the exact HuggingFace revision the one-time setup
    step (``aizk-graph fetch-gliner2-weights``) fetches into that directory, so
    the local weights only change when setup is re-run against a new revision.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__NER__",
        env_file=None,
        extra="ignore",
    )

    gliner2_model_dir: str = "data/models/gliner2-base-v1"
    gliner2_revision: str = "f5b2ecedebe4381b088c1cf276f5bf72a52cac54"


class ExtractionConfig(BaseSettings):
    """Extractor/input-policy selection and worker lease/retry settings for the extraction stage.

    Read from ``AIZK_GRAPH__EXTRACTION__*`` environment variables. ``extractor``
    selects which pinned NER extractor the worker's composition root
    (``aizk.graph.extraction_worker.build_extractor``) constructs; ``input_policy``
    is the run-level raw-vs-contextualized toggle passed to every extraction.
    Neither is a work-unit field. Changing either changes the run-level
    derivation key, but the work-unit surface does not re-enqueue a terminal
    unit on a config change (see :class:`~aizk.graph.datamodel.ExtractionJob`);
    re-extraction under the new configuration happens today through the direct
    entry points (:func:`~aizk.graph.extraction_run.extract_source` /
    :func:`~aizk.graph.extraction_run.extract_corpus`). The ``worker_*`` and
    ``retry_*`` fields tune the runner's lease, stale recovery, and bounded
    retries, mirroring :class:`ContextualizationConfig`.
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
    """What may enter the graph stages' queues, and whether it enters automatically.

    Read from ``AIZK_GRAPH__*`` environment variables. The two ``*_queue_max_depth``
    fields bound each stage's actionable backlog (see
    :mod:`aizk.graph.capacity`); ``0`` — the default — means the stage declares no
    limit and accepts work without a capacity refusal. The limits are per stage
    because contextualization is LLM-backed and extraction is not, so their spend
    profiles differ.

    ``queue_retry_after_seconds`` mirrors the conversion service's field of the
    same name: it is the ``Retry-After`` value graph intake returns with its 503,
    so one refusal convention covers the fleet.

    The two ``admission_*_enabled`` flags switch automatic admission on per stage,
    and are off by default: admitting contextualization work is external inference
    spend, so starting the flow is a deliberate act, and enabling one stage never
    enables the other. ``admission_interval_seconds`` is how often each enabled
    stage's worker evaluates its pending set.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__",
        env_file=None,
        extra="ignore",
    )

    contextualization_queue_max_depth: int = 0
    extraction_queue_max_depth: int = 0
    queue_retry_after_seconds: int = 30

    admission_contextualization_enabled: bool = False
    admission_extraction_enabled: bool = False
    admission_interval_seconds: float = 60.0
