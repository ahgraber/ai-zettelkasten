"""Unit tests for the operator console's status-rollup contract.

The dashboard groups every stage's work-units by the generic
:class:`~aizk.pipeline.lifecycle.WorkUnitStatus` vocabulary. Each stage's rollup
must map *every* native status onto exactly one generic category, so a native
status added later cannot silently vanish from the dashboard.
"""

from __future__ import annotations

from aizk.console.descriptors import (
    CONVERSION_ROLLUP,
    GRAPH_ROLLUP,
    StageDescriptor,
    rollup_counts,
)
from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.pipeline.lifecycle import WorkUnitStatus


def test_conversion_rollup_covers_every_native_status() -> None:
    """Every ``ConversionJobStatus`` member maps to exactly one generic category."""
    assert set(CONVERSION_ROLLUP) == set(ConversionJobStatus)
    assert all(target in WorkUnitStatus for target in CONVERSION_ROLLUP.values())


def test_graph_rollup_covers_every_work_unit_status() -> None:
    """Every ``WorkUnitStatus`` member maps to exactly one generic category (identity)."""
    assert set(GRAPH_ROLLUP) == set(WorkUnitStatus)
    assert all(source == target for source, target in GRAPH_ROLLUP.items())


def test_conversion_rollup_matches_the_declared_mapping() -> None:
    """The conversion mapping folds queued/running/failed variants as designed."""
    assert CONVERSION_ROLLUP[ConversionJobStatus.NEW] == WorkUnitStatus.QUEUED
    assert CONVERSION_ROLLUP[ConversionJobStatus.QUEUED] == WorkUnitStatus.QUEUED
    assert CONVERSION_ROLLUP[ConversionJobStatus.RUNNING] == WorkUnitStatus.RUNNING
    assert CONVERSION_ROLLUP[ConversionJobStatus.UPLOAD_PENDING] == WorkUnitStatus.RUNNING
    assert CONVERSION_ROLLUP[ConversionJobStatus.SUCCEEDED] == WorkUnitStatus.SUCCEEDED
    assert CONVERSION_ROLLUP[ConversionJobStatus.FAILED_RETRYABLE] == WorkUnitStatus.FAILED
    assert CONVERSION_ROLLUP[ConversionJobStatus.FAILED_PERM] == WorkUnitStatus.FAILED
    assert CONVERSION_ROLLUP[ConversionJobStatus.CANCELLED] == WorkUnitStatus.CANCELLED


def test_rollup_counts_folds_native_counts_without_loss() -> None:
    """``rollup_counts`` sums native counts into their generic categories, none dropped."""
    descriptor = StageDescriptor(
        key="conversion",
        label="Conversion",
        list_units=lambda *a, **k: None,
        count_by_status=lambda *a, **k: {},
        get_unit=lambda *a, **k: None,
        columns_template="",
        native_statuses=[status.value for status in ConversionJobStatus],
        rollup=CONVERSION_ROLLUP,
        events_stage="conversion",
        actions=[],
    )
    native_counts = {
        ConversionJobStatus.QUEUED.value: 2,
        ConversionJobStatus.UPLOAD_PENDING.value: 1,
        ConversionJobStatus.RUNNING.value: 3,
        ConversionJobStatus.FAILED_RETRYABLE.value: 4,
        ConversionJobStatus.FAILED_PERM.value: 5,
    }

    generic = rollup_counts(descriptor, native_counts)

    assert generic[WorkUnitStatus.QUEUED] == 2
    assert generic[WorkUnitStatus.RUNNING] == 4
    assert generic[WorkUnitStatus.FAILED] == 9
    assert generic[WorkUnitStatus.SUCCEEDED] == 0
    assert sum(generic.values()) == sum(native_counts.values())
