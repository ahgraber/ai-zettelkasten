"""Startup validation for the conversion service.

Probes required external services and logs optional feature status before
the worker or API process begins accepting work.
"""

from __future__ import annotations

import logging

import httpx

from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 10.0


class StartupValidationError(RuntimeError):
    """Raised when a required service is unreachable at startup."""


def probe_s3(config: ConversionConfig) -> None:
    """Verify S3 bucket is reachable with a HEAD bucket call.

    Raises:
        StartupValidationError: If the S3 bucket is unreachable or credentials
            are invalid.
    """
    try:
        client = S3Client(config)
        client.client.head_bucket(Bucket=config.s3_bucket_name)
    except Exception as exc:
        raise StartupValidationError(f"S3 bucket '{config.s3_bucket_name}' is unreachable: {exc}") from exc


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
        response = httpx.get(
            url,
            headers=headers,
            params={"limit": 1},
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise StartupValidationError(f"KaraKeep API returned HTTP {exc.response.status_code}: {exc}") from exc
    except httpx.RequestError as exc:
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
        response = httpx.get(url, headers=headers, timeout=_PROBE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise StartupValidationError(
            f"Picture description endpoint returned HTTP {exc.response.status_code}: {url}"
        ) from exc
    except httpx.RequestError as exc:
        raise StartupValidationError(f"Picture description endpoint unreachable at {url}: {exc}") from exc


def log_feature_summary(config: ConversionConfig, docling_cfg: DoclingConverterConfig, role: str) -> None:
    """Log a structured summary of optional feature states.

    Args:
        config: Conversion service configuration.
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
    if config.mlflow_tracing_enabled:
        features["mlflow_tracing"] = {"status": "enabled"}
    else:
        features["mlflow_tracing"] = {
            "status": "disabled",
            "reason": "MLFLOW_TRACING_ENABLED is false",
        }

    # Litestream replication
    if config.litestream_enabled and config.litestream_s3_bucket_name:
        features["litestream_replication"] = {"status": "enabled"}
    else:
        if not config.litestream_enabled:
            reason = "LITESTREAM_ENABLED is false"
        else:
            reason = "LITESTREAM_S3_BUCKET_NAME is empty"
        features["litestream_replication"] = {
            "status": "disabled",
            "reason": reason,
        }

    logger.info(
        "startup feature summary",
        extra={"role": role, "features": features},
    )


def validate_startup(
    config: ConversionConfig,
    docling_cfg: DoclingConverterConfig,
    karakeep_cfg: KarakeepFetcherConfig,
    role: str,
) -> None:
    """Run all startup validation checks.

    Probes required services (S3, KaraKeep) and the optional picture description
    endpoint (when configured), then logs optional feature status.
    Raises on the first required service failure.

    Args:
        config: Conversion service configuration.
        docling_cfg: Docling-specific configuration.
        karakeep_cfg: KaraKeep-specific configuration.
        role: Process role (e.g. "worker", "api").

    Raises:
        StartupValidationError: If any required service is unreachable.
    """
    logger.info("validating startup prerequisites", extra={"role": role})

    probe_s3(config)
    logger.info("S3 probe passed", extra={"role": role})

    probe_karakeep(karakeep_cfg)
    logger.info("KaraKeep probe passed", extra={"role": role})

    probe_picture_description(docling_cfg)
    if docling_cfg.is_picture_description_enabled():
        logger.info("picture description endpoint probe passed", extra={"role": role})

    log_feature_summary(config, docling_cfg, role)
