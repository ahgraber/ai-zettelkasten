"""Schema-fidelity tests for the graph-stage mention tables migration.

Asserts the migrated schema for ``graph_mentions`` and
``graph_mention_cooccurrences`` is structurally equivalent to the ORM baseline
(``create_all``) — columns, nullability, and type affinity; indexes (including
uniqueness and partial predicates); foreign keys; primary keys; unique
constraints; and CHECK constraint expressions — and functionally probes the
SQLite-enforced CHECK and partial-unique behavior the two tables rely on.
Scoped to the mention tables so it is independent of any models other test
modules register on ``SQLModel.metadata``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

from aizk.graph.datamodel import Mention, MentionCooccurrence
from tests.db.migrations._schema_equivalence import assert_table_schema_equivalent

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent

# The mention tables are added in one revision on top of the current head at the
# time of writing (the surrogate-chunk_id / source_id rename).
_PREV_REVISION = "f1a2b3c4d5e6"
_MENTION_TABLES = ("graph_mentions", "graph_mention_cooccurrences")
_MENTION_ORM_TABLES = [Mention.__table__, MentionCooccurrence.__table__]


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_mention_tables_match_create_all(tmp_path: Path) -> None:
    """Running migrations produces the same mention-table schema as create_all."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    command.upgrade(_alembic_cfg(migrated_url), "head")

    baseline_engine = create_engine(baseline_url)
    SQLModel.metadata.create_all(baseline_engine, tables=_MENTION_ORM_TABLES)

    migrated = inspect(create_engine(migrated_url))
    baseline = inspect(baseline_engine)

    migrated_tables = set(migrated.get_table_names())
    assert set(_MENTION_TABLES) <= migrated_tables, f"missing mention tables: {migrated_tables}"

    for table in _MENTION_TABLES:
        assert_table_schema_equivalent(migrated, baseline, table)


def test_mention_chunk_id_foreign_keys_into_chunks(tmp_path: Path) -> None:
    """``graph_mentions.chunk_id`` foreign-keys into ``graph_chunks.chunk_id``."""
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    migrated = inspect(create_engine(url))

    fks = migrated.get_foreign_keys("graph_mentions")
    assert any(
        fk["referred_table"] == "graph_chunks"
        and fk["referred_columns"] == ["chunk_id"]
        and fk["constrained_columns"] == ["chunk_id"]
        for fk in fks
    ), "graph_mentions missing chunk_id foreign key into graph_chunks"


