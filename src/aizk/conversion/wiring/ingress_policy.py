"""IngressPolicy — deployment configuration for publicly accepted submission kinds."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class IngressPolicy(BaseSettings):
    """Declares which submission kinds the API layer accepts from external callers.

    Reads from env var ``AIZK_INGRESS__ACCEPTED_SUBMISSION_KINDS``.
    """

    model_config = SettingsConfigDict(env_prefix="AIZK_INGRESS__", env_file=None, extra="ignore")

    accepted_submission_kinds: frozenset[str] = frozenset({"karakeep_bookmark"})


__all__ = ["IngressPolicy"]
