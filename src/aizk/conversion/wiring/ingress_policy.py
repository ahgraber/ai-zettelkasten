"""IngressPolicy — deployment configuration for publicly accepted submission kinds."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class IngressPolicy(BaseSettings):
    """Declares which submission kinds the API layer accepts from external callers.

    Reads from env var ``AIZK_INGRESS__ACCEPTED_SUBMISSION_KINDS``.
    In tests, use ``IngressPolicy(_env_file=None)`` to avoid loading a ``.env``
    file, or override the field directly.
    """

    model_config = SettingsConfigDict(env_prefix="AIZK_INGRESS__", env_file=".env", extra="ignore")

    accepted_submission_kinds: frozenset[str] = frozenset({"karakeep_bookmark"})


__all__ = ["IngressPolicy"]
