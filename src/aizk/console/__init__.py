"""Operator console: the descriptor-driven monitoring surface for every pipeline stage.

The console derives every stage-specific behavior — listing, per-status counts,
columns, actions, and drill-down — from a :class:`StageDescriptor` registered per
pipeline stage. One generic set of routes and templates renders any registered
descriptor.
"""

from __future__ import annotations

from aizk.console.descriptors import (
    GRAPH_ROLLUP,
    StageAction,
    StageDescriptor,
    get_descriptor,
    register_stage,
    registered_stages,
    rollup_counts,
)

__all__ = [
    "GRAPH_ROLLUP",
    "StageAction",
    "StageDescriptor",
    "get_descriptor",
    "register_stage",
    "registered_stages",
    "rollup_counts",
]
