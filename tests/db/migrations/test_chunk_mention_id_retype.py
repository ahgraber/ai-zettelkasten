"""Data and index integrity for the chunk/mention surrogate-identity retype on populated data.

The retype rewrites every stored ``chunk_id`` / ``mention_id`` / ``mention_id_lo`` /
``mention_id_hi`` from the dashed string form to the dashless hex ``sa.Uuid`` stores
under SQLite, swaps the declared column types through SQLite's table-recreate
path across all five tables, and rewrites the content index's ``chunk_id`` data in
place (no DDL change — FTS5 has no type affinity). This seeds a populated database
at the revision before it and pins what the migration must hold:

- every identity survives the rewrite, readable back as the same ``UUID``;
- the manifest composite primary key, the co-occurrence composite primary key and
  its ``CHECK (mention_id_lo < mention_id_hi)``, and the mentions table's two
  partial unique indexes all survive the batch table recreate;
- the search join (``aizk.graph.search``'s manifest join,
  ``m.chunk_id = f.chunk_id``) still matches after the retype;
- a row outside the storage form stops the migration instead of being reshaped;
- the downgrade restores the dashed form everywhere, including the FTS index.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent

_PRE_RETYPE_REVISION = "d2e3f4a5b6c7"
_RETYPE_REVISION = "e3f4a5b6c7d8"

_SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")
_CHUNK_ID = UUID("22222222-2222-2222-2222-222222222222")
_MENTION_ID_LO = UUID("44444444-4444-4444-4444-444444444441")
_MENTION_ID_HI = UUID("44444444-4444-4444-4444-444444444442")

_CHUNKING_RUN_ID = 1
_VARIANT_RUN_ID = 2
_EXTRACTION_RUN_ID = 3


def _alembic_cfg(database_url: str) -> Config:
    """Return an Alembic config pointed at the shared migration tree and ``database_url``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _seed(engine) -> None:  # noqa: ANN001 - SQLAlchemy Engine
    """Populate one chunk, its manifest/variant/mention rows, and the FTS index at the pre-retype schema.

    Written directly against the schema as of ``_PRE_RETYPE_REVISION``: at that
    point ``source_id`` / ``scope_id`` are already the ``sa.Uuid`` dashless-hex
    form (retyped by earlier revisions), while ``chunk_id`` / ``mention_id`` /
    ``mention_id_lo`` / ``mention_id_hi`` are still the dashed string form this
    revision retypes.
    """
    body = "boilerplate chunk text"
    with engine.begin() as conn:
        for run_id, stage in (
            (_CHUNKING_RUN_ID, "chunking"),
            (_VARIANT_RUN_ID, "chunk_contextualization"),
            (_EXTRACTION_RUN_ID, "mention_extraction"),
        ):
            conn.execute(
                text(
                    "INSERT INTO pipeline_runs (id, stage, scope_id, status, derivation_key, "
                    "version_stamps_json, created_at) "
                    "VALUES (:id, :stage, :scope_id, 'active', :key, '{}', '2026-09-02T00:00:00')"
                ),
                {"id": run_id, "stage": stage, "scope_id": _SOURCE_ID.hex, "key": f"key-{run_id}"},
            )
        conn.execute(
            text(
                "INSERT INTO graph_chunks (chunk_id, content_hash, source_id, heading_path_json, "
                "ordinal, text, char_count) "
                "VALUES (:chunk_id, 'hash-a', :source_id, '[]', 0, :text, :char_count)"
            ),
            {"chunk_id": str(_CHUNK_ID), "source_id": _SOURCE_ID.hex, "text": body, "char_count": len(body)},
        )
        conn.execute(
            text(
                "INSERT INTO graph_chunk_run_manifest (run_id, chunk_id, span_start, span_end) "
                "VALUES (:run_id, :chunk_id, 0, :span_end)"
            ),
            {"run_id": _CHUNKING_RUN_ID, "chunk_id": str(_CHUNK_ID), "span_end": len(body)},
        )
        conn.execute(
            text(
                "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "
                "VALUES (:text, 'chunk', :chunk_id, NULL, :scope_id)"
            ),
            {"text": body, "chunk_id": str(_CHUNK_ID), "scope_id": _SOURCE_ID.hex},
        )
        conn.execute(
            text(
                "INSERT INTO graph_contextualized_chunks (run_id, summary_run_id, chunking_run_id, chunk_id, "
                "context_version, contextualized_text, derivation_key, created_at) "
                "VALUES (:run_id, :chunking_run_id, :chunking_run_id, :chunk_id, 1, '', 'dk', "
                "'2026-09-02T00:00:00')"
            ),
            {"run_id": _VARIANT_RUN_ID, "chunking_run_id": _CHUNKING_RUN_ID, "chunk_id": str(_CHUNK_ID)},
        )
        for mention_id, surface, start, end, occ in (
            (_MENTION_ID_LO, "Acme", 0, 4, "occ-1"),
            (_MENTION_ID_HI, "Beta", 12, 16, "occ-2"),
        ):
            conn.execute(
                text(
                    "INSERT INTO graph_mentions (mention_id, run_id, chunk_id, anchor_kind, surface_form, "
                    "input_kind, input_ref, input_span_start, input_span_end, source_span_start, "
                    "source_span_end, source_occurrence_key) "
                    "VALUES (:mention_id, :run_id, :chunk_id, 'source', :surface, 'raw', '{}', "
                    ":start, :end, :start, :end, :occ)"
                ),
                {
                    "mention_id": str(mention_id),
                    "run_id": _EXTRACTION_RUN_ID,
                    "chunk_id": str(_CHUNK_ID),
                    "surface": surface,
                    "start": start,
                    "end": end,
                    "occ": occ,
                },
            )
        conn.execute(
            text(
                "INSERT INTO graph_mention_cooccurrences (run_id, mention_id_lo, mention_id_hi, chunk_id) "
                "VALUES (:run_id, :lo, :hi, :chunk_id)"
            ),
            {
                "run_id": _EXTRACTION_RUN_ID,
                "lo": str(_MENTION_ID_LO),
                "hi": str(_MENTION_ID_HI),
                "chunk_id": str(_CHUNK_ID),
            },
        )