def test_cooccurrence_endpoint_foreign_keys_into_mentions(tmp_path: Path) -> None:
    """Both co-occurrence endpoints foreign-key into ``graph_mentions.mention_id``."""
    url = f"sqlite:///{tmp_path / 'fk_cooc.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    migrated = inspect(create_engine(url))

    fks = migrated.get_foreign_keys("graph_mention_cooccurrences")
    referred = {(tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"])) for fk in fks}
    assert (("mention_id_lo",), "graph_mentions", ("mention_id",)) in referred
    assert (("mention_id_hi",), "graph_mentions", ("mention_id",)) in referred


def test_cooccurrence_composite_primary_key(tmp_path: Path) -> None:
    """The co-occurrence table's primary key is the composite ``(run_id, mention_id_lo, mention_id_hi)``.

    A narrower primary key would allow more than one link row per unordered pair
    within a run, breaking the append-only-once-per-pair contract.
    """
    url = f"sqlite:///{tmp_path / 'pk.db'}"
    command.upgrade(_alembic_cfg(url), "head")

    pk = inspect(create_engine(url)).get_pk_constraint("graph_mention_cooccurrences")
    assert sorted(pk["constrained_columns"]) == ["mention_id_hi", "mention_id_lo", "run_id"]


def test_mention_revision_partial_unique_index_rejects_duplicate(tmp_path: Path) -> None:
    """Inserting two identical revision-anchored rows violates the partial unique index.

    The revision-class identity is ``(run_id, chunk_id, surface_form)``; a second
    row with the same tuple (both with ``anchor_kind = revision`` and ``NULL``
    source spans) must be rejected — otherwise the same detection could be
    persisted twice within one run.
    """
    url = f"sqlite:///{tmp_path / 'revision_unique.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_mentions "
        "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
        "input_span_start, input_span_end) "
        "VALUES (:mention_id, :run_id, :chunk_id, 'revision', :surface_form, 'contextualized', "
        "'{}', 0, 10)"
    )
    params = {
        "mention_id": "m1",
        "run_id": 1,
        "chunk_id": "c1",
        "surface_form": "Acme Corp",
    }
    with engine.begin() as conn:
        conn.execute(insert, params)

    with (
        pytest.raises(Exception, match="UNIQUE|unique"),  # noqa: PT011 — driver-specific IntegrityError message
        engine.begin() as conn,
    ):
        conn.execute(insert, {**params, "mention_id": "m2"})


_SOURCE_INSERT = (
    "INSERT INTO graph_mentions "
    "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
    "input_span_start, input_span_end, source_span_start, source_span_end, "
    "source_occurrence_key) "
    "VALUES (:mention_id, :run_id, :chunk_id, 'source', :surface_form, 'raw', '{}', "
    "0, 9, :source_span_start, :source_span_end, :source_occurrence_key)"
)


def test_mention_source_partial_unique_index_rejects_duplicate_but_allows_new_span(tmp_path: Path) -> None:
    """The source-class identity dedupes per occurrence: equal tuples reject, a new span persists.

    The source-class identity is ``(run_id, chunk_id, source_span_start,
    source_span_end, surface_form)``; a second row with the same tuple must be
    rejected — otherwise the same occurrence could be persisted twice within one
    run — while a row differing only in span must persist, because per-occurrence
    expansion emits one source-anchored mention per raw occurrence of a repeated
    surface form.
    """
    url = f"sqlite:///{tmp_path / 'source_unique.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(_SOURCE_INSERT)
    params = {
        "mention_id": "m1",
        "run_id": 1,
        "chunk_id": "c1",
        "surface_form": "Acme Corp",
        "source_span_start": 0,
        "source_span_end": 9,
        "source_occurrence_key": "occ-key-1",
    }
    with engine.begin() as conn:
        conn.execute(insert, params)

    # The identity is the class tuple, not the occurrence key: a differing key
    # does not rescue a duplicate tuple.
    with (
        pytest.raises(Exception, match="UNIQUE|unique"),  # noqa: PT011 — driver-specific IntegrityError message
        engine.begin() as conn,
    ):
        conn.execute(insert, {**params, "mention_id": "m2", "source_occurrence_key": "occ-key-2"})

    # A second raw occurrence of the same surface form (differing only in span)
    # is a distinct mention and persists.
    with engine.begin() as conn:
        conn.execute(
            insert,
            {
                **params,
                "mention_id": "m3",
                "source_span_start": 20,
                "source_span_end": 29,
                "source_occurrence_key": "occ-key-3",
            },
        )


def test_mention_partial_indexes_scope_by_anchor_class(tmp_path: Path) -> None:
    """A source-anchored and a revision-anchored row sharing ``(run_id, chunk_id, surface_form)`` coexist.

    Each partial unique index carries a ``WHERE anchor_kind = ...`` predicate
    scoping it to its own class. If the revision index's predicate were dropped
    or broadened, it would cover both rows' shared ``(run_id, chunk_id,
    surface_form)`` tuple and reject this cross-class insert — the classes must
    deduplicate independently, never against each other. (The source index's
    five-column tuple stays distinct across classes regardless, because the
    revision row's ``NULL`` spans are distinct under SQL ``UNIQUE``.)
    """
    url = f"sqlite:///{tmp_path / 'cross_class.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    source_insert = text(_SOURCE_INSERT)
    revision_insert = text(
        "INSERT INTO graph_mentions "
        "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
        "input_span_start, input_span_end) "
        "VALUES (:mention_id, :run_id, :chunk_id, 'revision', :surface_form, 'contextualized', "
        "'{}', 0, 9)"
    )
    shared = {"run_id": 1, "chunk_id": "c1", "surface_form": "Acme Corp"}

    with engine.begin() as conn:
        conn.execute(
            source_insert,
            {
                **shared,
                "mention_id": "m-source",
                "source_span_start": 0,
                "source_span_end": 9,
                "source_occurrence_key": "occ-key-1",
            },
        )
        conn.execute(revision_insert, {**shared, "mention_id": "m-revision"})


def test_mention_source_span_check_rejects_null_span(tmp_path: Path) -> None:
    """A source-anchored row with a ``NULL`` span/occurrence key fails the CHECK constraint.

    Source anchoring asserts a raw-chunk occurrence exists; a source-anchored row
    with no span would be an unverifiable claim, so the constraint fails closed.
    """
    url = f"sqlite:///{tmp_path / 'source_check.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_mentions "
        "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
        "input_span_start, input_span_end, source_span_start, source_span_end, "
        "source_occurrence_key) "
        "VALUES ('m1', 1, 'c1', 'source', 'Acme Corp', 'raw', '{}', 0, 9, "
        ":source_span_start, :source_span_end, :source_occurrence_key)"
    )
    with (
        pytest.raises(Exception, match="CHECK|check|constraint"),  # noqa: PT011 — driver-specific message
        engine.begin() as conn,
    ):
        conn.execute(
            insert,
            {"source_span_start": None, "source_span_end": None, "source_occurrence_key": None},
        )


