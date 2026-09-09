"""Startup probe for the object store."""

from __future__ import annotations

from aizk.storage.s3 import S3Client
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.errors import StartupValidationError


def probe_s3(config: ObjectStorageSettings) -> None:
    """Verify S3 bucket is reachable with a HEAD bucket call.

    Args:
        config: Object-storage settings naming the bucket to reach.

    Raises:
        StartupValidationError: If the S3 bucket is unreachable or credentials
            are invalid.
    """
    try:
        client = S3Client(config)
        client.client.head_bucket(Bucket=config.s3_bucket_name)
    except Exception as exc:
        raise StartupValidationError(f"S3 bucket '{config.s3_bucket_name}' is unreachable: {exc}") from exc
