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
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextualizationConfig(BaseSettings):
    """Contextualization model endpoint and worker lease/retry settings.

    Read from ``AIZK_GRAPH__CONTEXTUALIZATION__*`` environment variables. The
    ``llm_*`` triple configures the OpenAI-compatible model; the ``worker_*`` and
    ``retry_*`` fields tune the runner's lease, stale recovery, and bounded
    retries.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__CONTEXTUALIZATION__",
        env_file=None,
        extra="ignore",
    )

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

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
