"""Startup validation for the conversion service.

Probes the external services the conversion adapters need and logs optional
feature status before the worker or API process begins accepting work.
"""

from __future__ import annotations

import logging

import httpx2

from aizk.conversion.settings import DoclingConverterConfig, KarakeepFetcherConfig
from aizk.db.backends.sqlite import SqliteDurabilityConfig
from aizk.service.settings import ServiceSettings
from aizk.utilities.errors import StartupValidationError

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 10.0


def probe_karakeep(karakeep_cfg: KarakeepFetcherConfig) -> None:
    """Verify KaraKeep API is reachable and credentials are valid.

    Raises:
        StartupValidationError: If the KaraKeep API is unreachable, returns
            an error, or required config values are missing.
    """
    base_url = karakeep_cfg.base_url
    api_key = karakeep_cfg.api_key

    if not base_url or not api_key:
        missing = []
        if not base_url:
            missing.append("AIZK_FETCHER__KARAKEEP__BASE_URL")
        if not api_key:
            missing.append("AIZK_FETCHER__KARAKEEP__API_KEY")
        raise StartupValidationError(f"Missing required environment variables: {', '.join(missing)}")

    url = f"{base_url.rstrip('/')}/api/v1/bookmarks"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    try:
        response = httpx2.get(
            url,
            headers=headers,
            params={"limit": 1},
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx2.HTTPStatusError as exc:
        raise StartupValidationError(f"KaraKeep API returned HTTP {exc.response.status_code}: {exc}") from exc
    except httpx2.RequestError as exc:
        raise StartupValidationError(f"KaraKeep API unreachable at {url}: {exc}") from exc


def probe_picture_description(docling_cfg: DoclingConverterConfig) -> None:
    """Verify the picture description endpoint is reachable via GET /models.

    No-op when the endpoint is not configured (picture description disabled).

    Raises:
        StartupValidationError: If the endpoint returns non-2xx or is unreachable.
    """
    base_url = docling_cfg.picture_description_base_url.strip().rstrip("/")
    api_key = docling_cfg.picture_description_api_key.strip()

    if not base_url or not api_key:
        return

    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        response = httpx2.get(url, headers=headers, timeout=_PROBE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx2.HTTPStatusError as exc:
        raise StartupValidationError(
            f"Picture description endpoint returned HTTP {exc.response.status_code}: {url}"
        ) from exc
    except httpx2.RequestError as exc:
        raise StartupValidationError(f"Picture description endpoint unreachable at {url}: {exc}") from exc


def log_feature_summary(settings: ServiceSettings, docling_cfg: DoclingConverterConfig, role: str) -> None:
    """Log a structured summary of optional feature states.

    Args:
        settings: Service-wide settings; the tracing state is read from its
            MLflow section.
        docling_cfg: Docling-specific configuration.
        role: Process role (e.g. "worker", "api").
    """
    features: dict[str, dict[str, str]] = {}

    # Picture descriptions
    if docling_cfg.is_picture_description_enabled():
        features["picture_descriptions"] = {"status": "enabled"}
    else:
        features["picture_descriptions"] = {
            "status": "disabled",
            "reason": "AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL not configured",
        }

    # Picture classification (requires both the config flag and a VLM endpoint)
    if not docling_cfg.is_picture_description_enabled():
        features["picture_classification"] = {
            "status": "disabled",
            "reason": "picture description not enabled",
        }
    elif not docling_cfg.picture_classification_enabled:
        features["picture_classification"] = {
            "status": "disabled",
            "reason": "AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=false",
        }
    else:
        features["picture_classification"] = {"status": "enabled"}

    # MLflow tracing
    if settings.mlflow.enabled:
        features["mlflow_tracing"] = {"status": "enabled"}
    else:
        features["mlflow_tracing"] = {
            "status": "disabled",
            "reason": "AIZK_MLFLOW__ENABLED is false",
        }

    # Litestream replication (SQLite backend durability; reads its own settings)
    durability = SqliteDurabilityConfig()
    if durability.litestream_enabled and durability.litestream_s3_bucket_name:
        features["litestream_replication"] = {"status": "enabled"}
    else:
        if not durability.litestream_enabled:
            reason = "AIZK_LITESTREAM_ENABLED is false"
        else:
            reason = "AIZK_LITESTREAM_S3_BUCKET_NAME is empty"
        features["litestream_replication"] = {
            "status": "disabled",
            "reason": reason,
        }

    logger.info(
        "startup feature summary",
        extra={"role": role, "features": features},
    )
