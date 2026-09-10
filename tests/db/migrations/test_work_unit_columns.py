"""The graph job tables carry exactly the columns the baseline schema declares.

Structural equivalence between the migrated schema and the ORM is covered by
``tests/db/migrations/test_baseline.py``. This module pins the column set of
each graph job table by name, so a change to the shared lifecycle declaration
in :class:`aizk.pipeline.lifecycle.WorkUnitColumns` that adds or drops a
column on either table is caught here with the literal list to compare against.
"""

from __future__ import annotations

import pytest
from sqlmodel import SQLModel

from aizk.graph.datamodel import ContextualizationJob, ExtractionJob

_LIFECYCLE_COLUMNS = [
    "attempts",
    "created_at",
    "earliest_next_attempt_at",
    "error_code",
    "error_message",
    "finished_at",
    "last_error_at",
    "queued_at",
    "started_at",
    "status",
    "updated_at",
]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            ContextualizationJob,
            sorted([*_LIFECYCLE_COLUMNS, "conversion_output_id", "id", "idempotency_key", "source_id"]),
        ),
        (
            ExtractionJob,
            sorted([*_LIFECYCLE_COLUMNS, "id", "idempotency_key", "source_id"]),
        ),
    ],
    ids=["graph_contextualization_jobs", "graph_extraction_jobs"],
)
def test_graph_job_table_has_exactly_its_declared_columns(model: type[SQLModel], expected: list[str]) -> None:
    """Each graph job table declares the lifecycle columns plus its own identity columns."""
    assert sorted(column.name for column in model.__table__.columns) == expected
