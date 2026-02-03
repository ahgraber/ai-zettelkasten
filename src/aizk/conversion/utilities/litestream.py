"""Litestream lifecycle management for SQLite replication."""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Literal
import yaml

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import make_url

from aizk.conversion.utilities.config import ConversionConfig

logger = logging.getLogger(__name__)


class LitestreamReplicaS3(BaseModel):
    """Pydantic model for Litestream S3 replica configuration."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["s3"] = "s3"
    bucket: str = Field(min_length=1)
    path: str = Field(min_length=1)
    region: str = Field(min_length=1)
    endpoint: str | None = None
    force_path_style: bool | None = Field(default=None, alias="force-path-style")
    sign_payload: bool | None = Field(default=None, alias="sign-payload")

    @field_validator("path")
    @classmethod
    def _validate_replica_path(cls, value: str) -> str:
        if value.startswith("/"):
            raise ValueError("replica path must be a relative S3 key")
        return value


class LitestreamDBConfig(BaseModel):
    """Pydantic model for Litestream database configuration."""

    path: Path
    replicas: list[LitestreamReplicaS3]

    @field_validator("path")
    @classmethod
    def _validate_db_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("database path must be absolute")
        return value


class LitestreamConfigFile(BaseModel):
    """Pydantic model for Litestream YAML configuration."""

    dbs: list[LitestreamDBConfig]


class LitestreamManager:
    """Manage Litestream restore/replication for a SQLite database."""

    def __init__(self, config: ConversionConfig, role: str) -> None:
        self._config = config
        self._role = role
        self._process: subprocess.Popen[str] | None = None
        self._db_path = _resolve_sqlite_path(config.database_url)

    def start(self) -> None:
        """Start Litestream restore/replication if enabled."""
        if not self._config.litestream_enabled:
            logger.info("Litestream disabled via configuration.")
            return
        if not _role_is_enabled(self._config.litestream_start_role, self._role):
            logger.info("Litestream not started for role=%s.", self._role)
            return
        if self._db_path is None:
            logger.info("Litestream skipped: database_url is not a file-based SQLite path.")
            return
        binary = _resolve_litestream_binary(self._config.litestream_binary)
        bucket = self._bucket_name()
        if not bucket:
            raise RuntimeError("Litestream requires S3 bucket configuration.")
        config_path = _write_config_file(
            db_path=self._db_path,
            bucket=bucket,
            config_path=Path(self._config.litestream_config_path),
            s3_prefix=self._config.litestream_s3_prefix,
            s3_region=self._config.s3_region,
            s3_endpoint_url=self._config.s3_endpoint_url,
            s3_force_path_style=self._config.litestream_s3_force_path_style,
            s3_sign_payload=self._config.litestream_s3_sign_payload,
        )
        if self._config.litestream_restore_on_startup and not self._db_path.exists():
            self._restore(binary=binary, config_path=config_path)
        self._process = self._start_replicate(binary=binary, config_path=config_path)
        atexit.register(self.stop)

    def stop(self) -> None:
        """Terminate the Litestream replication process."""
        if self._process is None:
            return
        if self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
            self._process.wait(timeout=5)
        except ProcessLookupError:
            logger.info("Litestream process already exited.")
        except subprocess.TimeoutExpired:
            logger.warning("Litestream did not exit; sending SIGKILL.")
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                logger.info("Litestream process already exited after SIGKILL.")

    def _restore(self, binary: str, config_path: Path) -> None:
        env = _litestream_env(self._config)
        command = [binary, "restore", "-config", str(config_path), str(self._db_path)]
        logger.info("Running Litestream restore for %s.", self._db_path)
        result = subprocess.run(  # NOQA: S603
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            return
        if self._config.litestream_allow_empty_restore:
            logger.warning(
                "Litestream restore failed; continuing. stdout=%s stderr=%s",
                result.stdout.strip(),
                result.stderr.strip(),
            )
            return
        raise RuntimeError(f"Litestream restore failed: {result.stdout.strip()} {result.stderr.strip()}".strip())

    def _start_replicate(self, binary: str, config_path: Path) -> subprocess.Popen[str]:
        env = _litestream_env(self._config)
        command = [binary, "replicate", "-config", str(config_path)]
        logger.info("Starting Litestream replication for %s.", self._db_path)
        return subprocess.Popen(  # NOQA: S603
            command,
            env=env,
            preexec_fn=os.setpgrp,
        )

    def _bucket_name(self) -> str:
        """Use Litestream-specific S3 bucket if configured, else the default s3 bucket name."""
        if self._config.litestream_s3_bucket_name:
            return self._config.litestream_s3_bucket_name
        return self._config.s3_bucket_name


def _resolve_sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    if not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def _resolve_litestream_binary(binary: str) -> str:
    if Path(binary).is_file():
        return binary
    resolved = shutil.which(binary)
    if not resolved:
        raise RuntimeError("Litestream binary not found in PATH.")
    return resolved


def _role_is_enabled(configured_roles: str, role: str) -> bool:
    roles = {value.strip().lower() for value in configured_roles.split(",") if value.strip()}
    return "both" in roles or role.lower() in roles


def _write_config_file(
    db_path: Path,
    bucket: str,
    config_path: Path,
    s3_prefix: str,
    s3_region: str,
    s3_endpoint_url: str,
    s3_force_path_style: bool,
    s3_sign_payload: bool,
) -> Path:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = s3_prefix.strip("/")
    replica_path = f"{prefix}/{db_path.name}" if prefix else db_path.name
    replica = LitestreamReplicaS3(
        bucket=bucket,
        path=replica_path,
        region=s3_region,
        endpoint=s3_endpoint_url or None,
        force_path_style=True if s3_force_path_style else None,
        sign_payload=True if s3_sign_payload else None,
    )
    config = LitestreamConfigFile(dbs=[LitestreamDBConfig(path=db_path, replicas=[replica])])
    payload = config.model_dump(mode="json", exclude_none=True, by_alias=True)
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def _litestream_env(config: ConversionConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.s3_access_key_id:
        env.setdefault("AWS_ACCESS_KEY_ID", config.s3_access_key_id)
    if config.s3_secret_access_key:
        env.setdefault("AWS_SECRET_ACCESS_KEY", config.s3_secret_access_key)
    if config.s3_region:
        env.setdefault("AWS_REGION", config.s3_region)
    return env
