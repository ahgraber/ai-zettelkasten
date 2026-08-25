"""Unit tests for the operator console's descriptor contract.

The dashboard groups every stage's work-units by the generic
:class:`~aizk.pipeline.lifecycle.WorkUnitStatus` vocabulary. Each stage's rollup
must map *every* native status onto exactly one generic category, so a native
status added later cannot silently vanish from the dashboard.

The coverage capabilities beyond that required set — the pending-work and
staleness derivations — are optional and feature-detected: a stage declares them
or the console omits those surfaces for it entirely.
"""

from __future__ import annotations

import pytest

from aizk.console.descriptors import (
    GRAPH_ROLLUP,
    StageDescriptor,
    rollup_counts,
)
from aizk.console.stages.contextualization import DESCRIPTOR as CONTEXTUALIZATION_DESCRIPTOR
from aizk.console.stages.conversion import CONVERSION_ROLLUP, DESCRIPTOR as CONVERSION_DESCRIPTOR
from aizk.console.stages.extraction import DESCRIPTOR as EXTRACTION_DESCRIPTOR
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


def test_rollup_counts_fails_closed_on_an_unmapped_native_status() -> None:
    """A native status absent from the rollup raises a named error, not a silent miscount."""
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

    with pytest.raises(ValueError, match="no rollup mapping for native status"):
        rollup_counts(descriptor, {"a_future_unmapped_status": 1})


# --- optional coverage capabilities ------------------------------------------


def test_a_descriptor_declares_no_coverage_capabilities_by_default() -> None:
    """The coverage callables are optional, so a stage is never obliged to have the concept."""
    descriptor = StageDescriptor(
        key="minimal",
        label="Minimal",
        list_units=lambda *a, **k: None,
        count_by_status=lambda *a, **k: {},
        get_unit=lambda *a, **k: None,
        columns_template="",
        native_statuses=[],
        rollup=GRAPH_ROLLUP,
        events_stage="minimal",
        actions=[],
    )

    assert descriptor.pending_count is None
    assert descriptor.pending_list is None
    assert descriptor.stale_count is None


@pytest.mark.parametrize(
    "descriptor",
    [CONTEXTUALIZATION_DESCRIPTOR, EXTRACTION_DESCRIPTOR],
    ids=["contextualization", "extraction"],
)
def test_a_stage_with_a_pending_derivation_declares_both_projections(descriptor: StageDescriptor) -> None:
    """A stage declaring a pending-work derivation reports both its count and its listing."""
    assert descriptor.pending_count is not None
    assert descriptor.pending_list is not None


def test_a_stage_without_a_pending_derivation_declares_none() -> None:
    """Conversion has no upstream to derive pending work from, so it declares neither."""
    assert CONVERSION_DESCRIPTOR.pending_count is None
    assert CONVERSION_DESCRIPTOR.pending_list is None


def test_only_extraction_declares_a_staleness_derivation() -> None:
    """Staleness is extraction's concept: its unit is per source, so completed work can fall behind.

    Contextualization's unit is per conversion output, so a re-converted source
    becomes pending again rather than stale, and conversion has no upstream at all.
    """
    assert EXTRACTION_DESCRIPTOR.stale_count is not None
    assert CONTEXTUALIZATION_DESCRIPTOR.stale_count is None
    assert CONVERSION_DESCRIPTOR.stale_count is None


def test_only_extraction_declares_the_re_extract_action() -> None:
    """Re-admission rides the console's declared-action machinery, on the one stage that needs it."""
    assert "re-extract" in {action.key for action in EXTRACTION_DESCRIPTOR.actions}
    assert "re-extract" not in {action.key for action in CONTEXTUALIZATION_DESCRIPTOR.actions}
