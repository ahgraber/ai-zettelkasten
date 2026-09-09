"""Define the service-wide settings every aizk process reads from the environment.

The flat fields on :class:`ServiceSettings` cover logging and the primary HTTP
listener under the ``AIZK_`` prefix. Concerns with more than one field of their
own are their own settings class with their own prefix, composed onto
:class:`ServiceSettings` as a section.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MlflowSettings(BaseSettings):
    """MLflow tracing configuration read from ``AIZK_MLFLOW__*``."""

    model_config = SettingsConfigDict(
        env_prefix="AIZK_MLFLOW__",
        env_file=None,
        extra="ignore",
    )

    enabled: bool = False
    tracking_uri: str = ""
    experiment_name: str = ""


class OperatorApiSettings(BaseSettings):
    """Operator API listener configuration read from ``AIZK_SERVICE__OPERATOR_API_*``.

    The operator app is served on its own listener, hence a default port
    distinct from the conversion API's.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_SERVICE__OPERATOR_API_",
        env_file=None,
        extra="ignore",
    )

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8001
    reload: bool = False


class ServiceSettings(BaseSettings):
    """Environment-driven settings shared by every aizk service process."""

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

    log_level: str = "INFO"
    log_format: str = "json"

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

    mlflow: MlflowSettings = Field(default_factory=MlflowSettings)
    operator_api: OperatorApiSettings = Field(default_factory=OperatorApiSettings)
