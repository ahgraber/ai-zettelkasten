"""Re-export the Markdown whitespace normalization helper for importers written against this path."""

from __future__ import annotations

from aizk.conversion.processing.whitespace import normalize_whitespace

__all__ = ["normalize_whitespace"]
