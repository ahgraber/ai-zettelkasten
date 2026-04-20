"""Health check endpoints for liveness and readiness probes."""

from __future__ import annotations

import asyncio
import logging

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from fastapi import APIRouter, Request, Response, status

from aizk.conversion.api.dependencies import get_config
from aizk.conversion.api.schemas import CheckResult, HealthResponse
from aizk.conversion.db import get_engine
from aizk.conversion.storage.s3_client import S3Client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

_CHECK_TIMEOUT_SECONDS = 5.0


async def _check_db(engine: Engine) -> CheckResult:
    """Verify database connectivity with SELECT 1."""

    def _query() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_query),
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return CheckResult(name="database", status="unavailable", detail="timeout")
    except Exception as exc:
        return CheckResult(name="database", status="unavailable", detail=str(exc))
    return CheckResult(name="database", status="ok")


async def _check_picture_description(config) -> CheckResult:
    """Verify picture description endpoint reachability via GET /models."""
    base_url = config.picture_description_base_url.strip().rstrip("/")
    api_key = config.picture_description_api_key.strip()
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    def _get() -> None:
        response = httpx.get(url, headers=headers, timeout=_CHECK_TIMEOUT_SECONDS)
        response.raise_for_status()

    try:
        await asyncio.wait_for(asyncio.to_thread(_get), timeout=_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return CheckResult(name="picture_description", status="unavailable", detail="timeout")
    except Exception as exc:
        return CheckResult(name="picture_description", status="unavailable", detail=str(exc))
    return CheckResult(name="picture_description", status="ok")


async def _check_s3(s3_client: S3Client) -> CheckResult:
    """Verify S3 reachability with HEAD bucket."""

    def _head() -> None:
        s3_client.client.head_bucket(Bucket=s3_client.config.s3_bucket_name)

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_head),
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return CheckResult(name="s3", status="unavailable", detail="timeout")
    except Exception as exc:
        return CheckResult(name="s3", status="unavailable", detail=str(exc))
    return CheckResult(name="s3", status="ok")


@router.get("/live")
async def liveness() -> HealthResponse:
    """Liveness probe — returns 200 if the process is running."""
    return HealthResponse(status="ok")


@router.get("/ready")
async def readiness(request: Request, response: Response) -> HealthResponse:
    """Readiness probe — validates DB connectivity and S3 reachability."""
    config = get_config(request)
    engine = get_engine(config.database_url)
    s3_client = S3Client(config)

    docling_cfg = request.app.state.docling_config
    check_coros = [_check_db(engine), _check_s3(s3_client)]
    if docling_cfg.is_picture_description_enabled():
        check_coros.append(_check_picture_description(docling_cfg))

    checks = await asyncio.gather(*check_coros)

    all_ok = all(c.status == "ok" for c in checks)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        for check in checks:
            if check.status != "ok":
                logger.warning("readiness check failed", extra={"check": check.name, "detail": check.detail})

    return HealthResponse(
        status="ok" if all_ok else "unavailable",
        checks=list(checks),
    )
