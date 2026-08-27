"""Data and index integrity for the graph_chunks.source_id retype on populated data.

The retype rewrites every stored ``graph_chunks.source_id`` from the dashed string
form to the dashless hex ``sa.Uuid`` stores under SQLite, swaps the declared column
type through SQLite's table-recreate path, and renames the content index's identity
column ``source_id`` → ``scope_id`` while keeping its dashed value. This seeds a
populated database at the revision before it and pins what the migration must hold:

- every row's identity survives the rewrite, readable back as the same ``UUID``;
- the sameness-key unique index survives the table recreate, so run-independent
  identity reuse still has its database-level backing;
- the rebuilt index carries the dashed scope key under its new name, so the search
  join against ``pipeline_runs.scope_id`` still matches — a break there returns no
  rows rather than an error;
- a row outside the storage form stops the migration instead of being reshaped;
- the downgrade restores both the dashed column and the index's prior shape.
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

_PRE_RETYPE_REVISION = "b3c4d5e6f7a8"
_RETYPE_REVISION = "c4d5e6f7a8b9"

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
    """Populate chunks, their chunking runs, and the FTS index at the pre-retype schema."""
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
                    "source_id": str(source_id),
                    "text": body,
                    "char_count": len(body),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO pipeline_runs (stage, scope_id, status, derivation_key, version_stamps_json, "
                    "created_at) VALUES ('chunking', :scope_id, 'active', :key, '{}', '2026-08-26T00:00:00')"
                ),
                {"scope_id": str(source_id), "key": f"key-{index}"},
            )
            conn.execute(
                text(
                    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
                    "VALUES (:text, 'chunk', :chunk_id, NULL, :source_id)"
                ),
                {"text": body, "chunk_id": chunk_id, "source_id": str(source_id)},
            )


def _fts_columns(conn) -> set[str]:  # noqa: ANN001 - SQLAlchemy Connection
    """Return the content index's column names, which reflection does not expose for FTS5."""
    return {row[1] for row in conn.execute(text("PRAGMA table_info(graph_content_fts)")).fetchall()}


def test_retype_preserves_identities_indexes_and_the_search_join(tmp_path: Path) -> None:
    """Upgrading rewrites the stored form, keeps both indexes, and leaves the FTS join intact."""
    url = f"sqlite:///{tmp_path / 'retype.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine, _SOURCE_IDS)

    command.upgrade(cfg, _RETYPE_REVISION)

    with engine.begin() as conn:
        stored = {row[0] for row in conn.execute(text("SELECT source_id FROM graph_chunks")).fetchall()}
        assert stored == {s.hex for s in _SOURCE_IDS}, "stored values are the dashless hex sa.Uuid form"
        assert {UUID(value) for value in stored} == set(_SOURCE_IDS), "every identity round-trips unchanged"

        # The index's identity column is renamed and still holds the dashed form,
        # so the search join resolves each indexed row to its active chunking run.
        assert _fts_columns(conn) == {"text", "kind", "chunk_id", "run_id", "scope_id"}
        joined = conn.execute(
            text(
                "SELECT count(*) FROM graph_content_fts AS f "
                "JOIN pipeline_runs AS cr ON cr.stage = 'chunking' AND cr.scope_id = f.scope_id "
                "WHERE cr.status = 'active'"
            )
        ).scalar()
        assert joined == len(_SOURCE_IDS), "the search join still matches every indexed chunk"

    index_names = {index["name"] for index in inspect(engine).get_indexes("graph_chunks")}
    assert "ix_graph_chunks_source_id" in index_names
    sameness = next(
        i for i in inspect(engine).get_indexes("graph_chunks") if i["name"] == "ix_graph_chunks_sameness_key"
    )
    assert sameness["unique"], "the sameness-key index survives the table recreate as unique"

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
    _seed(engine, _SOURCE_IDS)
    with engine.begin() as conn:
        conn.execute(text("UPDATE graph_chunks SET source_id = '1111-2222' WHERE chunk_id = 'chunk-0'"))

    with pytest.raises(ValueError, match="outside the 32 lowercase hex storage form"):
        command.upgrade(cfg, _RETYPE_REVISION)

    with engine.begin() as conn:
        surviving = {row[0] for row in conn.execute(text("SELECT source_id FROM graph_chunks")).fetchall()}
    assert "1111-2222" in surviving, "the refused migration left the original values in place"

    engine.dispose()


def test_downgrade_restores_the_dashed_column_and_the_prior_index(tmp_path: Path) -> None:
    """Downgrading restores the dashed values and rebuilds the index under its prior name."""
    url = f"sqlite:///{tmp_path / 'retype-down.db'}"
    cfg = _alembic_cfg(url)
    engine = create_engine(url)

    command.upgrade(cfg, _PRE_RETYPE_REVISION)
    _seed(engine, _SOURCE_IDS)
    command.upgrade(cfg, _RETYPE_REVISION)

    command.downgrade(cfg, _PRE_RETYPE_REVISION)

    with engine.begin() as conn:
        stored = {row[0] for row in conn.execute(text("SELECT source_id FROM graph_chunks")).fetchall()}
        assert stored == {str(s) for s in _SOURCE_IDS}, "the dashed form is restored group-for-group"

        assert _fts_columns(conn) == {"text", "kind", "chunk_id", "run_id", "source_id"}
        indexed = {row[0] for row in conn.execute(text("SELECT DISTINCT source_id FROM graph_content_fts")).fetchall()}
        assert indexed == stored, "the rebuilt index carries the restored dashed identities"

    engine.dispose()