def test_retype_preserves_identities_indexes_check_and_the_search_join(tmp_path: Path) -> None:
    """Upgrading rewrites the stored form, keeps every constraint, and leaves the search join intact."""
    url = f"sqlite:///{tmp_path / 'retype.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine)

    command.upgrade(cfg, _RETYPE_REVISION)

    with engine.begin() as conn:
        chunk_id = conn.execute(text("SELECT chunk_id FROM graph_chunks")).scalar_one()
        assert chunk_id == _CHUNK_ID.hex, "graph_chunks.chunk_id stores the dashless hex sa.Uuid form"
        assert UUID(chunk_id) == _CHUNK_ID, "the chunk identity round-trips unchanged"

        manifest_chunk_id = conn.execute(text("SELECT chunk_id FROM graph_chunk_run_manifest")).scalar_one()
        assert manifest_chunk_id == _CHUNK_ID.hex

        variant_chunk_id = conn.execute(text("SELECT chunk_id FROM graph_contextualized_chunks")).scalar_one()
        assert variant_chunk_id == _CHUNK_ID.hex

        mention_ids = {row[0] for row in conn.execute(text("SELECT mention_id FROM graph_mentions")).fetchall()}
        assert mention_ids == {_MENTION_ID_LO.hex, _MENTION_ID_HI.hex}
        mention_chunk_ids = {row[0] for row in conn.execute(text("SELECT chunk_id FROM graph_mentions")).fetchall()}
        assert mention_chunk_ids == {_CHUNK_ID.hex}

        cooc = conn.execute(
            text("SELECT mention_id_lo, mention_id_hi, chunk_id FROM graph_mention_cooccurrences")
        ).one()
        assert cooc.mention_id_lo == _MENTION_ID_LO.hex
        assert cooc.mention_id_hi == _MENTION_ID_HI.hex
        assert cooc.chunk_id == _CHUNK_ID.hex
        assert cooc.mention_id_lo < cooc.mention_id_hi, "relative ordering survives the dash-stripping rewrite"

        fts_chunk_id = conn.execute(text("SELECT chunk_id FROM graph_content_fts")).scalar_one()
        assert fts_chunk_id == _CHUNK_ID.hex, "the FTS index's chunk_id is rewritten to the same dashless form"

        # Replicate aizk.graph.search's manifest join: the search join still
        # matches after the retype, the site that fails silently (zero rows) if
        # the FTS chunk_id form ever diverges from the manifest's.
        joined = conn.execute(
            text(
                "SELECT count(*) FROM graph_content_fts AS f "
                "JOIN pipeline_runs AS cr ON cr.stage = 'chunking' AND cr.scope_id = f.scope_id "
                "  AND cr.status = 'active' "
                "JOIN graph_chunk_run_manifest AS m ON m.run_id = cr.id AND m.chunk_id = f.chunk_id "
                "WHERE f.chunk_id = :chunk_id"
            ),
            {"chunk_id": _CHUNK_ID.hex},
        ).scalar_one()
        assert joined == 1, "the search join still matches the retyped chunk"

        # The CHECK constraint still rejects an unordered/reversed pair.
        with pytest.raises(Exception, match="CHECK|check|constraint"):  # noqa: PT011 — driver-specific message
            conn.execute(
                text(
                    "INSERT INTO graph_mention_cooccurrences (run_id, mention_id_lo, mention_id_hi, chunk_id) "
                    "VALUES (:run_id, :lo, :hi, :chunk_id)"
                ),
                {
                    "run_id": _EXTRACTION_RUN_ID,
                    "lo": _MENTION_ID_HI.hex,
                    "hi": _MENTION_ID_LO.hex,
                    "chunk_id": _CHUNK_ID.hex,
                },
            )

    inspector = inspect(engine)

    manifest_pk = inspector.get_pk_constraint("graph_chunk_run_manifest")
    assert sorted(manifest_pk["constrained_columns"]) == ["chunk_id", "run_id"], (
        "the manifest composite primary key survives the batch recreate"
    )

    cooc_pk = inspector.get_pk_constraint("graph_mention_cooccurrences")
    assert sorted(cooc_pk["constrained_columns"]) == ["mention_id_hi", "mention_id_lo", "run_id"], (
        "the co-occurrence composite primary key survives the batch recreate"
    )

    mention_indexes = {i["name"]: i for i in inspector.get_indexes("graph_mentions")}
    assert mention_indexes["uq_graph_mentions_source_identity"]["unique"]
    assert mention_indexes["uq_graph_mentions_revision_identity"]["unique"]

    engine.dispose()


