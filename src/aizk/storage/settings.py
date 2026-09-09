"""Object-storage settings read from the process environment.

The field names carry the ``s3_`` prefix under an ``AIZK_`` env prefix, so the
environment names an operator sets are ``AIZK_S3_*`` and a client's
``config.s3_bucket_name`` reads the same on this class as on any other holder of
the same values.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ObjectStorageSettings(BaseSettings):
    """Connection settings for the object store, read from ``AIZK_S3_*``."""

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

    s3_endpoint_url: str = ""
    s3_bucket_name: str = "aizk"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "us-east-1"
