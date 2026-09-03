"""Data and index integrity for the pipeline_runs.scope_id retype on populated data.

The retype rewrites every stored ``scope_id`` (on ``pipeline_runs``,
``graph_contextualization_output_memo``, and the ``graph_content_fts`` content
index) from the dashed string form to the dashless hex ``sa.Uuid`` stores under
SQLite, and swaps the two real columns' declared type through SQLite's
table-recreate path. This seeds a populated database at the revision before it and
pins what the migration must hold:

- every scope survives the rewrite, readable back as the same ``UUID``;
- the search join (``graph_content_fts.scope_id`` ⋈ ``pipeline_runs.scope_id``)
  still matches after the retype;
- the partial unique active-run index and the memo's unique index survive the
  table recreate;
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

_PRE_RETYPE_REVISION = "c4d5e6f7a8b9"
_RETYPE_REVISION = "d2e3f4a5b6c7"

_SOURCE_IDS = (
    UUID("11111111-1111-1111-1111-111111111111"),
    UUID("0f8cd1a2-0034-4b00-80cd-000000000abc"),
)


def _alembic_cfg(database_url: str) -> Config:
    """Return an Alembic config pointed at the shared migration tree and ``database_url``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _seed(engine, source_ids: tuple[UUID, ...]) -> None:
    """Populate a chunk, its chunking run, its memo entry, and the FTS index per source."""
    with engine.begin() as conn:
        for index, source_id in enumerate(source_ids):
            chunk_id = f"chunk-{index}"
            body = f"body text {index}"
            conn.execute(
                text(
                    "INSERT INTO graph_chunks (chunk_id, content_hash, source_id, heading_path_json, "
                    "ordinal, text, char_count) "
                    "VALUES (:chunk_id, :content_hash, :source_id, '[]', 0, :text, :char_count)"
                ),
                {
                    "chunk_id": chunk_id,
                    "content_hash": f"hash{index}",
                    "source_id": source_id.hex,
                    "text": body,
                    "char_count": len(body),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO pipeline_runs (stage, scope_id, status, derivation_key, version_stamps_json, "
                    "created_at) VALUES ('chunking', :scope_id, 'active', :key, '{}', '2026-09-02T00:00:00')"
                ),
                {"scope_id": str(source_id), "key": f"key-{index}"},
            )
            conn.execute(
                text(
                    "INSERT INTO graph_contextualization_output_memo "
                    "(kind, scope_id, derivation_key, output_text, created_at) "
                    "VALUES ('summary', :scope_id, :key, 'a summary', '2026-09-02T00:00:00')"
                ),
                {"scope_id": str(source_id), "key": f"memo-key-{index}"},
            )
            conn.execute(
                text(
                    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "
                    "VALUES (:text, 'chunk', :chunk_id, NULL, :scope_id)"
                ),
                {"text": body, "chunk_id": chunk_id, "scope_id": str(source_id)},
            )


def test_retype_preserves_scopes_indexes_and_the_search_join(tmp_path: Path) -> None:
    """Upgrading rewrites the stored form on all three tables and keeps the search join intact."""
    url = f"sqlite:///{tmp_path / 'retype.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine, _SOURCE_IDS)

    command.upgrade(cfg, _RETYPE_REVISION)

    with engine.begin() as conn:
        run_scopes = {row[0] for row in conn.execute(text("SELECT scope_id FROM pipeline_runs")).fetchall()}
        memo_scopes = {
            row[0] for row in conn.execute(text("SELECT scope_id FROM graph_contextualization_output_memo")).fetchall()
        }
        fts_scopes = {row[0] for row in conn.execute(text("SELECT scope_id FROM graph_content_fts")).fetchall()}
        expected = {s.hex for s in _SOURCE_IDS}
        assert run_scopes == memo_scopes == fts_scopes == expected, "every scope stores the dashless sa.Uuid form"
        assert {UUID(v) for v in run_scopes} == set(_SOURCE_IDS), "every scope round-trips unchanged"

        joined = conn.execute(
            text(
                "SELECT count(*) FROM graph_content_fts AS f "
                "JOIN pipeline_runs AS cr ON cr.stage = 'chunking' AND cr.scope_id = f.scope_id "
                "WHERE cr.status = 'active'"
            )
        ).scalar()
        assert joined == len(_SOURCE_IDS), "the search join still matches every indexed row"

    inspector = inspect(engine)
    run_indexes = {i["name"]: i for i in inspector.get_indexes("pipeline_runs")}
    assert "uq_pipeline_runs_active_scope" in run_indexes
    assert run_indexes["uq_pipeline_runs_active_scope"]["unique"]
    assert "ix_pipeline_runs_stage_scope_id" in run_indexes

    memo_indexes = {i["name"] for i in inspector.get_indexes("graph_contextualization_output_memo")}
    assert "ix_graph_contextualization_output_memo_key" in memo_indexes
    memo_unique = {
        tuple(uc["column_names"]) for uc in inspector.get_unique_constraints("graph_contextualization_output_memo")
    }
    assert ("kind", "scope_id", "derivation_key") in memo_unique

    engine.dispose()


def test_upgrade_refuses_a_scope_the_rewrite_would_reshape(tmp_path: Path) -> None:
    """A row that is not a canonical dashed UUID stops the upgrade instead of being reshaped.

    The rewrite only strips dashes and the type change accepts whatever results, so
    a truncated value would migrate cleanly into a wrong identity. The migration
    refuses while the original data is still intact.
    """
    url = f"sqlite:///{tmp_path / 'retype-bad.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine, _SOURCE_IDS)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET scope_id = '1111-2222' WHERE scope_id = :scope_id"),
            {"scope_id": str(_SOURCE_IDS[0])},
        )

    with pytest.raises(ValueError, match="outside the 32 lowercase hex storage form"):
        command.upgrade(cfg, _RETYPE_REVISION)

    with engine.begin() as conn:
        surviving = {row[0] for row in conn.execute(text("SELECT scope_id FROM pipeline_runs")).fetchall()}
    assert "1111-2222" in surviving, "the refused migration left the original values in place"

    engine.dispose()


def test_downgrade_restores_the_dashed_form_everywhere(tmp_path: Path) -> None:
    """Downgrading restores the dashed values on all three tables."""
    url = f"sqlite:///{tmp_path / 'retype-down.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine, _SOURCE_IDS)
    command.upgrade(cfg, _RETYPE_REVISION)

    command.downgrade(cfg, _PRE_RETYPE_REVISION)

    with engine.begin() as conn:
        run_scopes = {row[0] for row in conn.execute(text("SELECT scope_id FROM pipeline_runs")).fetchall()}
        memo_scopes = {
            row[0] for row in conn.execute(text("SELECT scope_id FROM graph_contextualization_output_memo")).fetchall()
        }
        fts_scopes = {row[0] for row in conn.execute(text("SELECT scope_id FROM graph_content_fts")).fetchall()}
        expected = {str(s) for s in _SOURCE_IDS}
        assert run_scopes == memo_scopes == fts_scopes == expected, "the dashed form is restored group-for-group"

    engine.dispose()
