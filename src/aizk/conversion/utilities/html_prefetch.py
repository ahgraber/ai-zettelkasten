"""Re-export the HTML <img> admission helper for importers written against this path."""

from __future__ import annotations

from aizk.conversion.egress.prefetch import (
    PrefetchPolicy,
    _extension_for_content_type,
    _media_type_for_content_type,
    prefetch_images,
)

__all__ = ["PrefetchPolicy", "prefetch_images"]
