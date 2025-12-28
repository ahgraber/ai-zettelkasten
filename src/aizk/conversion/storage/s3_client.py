"""S3 storage client for conversion artifacts."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import NoReturn, Optional

import boto3
from botocore.exceptions import ClientError

from aizk.conversion.utilities.config import ConversionConfig

logger = logging.getLogger(__name__)


class S3Error(Exception):
    """Base exception for S3 errors."""

    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code


class S3UploadError(S3Error):
    """Raised when S3 upload fails."""

    def __init__(self, key: str, message: str):
        super().__init__(f"S3 upload failed for {key}: {message}", "s3_upload_failed")


class S3Client:
    """S3 storage client for uploading conversion artifacts."""

    def __init__(self, config: ConversionConfig):
        """Initialize S3 client.

        Args:
            config: Conversion configuration.
        """
        self.config = config
        self.client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint_url or None,
            aws_access_key_id=config.s3_access_key_id,
            aws_secret_access_key=config.s3_secret_access_key,
            region_name=config.s3_region,
        )
        self.bucket = config.s3_bucket_name

    def upload_file(self, local_path: Path, s3_key: str) -> str:
        """Upload a file to S3 with verification.

        Args:
            local_path: Path to local file.
            s3_key: S3 object key.

        Returns:
            S3 URI (s3://bucket/key).

        Raises:
            S3UploadError: If upload fails or verification fails.
        """
        try:
            # Upload file
            self.client.upload_file(str(local_path), self.bucket, s3_key)

            # Verify upload
            response = self.client.head_object(Bucket=self.bucket, Key=s3_key)
            if not response:
                raise S3UploadError(s3_key, "HEAD request failed after upload")  # NOQA: TRY301
            etag = response.get("ETag")
            if not etag:
                raise S3UploadError(s3_key, "ETag missing after upload")  # NOQA: TRY301

            etag_value = str(etag).strip('"')
            local_md5 = hashlib.md5(local_path.read_bytes()).hexdigest()  # NOQA: S324
            if "-" not in etag_value and etag_value != local_md5:
                raise S3UploadError(s3_key, "ETag mismatch after upload")  # NOQA: TRY301
            if "-" in etag_value:
                content_length = response.get("ContentLength")
                local_size = local_path.stat().st_size
                if content_length != local_size:
                    raise S3UploadError(s3_key, "Content length mismatch after upload")  # NOQA: TRY301

            s3_uri = f"s3://{self.bucket}/{s3_key}"
            logger.info("Uploaded %s to %s", local_path.name, s3_uri)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))
            logger.exception("S3 upload failed for %s: %s (%s)", s3_key, error_message, error_code)
            raise S3UploadError(s3_key, f"{error_code}: {error_message}") from e

        except Exception as e:
            logger.exception("S3 upload failed for %s", s3_key)
            raise S3UploadError(s3_key, str(e)) from e

        else:
            return s3_uri

    def upload_artifacts(
        self,
        s3_prefix: str,
        markdown_path: Path,
        figure_paths: list[Path],
        manifest_path: Optional[Path] = None,
    ) -> dict[str, str | list[str]]:
        """Upload conversion artifacts to S3.

        Args:
            s3_prefix: S3 path prefix (e.g., "aizk_uuid").
            markdown_path: Path to markdown file.
            figure_paths: List of paths to figure files.
            manifest_path: Optional path to manifest.json.

        Returns:
            Dictionary mapping artifact type to S3 URI:
            {
                "markdown": "s3://bucket/prefix/output.md",
                "figures": ["s3://bucket/prefix/figures/image-001.png", ...],
                "manifest": "s3://bucket/prefix/manifest.json",
            }

        Raises:
            S3UploadError: If any upload fails.
        """
        uploaded: dict[str, str | list[str]] = {}

        # Upload markdown
        markdown_key = f"{s3_prefix}/output.md"
        uploaded["markdown"] = self.upload_file(markdown_path, markdown_key)

        # Upload figures
        figure_uris = []
        for fig_path in figure_paths:
            fig_key = f"{s3_prefix}/figures/{fig_path.name}"
            uri = self.upload_file(fig_path, fig_key)
            figure_uris.append(uri)
        uploaded["figures"] = figure_uris

        # Upload manifest if provided
        if manifest_path and manifest_path.exists():
            manifest_key = f"{s3_prefix}/manifest.json"
            uploaded["manifest"] = self.upload_file(manifest_path, manifest_key)

        logger.info(
            "Uploaded %d artifacts to S3 (prefix: %s): markdown, %d figures, manifest=%s",
            1 + len(figure_paths) + (1 if manifest_path else 0),
            s3_prefix,
            len(figure_paths),
            bool(manifest_path),
        )
        return uploaded


def get_s3_client(config: ConversionConfig) -> S3Client:
    """Factory function for S3Client dependency injection.

    Args:
        config: Conversion configuration.

    Returns:
        Configured S3Client instance.
    """
    return S3Client(config)
