"""Provide the graph operator HTTP surface for work-unit inspection and control."""

from __future__ import annotations

import importlib


def __getattr__(name: str) -> object:
    """Import the operator app module on first access to ``create_app`` and return it."""
    if name != "create_app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module("aizk.graph.api.main"), name)


__all__ = ["create_app"]
