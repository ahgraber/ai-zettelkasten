"""Guard the canonical source-identity / scope-identity columns across stages.

The pipeline-identity grammar names the durable source identity ``source_id`` and
the run scope ``scope_id`` everywhere they appear as persisted columns. This guards
that every affected model exposes the canonical name and no longer carries its
pre-rename synonym (``aizk_uuid`` / ``doc_id`` / ``scope_key``), so a stray
synonym cannot creep back onto a model. Round-trip persistence by these names is
exercised by the per-stage conversion, graph, and pipeline suites.

The two names also carry different types. ``source_id`` is referential — the value
resolves to a ``sources`` row — so it is a ``UUID``. ``scope_id`` is role-generic:
the run primitive is stage-agnostic and must not assume its scope is a source, so
it is a string. The two therefore store in different forms and **cannot be joined
in SQL**; crossing the seam converts in Python. The tests below pin the type
convention over every registered table, so a table added later cannot drift out of
it, and pin the storage difference itself, because a naive cross-seam join returns
no rows rather than raising.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import String, Uuid, cast, text
from sqlalchemy.types import TypeDecorator, TypeEngine
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.conversion.datamodel import ConversionJob, ConversionOutput, Source
from aizk.graph.datamodel import Chunk, ContextualizationJob, ContextualizationOutputMemo, ExtractionJob
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun, RunStatus


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


#: Identity columns that deviate from the type their name implies, each with its reason.
#:
#: An entry belongs here only as a reviewed decision; anything else reaching the
#: list is drift. ``graph_chunks.source_id`` holds the dashed string so it compares
#: directly against ``PipelineRun.scope_id`` — named for the contract term, typed as
#: a scope key. Reconciling it is a schema change, tracked separately.
_TYPE_DEVIATIONS: dict[tuple[str, str], str] = {
    ("graph_chunks", "source_id"): "typed as a scope key so it compares against PipelineRun.scope_id",
}

_EXPECTED_TYPE = {"source_id": Uuid, "scope_id": String}


def _underlying(column_type: TypeEngine) -> TypeEngine:
    """Unwrap a type decorator to the type it actually stores as.

    SQLModel's ``AutoString`` decorates ``String`` rather than subclassing it, so a
    plain ``isinstance`` against the generic type would miss every string column.
    """
    return column_type.impl if isinstance(column_type, TypeDecorator) else column_type


def _identity_columns() -> list[tuple[str, str, TypeEngine]]:
    """Return every registered ``(table, column, stored type)`` carrying an identity name."""
    return [
        (table_name, column.name, _underlying(column.type))
        for table_name, table in sorted(SQLModel.metadata.tables.items())
        for column in table.columns
        if column.name in _EXPECTED_TYPE
    ]


def test_every_identity_column_carries_the_type_its_name_implies() -> None:
    """`source_id` is a UUID and `scope_id` is a string, on every registered table.

    Enumerated from the model metadata, so a table added later is covered without
    anyone remembering to add it here.
    """
    found = _identity_columns()
    assert found, "no identity columns discovered — the metadata import is not registering tables"

    offenders = [
        f"{table}.{column} is {type(column_type).__name__}, expected {_EXPECTED_TYPE[column].__name__}"
        for table, column, column_type in found
        if (table, column) not in _TYPE_DEVIATIONS and not isinstance(column_type, _EXPECTED_TYPE[column])
    ]
    assert not offenders, "identity columns drifted from the type their name implies: " + "; ".join(offenders)


def test_each_recorded_type_deviation_still_exists() -> None:
    """A deviation that has been reconciled is removed from the list, not left to rot."""
    actual = {(table, column) for table, column, _type in _identity_columns()}
    stale = [f"{table}.{column}" for table, column in _TYPE_DEVIATIONS if (table, column) not in actual]
    assert not stale, f"recorded deviations no longer exist and should be dropped: {stale}"


def test_a_source_id_and_a_scope_id_for_one_source_do_not_compare_in_sql(tmp_path) -> None:
    """The two identity forms are stored differently, so a cross-seam SQL join finds nothing.

    This is why a derivation spanning the seam — a work-unit table against
    ``pipeline_runs`` — resolves its anti-join in Python. The failure is silent, so
    it is pinned: if the forms ever converge, this fails and the Python-side
    conversion can go.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    SQLModel.metadata.create_all(engine, tables=[ExtractionJob.__table__, PipelineRun.__table__])
    source_id = UUID("11111111-1111-1111-1111-111111111111")

    with Session(engine) as session:
        session.add(ExtractionJob(idempotency_key=f"source:{source_id}", source_id=source_id))
        session.add(
            PipelineRun(
                stage="chunking",
                scope_id=str(source_id),
                status=RunStatus.ACTIVE,
                derivation_key="dk",
            )
        )
        session.commit()

        stored_unit = session.execute(text("SELECT source_id FROM graph_extraction_jobs")).scalar_one()
        stored_run = session.execute(text("SELECT scope_id FROM pipeline_runs")).scalar_one()
        assert stored_unit == source_id.hex, "a UUID column stores the dashless form"
        assert stored_run == str(source_id), "a scope column stores the dashed form"
        assert stored_unit != stored_run

        matched_in_sql = session.exec(
            select(ExtractionJob.id).where(cast(ExtractionJob.source_id, String) == PipelineRun.scope_id)
        ).all()
        assert matched_in_sql == [], "comparing the two columns in SQL silently matches nothing"

        enqueued = set(session.exec(select(ExtractionJob.source_id)).all())
        scoped = [UUID(scope_id) for scope_id in session.exec(select(PipelineRun.scope_id)).all()]
        assert [scope for scope in scoped if scope in enqueued] == [source_id], (
            "converting at the boundary is what actually resolves the match"
        )
