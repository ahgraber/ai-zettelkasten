"""Operator HTTP surface for the contextualization (graph) stage.

A small, stage-owned FastAPI app for inspecting and steering contextualization
work-units (list / detail / retry / cancel), mirroring the conversion stage's
jobs API. The shared ``pipeline-stage-runtime`` ships no generic operator UI, so
this view is stage-owned for now. The stage is internal/post-conversion, so the
surface is unauthenticated and not owner-scoped; a unit's owner is resolvable via
provenance (``aizk_uuid`` → source) if a future surface needs it.
"""

from __future__ import annotations

from aizk.graph.api.main import create_app

__all__ = ["create_app"]