def test_upgrade_refuses_an_identity_the_rewrite_would_reshape(tmp_path: Path) -> None:
    """A row that is not a canonical dashed UUID stops the upgrade instead of being reshaped.

    The rewrite only strips dashes and the type change accepts whatever results, so
    a truncated value would migrate cleanly into a wrong identity. The migration
    refuses while the original data is still intact.
    """
    url = f"sqlite:///{tmp_path / 'retype-bad.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE graph_chunks SET chunk_id = '1111-2222' WHERE chunk_id = :chunk_id"),
            {"chunk_id": str(_CHUNK_ID)},
        )

    with pytest.raises(ValueError, match="outside the 32 lowercase hex storage form"):
        command.upgrade(cfg, _RETYPE_REVISION)

    with engine.begin() as conn:
        surviving = conn.execute(text("SELECT chunk_id FROM graph_chunks")).scalar_one()
    assert surviving == "1111-2222", "the refused migration left the original value in place"

    engine.dispose()


def test_downgrade_restores_the_dashed_form_everywhere(tmp_path: Path) -> None:
    """Downgrading restores the dashed values on every retyped column, including the FTS index."""
    url = f"sqlite:///{tmp_path / 'retype-down.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine)
    command.upgrade(cfg, _RETYPE_REVISION)

    command.downgrade(cfg, _PRE_RETYPE_REVISION)

    with engine.begin() as conn:
        assert conn.execute(text("SELECT chunk_id FROM graph_chunks")).scalar_one() == str(_CHUNK_ID)
        assert conn.execute(text("SELECT chunk_id FROM graph_chunk_run_manifest")).scalar_one() == str(_CHUNK_ID)
        assert conn.execute(text("SELECT chunk_id FROM graph_contextualized_chunks")).scalar_one() == str(_CHUNK_ID)
        mention_ids = {row[0] for row in conn.execute(text("SELECT mention_id FROM graph_mentions")).fetchall()}
        assert mention_ids == {str(_MENTION_ID_LO), str(_MENTION_ID_HI)}
        cooc = conn.execute(text("SELECT mention_id_lo, mention_id_hi FROM graph_mention_cooccurrences")).one()
        assert cooc.mention_id_lo == str(_MENTION_ID_LO)
        assert cooc.mention_id_hi == str(_MENTION_ID_HI)
        assert conn.execute(text("SELECT chunk_id FROM graph_content_fts")).scalar_one() == str(_CHUNK_ID), (
            "the FTS index's chunk_id is restored to the dashed form"
        )

    engine.dispose()
