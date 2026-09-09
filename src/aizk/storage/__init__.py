"""Object storage: the blob client, its settings, and its startup probe.

Every reader of the object store — the conversion stage's uploader and output
routes, the graph stage's Markdown source, the database backend's replication —
goes through this package, so one settings class owns the ``AIZK_S3_*``
environment and one client owns the boto3 details. It reaches only the leaf
helpers, never a stage.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BlobReader(Protocol):
    """Reads raw bytes from blob storage by key."""

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Return the raw bytes stored under ``s3_key``."""
        ...


__all__ = ["BlobReader"]