def test_mention_revision_anchor_check_rejects_populated_span(tmp_path: Path) -> None:
    """A revision-anchored row with a populated span fails the CHECK constraint.

    Revision anchoring asserts no raw-chunk occurrence witnessed the detection; a
    populated span would misrepresent an unwitnessed detection as verified.
    """
    url = f"sqlite:///{tmp_path / 'revision_check.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_mentions "
        "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
        "input_span_start, input_span_end, source_span_start, source_span_end, "
        "source_occurrence_key) "
        "VALUES ('m1', 1, 'c1', 'revision', 'Acme Corp', 'contextualized', '{}', 0, 9, "
        "0, 9, 'occurrence-key')"
    )
    with (
        pytest.raises(Exception, match="CHECK|check|constraint"),  # noqa: PT011 — driver-specific message
        engine.begin() as conn,
    ):
        conn.execute(insert)


def test_mention_anchor_kind_check_rejects_unknown_kind(tmp_path: Path) -> None:
    """An ``anchor_kind`` outside ``source`` / ``revision`` fails the CHECK constraint.

    A typo'd anchor class would otherwise create a durable but unreachable
    mention row; the constraint makes that fail closed at the persistence
    boundary.
    """
    url = f"sqlite:///{tmp_path / 'anchor_kind_check.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_mentions "
        "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
        "input_span_start, input_span_end) "
        "VALUES ('m1', 1, 'c1', 'anchored', 'Acme Corp', 'raw', '{}', 0, 9)"
    )
    with (
        pytest.raises(Exception, match="CHECK|check|constraint"),  # noqa: PT011 — driver-specific message
        engine.begin() as conn,
    ):
        conn.execute(insert)


def test_mention_input_kind_check_rejects_unknown_kind(tmp_path: Path) -> None:
    """An ``input_kind`` outside ``raw`` / ``contextualized`` fails the CHECK constraint.

    A typo'd input source would otherwise create a durable but unreachable
    mention row; the constraint makes that fail closed at the persistence
    boundary.
    """
    url = f"sqlite:///{tmp_path / 'input_kind_check.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_mentions "
        "(mention_id, run_id, chunk_id, anchor_kind, surface_form, input_kind, input_ref, "
        "input_span_start, input_span_end) "
        "VALUES ('m1', 1, 'c1', 'revision', 'Acme Corp', 'markdown', '{}', 0, 9)"
    )
    with (
        pytest.raises(Exception, match="CHECK|check|constraint"),  # noqa: PT011 — driver-specific message
        engine.begin() as conn,
    ):
        conn.execute(insert)


def test_cooccurrence_ordered_check_rejects_unordered_pair(tmp_path: Path) -> None:
    """A co-occurrence row with ``mention_id_lo >= mention_id_hi`` fails the CHECK constraint.

    The ordered pair is the canonicalization that also excludes self-pairs; an
    unordered or equal pair would let the same unordered pair be recorded twice
    under swapped endpoints, or link a mention to itself.
    """
    url = f"sqlite:///{tmp_path / 'cooc_check.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_mention_cooccurrences (run_id, mention_id_lo, mention_id_hi, chunk_id) "
        "VALUES (1, :lo, :hi, 'c1')"
    )
    with (
        pytest.raises(Exception, match="CHECK|check|constraint"),  # noqa: PT011 — driver-specific message
        engine.begin() as conn,
    ):
        conn.execute(insert, {"lo": "m2", "hi": "m1"})


def test_mention_revision_downgrade_drops_only_mention_tables(tmp_path: Path) -> None:
    """Downgrading one revision drops the mention tables and leaves the rest intact."""
    url = f"sqlite:///{tmp_path / 'down.db'}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    tables_at_head = set(inspect(create_engine(url)).get_table_names())
    assert set(_MENTION_TABLES) <= tables_at_head

    command.downgrade(cfg, _PREV_REVISION)
    tables_after = set(inspect(create_engine(url)).get_table_names())

    assert not (set(_MENTION_TABLES) & tables_after), "mention tables should be dropped"
    assert "graph_chunks" in tables_after, "chunk table remains after downgrade"
    assert "pipeline_runs" in tables_after, "pipeline tables remain after downgrade"
