"""In-memory stand-ins for the S3 object store."""

from __future__ import annotations

from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class MemoryS3:
    """A dict-backed object store keyed by storage key."""

    def __init__(self, blobs: dict[str, bytes] | None = None, *, bucket: str | None = None) -> None:
        """Wrap ``blobs`` (a fresh dict by default) under ``bucket`` (the configured bucket by default)."""
        from aizk.storage.settings import ObjectStorageSettings

        self.blobs: dict[str, bytes] = {} if blobs is None else blobs
        self.bucket = ObjectStorageSettings(_env_file=None).s3_bucket_name if bucket is None else bucket

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Return the bytes stored under ``s3_key``."""
        return self.blobs[s3_key]

    def upload_file(self, local_path: "Path", s3_key: str) -> str:
        """Store the file's bytes under ``s3_key`` and return its S3 URI."""
        self.blobs[s3_key] = local_path.read_bytes()
        return f"s3://{self.bucket}/{s3_key}"

    def upload_fileobj(self, file_obj: IO[bytes], s3_key: str) -> str:
        """Store the stream's bytes under ``s3_key`` and return its S3 URI."""
        self.blobs[s3_key] = file_obj.read()
        return f"s3://{self.bucket}/{s3_key}"


def install_memory_s3(monkeypatch: "pytest.MonkeyPatch") -> dict[str, bytes]:
    """Patch S3Client to write to an in-memory dict instead of real S3."""
    store = MemoryS3()

    def _init(self, config) -> None:
        self.config = config
        self.bucket = config.s3_bucket_name
        self.client = None

    def _upload_file(self, local_path, s3_key: str) -> str:
        return store.upload_file(local_path, s3_key)

    def _upload_fileobj(self, file_obj, s3_key: str) -> str:
        return store.upload_fileobj(file_obj, s3_key)

    def _get_object_bytes(self, s3_key: str) -> bytes:
        return store.get_object_bytes(s3_key)

    monkeypatch.setattr("aizk.storage.s3.S3Client.__init__", _init)
    monkeypatch.setattr("aizk.storage.s3.S3Client.upload_file", _upload_file)
    monkeypatch.setattr("aizk.storage.s3.S3Client.upload_fileobj", _upload_fileobj)
    monkeypatch.setattr("aizk.storage.s3.S3Client.get_object_bytes", _get_object_bytes)
    return store.blobs
