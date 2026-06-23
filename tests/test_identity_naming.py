"""Guard the canonical source-identity / scope-identity column names across stages.

The pipeline-identity grammar names the durable source identity ``source_id`` and
the run scope ``scope_id`` everywhere they appear as persisted columns. This guards
that every affected model exposes the canonical name and no longer carries its
pre-rename synonym (``aizk_uuid`` / ``doc_id`` / ``scope_key``), so a stray
synonym cannot creep back onto a model. Round-trip persistence by these names is
exercised by the per-stage conversion, graph, and pipeline suites.
"""

from __future__ import annotations

import pytest
from sqlmodel import SQLModel

from aizk.conversion.datamodel import ConversionJob, ConversionOutput, Source
from aizk.graph.datamodel import Chunk, ContextualizationJob, ContextualizationOutputMemo
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun


@pytest.mark.parametrize(
    ("model", "present", "absent"),
    [
        (Source, "source_id", "aizk_uuid"),
        (ConversionJob, "source_id", "aizk_uuid"),
        (ConversionOutput, "source_id", "aizk_uuid"),
        (PipelineEvent, "source_id", "aizk_uuid"),
        (ContextualizationJob, "source_id", "aizk_uuid"),
        (Chunk, "source_id", "doc_id"),
        (PipelineRun, "scope_id", "scope_key"),
        (ContextualizationOutputMemo, "scope_id", "scope_key"),
    ],
)
def test_models_use_canonical_identity_names(model: type[SQLModel], present: str, absent: str) -> None:
    """Each model carries the canonical identity column and not its pre-rename synonym."""
    columns = set(model.__table__.columns.keys())
    assert present in columns, f"{model.__tablename__} is missing the canonical column {present!r}"
    assert absent not in columns, f"{model.__tablename__} still carries the pre-rename column {absent!r}"
