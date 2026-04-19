"""Re-export shim: Bookmark → Source (removed in Stage 8)."""

from aizk.conversion.datamodel.source import Source as Bookmark  # noqa: F401

__all__ = ["Bookmark"]
