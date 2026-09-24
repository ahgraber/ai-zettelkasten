"""Define contextualization settings loaded from ``AIZK_GRAPH__CONTEXTUALIZATION__*`` environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextualizationConfig(BaseSettings):
    """Contextualization model endpoint and worker settings.

    Read from ``AIZK_GRAPH__CONTEXTUALIZATION__*``.
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
        """Return ``True`` only when the model endpoint values are configured.

        Unresolved ``${...}`` placeholders count as unconfigured.
        """
        values = (self.llm_base_url, self.llm_api_key, self.llm_model)
        if any(not value for value in values):
            return False
        return not any("${" in value for value in values)
